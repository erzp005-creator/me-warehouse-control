"""GET /admin/sales-orders/<id>/related -- the Related Records family walk.

Post-fulfillment records hang off their original via parent_so_id. The
admin SO modal's Related Records tab needs the WHOLE family from whichever
member the operator happens to be looking at, not one hop, because real
families in production are three deep: a sale, its backorder, and an RMA
raised against that backorder.

Tests pin:

- Fan-out: an original with several children returns all of them, and the
  same set comes back when asked from any child (siblings + parent).
- Depth: a three-generation chain is fully visible from the root, the
  middle, and the leaf, with depth values that let the UI indent.
- Voided returns are INCLUDED and flagged. Every SO listing hides them;
  hiding them here is what makes an RMA look like it vanished.
- Refund credit memos are included (the Sales Orders page filters them
  out via exclude_post_fulfillment).
- A cycle in parent_so_id terminates instead of hanging the request.
- A childless, parentless SO returns just itself, related_count 0.
- Unknown so_id is a 404, not an empty family.
- The detail GET carries parent_so_number (the Refunds modal renders it).
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


def _insert_so(so_number=None, *, order_type="sale", parent_so_id=None,
               status="SHIPPED", voided=False):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders (so_number, customer_name, status, "
        " warehouse_id, external_id, order_type, parent_so_id, voided_at) "
        "VALUES (%s, %s, %s, 1, %s, %s, %s, %s) RETURNING so_id",
        (so_number or f"SO-REL-{uuid.uuid4().hex[:8]}", "Cust", status,
         str(uuid.uuid4()), order_type, parent_so_id,
         "2026-08-01T00:00:00Z" if voided else None),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-REL-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012",
         str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_line(so_id, item_id, *, ordered=2, shipped=0, received=0,
                 line_number=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        " (so_id, item_id, quantity_ordered, quantity_shipped, "
        "  quantity_received, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'PENDING') RETURNING so_line_id",
        (so_id, item_id, ordered, shipped, received, line_number),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _point_parent(so_id, parent_so_id):
    """Repoint parent_so_id directly. Used only to manufacture a cycle,
    which no application path can create."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sales_orders SET parent_so_id = %s WHERE so_id = %s",
        (parent_so_id, so_id),
    )
    cur.close()


def _fetch(client, auth_headers, so_id):
    resp = client.get(
        f"/api/admin/sales-orders/{so_id}/related", headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _by_id(body):
    return {r["so_id"]: r for r in body["records"]}


class TestFanOut:
    """Mirrors production SO 650065, which carries four children at once:
    two RMAs and two exchanges."""

    def _family(self):
        parent = _insert_so(order_type="sale")
        kids = {
            "rma":    _insert_so(order_type="return", parent_so_id=parent,
                                 status="RECEIVED"),
            "rma2":   _insert_so(order_type="return", parent_so_id=parent,
                                 status="RECEIVED"),
            "exch":   _insert_so(order_type="exchange", parent_so_id=parent,
                                 status="CANCELLED"),
            "exch2":  _insert_so(order_type="exchange", parent_so_id=parent),
        }
        return parent, kids

    def test_parent_sees_every_child(self, client, auth_headers):
        parent, kids = self._family()
        body = _fetch(client, auth_headers, parent)
        ids = set(_by_id(body))
        assert ids == {parent, *kids.values()}
        assert body["related_count"] == 4

    def test_child_sees_parent_and_all_siblings(self, client, auth_headers):
        parent, kids = self._family()
        body = _fetch(client, auth_headers, kids["rma"])
        ids = set(_by_id(body))
        # Same family from a child as from the parent: parent, self, and
        # all three siblings.
        assert ids == {parent, *kids.values()}
        assert body["related_count"] == 4

    def test_is_current_marks_only_the_requested_record(self, client, auth_headers):
        parent, kids = self._family()
        body = _fetch(client, auth_headers, kids["exch"])
        current = [r for r in body["records"] if r["is_current"]]
        assert len(current) == 1
        assert current[0]["so_id"] == kids["exch"]

    def test_depth_places_children_one_below_the_root(self, client, auth_headers):
        parent, kids = self._family()
        rows = _by_id(_fetch(client, auth_headers, parent))
        assert rows[parent]["depth"] == 0
        for so_id in kids.values():
            assert rows[so_id]["depth"] == 1

    def test_order_type_and_parent_pointer_round_trip(self, client, auth_headers):
        parent, kids = self._family()
        rows = _by_id(_fetch(client, auth_headers, parent))
        assert rows[kids["rma"]]["order_type"] == "return"
        assert rows[kids["exch"]]["order_type"] == "exchange"
        assert rows[kids["exch"]]["status"] == "CANCELLED"
        assert rows[kids["rma"]]["parent_so_id"] == parent
        assert rows[parent]["parent_so_id"] is None


class TestThreeGenerations:
    """Mirrors production 652120 -> 652120-BO -> 652120-BO-RMA. A backorder
    can be RMA'd, so the family is a tree and a one-hop lookup would hide
    the grandchild from the original order entirely."""

    def _chain(self):
        root = _insert_so(order_type="sale")
        bo = _insert_so(order_type="backorder", parent_so_id=root)
        rma = _insert_so(order_type="return", parent_so_id=bo,
                         status="OPEN")
        return root, bo, rma

    def test_root_sees_the_grandchild(self, client, auth_headers):
        root, bo, rma = self._chain()
        body = _fetch(client, auth_headers, root)
        assert set(_by_id(body)) == {root, bo, rma}
        assert body["related_count"] == 2

    def test_leaf_sees_the_grandparent(self, client, auth_headers):
        root, bo, rma = self._chain()
        body = _fetch(client, auth_headers, rma)
        assert set(_by_id(body)) == {root, bo, rma}

    def test_middle_sees_both_directions(self, client, auth_headers):
        root, bo, rma = self._chain()
        assert set(_by_id(_fetch(client, auth_headers, bo))) == {root, bo, rma}

    def test_depth_increments_per_generation(self, client, auth_headers):
        root, bo, rma = self._chain()
        # Depth must be measured from the family root, not from the
        # requested record, so the same tree indents identically no matter
        # which member the operator opened.
        for asked in (root, bo, rma):
            rows = _by_id(_fetch(client, auth_headers, asked))
            assert rows[root]["depth"] == 0
            assert rows[bo]["depth"] == 1
            assert rows[rma]["depth"] == 2

    def test_uncle_and_nephew_both_appear(self, client, auth_headers):
        """A refund on the root and an RMA on the backorder are in the same
        family and must see each other."""
        root, bo, rma = self._chain()
        refund = _insert_so(order_type="refund", parent_so_id=root)
        assert set(_by_id(_fetch(client, auth_headers, rma))) == {
            root, bo, rma, refund,
        }


class TestDeliberateInclusions:
    def test_voided_return_is_included_and_flagged(self, client, auth_headers):
        """mig 076 soft-deletes an RMA and every listing drops it. Here it
        stays, flagged, so the UI can grey it instead of losing it."""
        parent = _insert_so(order_type="sale")
        live = _insert_so(order_type="return", parent_so_id=parent)
        dead = _insert_so(order_type="return", parent_so_id=parent,
                          voided=True)

        rows = _by_id(_fetch(client, auth_headers, parent))
        assert dead in rows, "voided RMA must not disappear from the family"
        assert rows[dead]["is_voided"] is True
        assert rows[dead]["voided_at"] is not None
        assert rows[live]["is_voided"] is False
        assert rows[live]["voided_at"] is None

    def test_voided_return_can_be_opened_from_the_family(self, client, auth_headers):
        """Clicking the greyed row must resolve -- the detail GET has no
        voided filter, so navigation into it works."""
        parent = _insert_so(order_type="sale")
        dead = _insert_so(order_type="return", parent_so_id=parent,
                          voided=True)
        resp = client.get(f"/api/admin/sales-orders/{dead}",
                          headers=auth_headers)
        assert resp.status_code == 200
        assert _fetch(client, auth_headers, dead)["related_count"] == 1

    def test_refund_credit_memo_is_included(self, client, auth_headers):
        parent = _insert_so(order_type="sale")
        refund = _insert_so(order_type="refund", parent_so_id=parent)
        rows = _by_id(_fetch(client, auth_headers, parent))
        assert refund in rows
        assert rows[refund]["order_type"] == "refund"

    def test_replacement_and_backorder_are_included(self, client, auth_headers):
        parent = _insert_so(order_type="sale")
        repl = _insert_so(order_type="replacement", parent_so_id=parent)
        bo = _insert_so(order_type="backorder", parent_so_id=parent,
                        status="WAITING_STOCK")
        rows = _by_id(_fetch(client, auth_headers, parent))
        assert {repl, bo} <= set(rows)
        assert rows[bo]["status"] == "WAITING_STOCK"


class TestInlineLines:
    def test_each_record_carries_its_own_lines(self, client, auth_headers):
        item_a, item_b = _insert_item(), _insert_item()
        parent = _insert_so(order_type="sale")
        _insert_line(parent, item_a, ordered=3, shipped=3)
        rma = _insert_so(order_type="return", parent_so_id=parent,
                         status="RECEIVED")
        _insert_line(rma, item_b, ordered=1, received=1)

        rows = _by_id(_fetch(client, auth_headers, parent))
        assert len(rows[parent]["lines"]) == 1
        assert rows[parent]["lines"][0]["quantity_shipped"] == 3
        assert len(rows[rma]["lines"]) == 1
        # A return's meaningful quantity is what came back, not what
        # shipped, so quantity_received has to be on the wire.
        assert rows[rma]["lines"][0]["quantity_received"] == 1
        assert rows[rma]["lines"][0]["sku"]
        assert rows[rma]["lines"][0]["item_name"]

    def test_lineless_record_returns_an_empty_list(self, client, auth_headers):
        parent = _insert_so(order_type="sale")
        rows = _by_id(_fetch(client, auth_headers, parent))
        assert rows[parent]["lines"] == []

    def test_lines_come_back_in_line_number_order(self, client, auth_headers):
        item_a, item_b = _insert_item(), _insert_item()
        parent = _insert_so(order_type="sale")
        _insert_line(parent, item_b, line_number=2)
        _insert_line(parent, item_a, line_number=1)
        rows = _by_id(_fetch(client, auth_headers, parent))
        assert [l["line_number"] for l in rows[parent]["lines"]] == [1, 2]


class TestEdges:
    def test_lone_order_returns_only_itself(self, client, auth_headers):
        so_id = _insert_so(order_type="sale")
        body = _fetch(client, auth_headers, so_id)
        assert body["related_count"] == 0
        assert [r["so_id"] for r in body["records"]] == [so_id]
        assert body["records"][0]["is_current"] is True

    def test_unknown_so_id_is_404(self, client, auth_headers):
        resp = client.get(
            "/api/admin/sales-orders/99999999/related", headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_cycle_terminates_instead_of_hanging(self, client, auth_headers):
        """parent_so_id has no CHECK forbidding a cycle. No application
        path builds one, but an unguarded recursive CTE would spin forever
        if the data ever did, taking a worker with it. The depth cap makes
        it return."""
        a = _insert_so(order_type="sale")
        b = _insert_so(order_type="backorder", parent_so_id=a)
        _point_parent(a, b)  # a -> b -> a

        resp = client.get(
            f"/api/admin/sales-orders/{a}/related", headers=auth_headers,
        )
        assert resp.status_code == 200
        ids = {r["so_id"] for r in resp.get_json()["records"]}
        assert ids == {a, b}

    def test_self_parent_terminates(self, client, auth_headers):
        so_id = _insert_so(order_type="sale")
        _point_parent(so_id, so_id)
        resp = client.get(
            f"/api/admin/sales-orders/{so_id}/related", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert {r["so_id"] for r in resp.get_json()["records"]} == {so_id}

    def test_relationship_ignores_so_number_convention(self, client, auth_headers):
        """Legacy children exist whose numbers do not follow the
        "<orig>-RMA" convention (POS-REF-*, hand-entered). The family must
        come from parent_so_id alone."""
        parent = _insert_so("REL-PARENT-A", order_type="sale")
        odd = _insert_so("POS-REF-99999", order_type="refund",
                         parent_so_id=parent)
        # A number that LOOKS like a child of the parent but carries no
        # parent_so_id is a stranger and must not be pulled in.
        _insert_so("REL-PARENT-A-RMA", order_type="return")

        ids = set(_by_id(_fetch(client, auth_headers, parent)))
        assert ids == {parent, odd}


class TestDetailParentNumber:
    def test_detail_get_returns_parent_so_number(self, client, auth_headers):
        """The Refunds modal renders detail.parent_so_number as "Original
        order". The field never existed, so that row never displayed."""
        parent = _insert_so("REL-ORIG-1", order_type="sale")
        refund = _insert_so(order_type="refund", parent_so_id=parent)

        body = client.get(f"/api/admin/sales-orders/{refund}",
                          headers=auth_headers).get_json()
        assert body["sales_order"]["parent_so_number"] == "REL-ORIG-1"
        assert body["sales_order"]["parent_so_id"] == parent

    def test_root_order_has_null_parent_so_number(self, client, auth_headers):
        so_id = _insert_so(order_type="sale")
        body = client.get(f"/api/admin/sales-orders/{so_id}",
                          headers=auth_headers).get_json()
        assert body["sales_order"]["parent_so_number"] is None
