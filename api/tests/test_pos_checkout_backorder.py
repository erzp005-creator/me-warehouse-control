"""POST /api/v1/pos/checkout -- backorder (create-without-stock) path.

A ship-mode order with backorder=true creates a regular sales order at the
backorder warehouse that
carries its payment normally but lands at status=WAITING_STOCK with no inventory
movement, so it sits on the backorder screen and clears through the receiving
auto-fulfill hook when stock arrives. It keeps its natural order_type, so it
composes with sale / replacement / exchange.

Coverage:
- A backorder sale -> WAITING_STOCK SO at the backorder warehouse, PENDING
  lines, money populated,
  backorder_opened_at set, shipped_at NULL, and ZERO inventory movement.
- Same for order_type replacement / exchange (status is orthogonal); an
  exchange still auto-creates the parent RMA for the returned items.
- No backorder warehouse resolvable -> 422 backorder_warehouse_unavailable.
- Unknown sku on a backorder line -> 422 fulfillment_failed.
- A NORMAL (non-backorder) line with an empty bin still 422s (the schema
  relaxation is scoped to backorder only -- no regression).
"""

import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from _wms_token_helpers import delete_token, insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def pos_token(seed_data):
    plaintext = f"pos-co-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["pos.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


# --- helpers ---------------------------------------------------------


def _post(client, token, body):
    return client.post(
        "/api/v1/pos/checkout",
        headers={"X-WMS-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _ensure_backorder_warehouse():
    """Resolve the warehouse the backorder path assigns to, creating it if this
    test transaction does not already have one, and point
    BACKORDER_WAREHOUSE_CODE at it. Setting the env var here rather than per
    test means a test cannot create the warehouse and forget to select it.
    Returns its warehouse_id."""
    os.environ["BACKORDER_WAREHOUSE_CODE"] = "BO"
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT warehouse_id FROM warehouses WHERE warehouse_code = 'BO'")
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO warehouses (warehouse_code, warehouse_name, address) "
            "VALUES ('BO', 'Backorder Fulfillment Center', 'test') RETURNING warehouse_id",
        )
        row = cur.fetchone()
    cur.close()
    return row[0]


def _backorder_body(*, sku="TST-001", qty=1, order_type=None,
                    parent_so_number=None, returned_items=None):
    """A ship-mode backorder body: the line carries no stock location (empty
    warehouse_id / bin_id) and backorder=true. is_phone_order rides true, the
    way the POS sends every ship-mode order."""
    body = {
        "idempotency_key":  str(uuid.uuid4()),
        "external_txn_ref": f"WC-{uuid.uuid4().hex[:12]}",
        # A real seeded Sentry user: post-SSO the cashier_id IS a Sentry
        # username, and the backorder.opened emit resolves opened_by from it,
        # so the actor must exist in `users` (as every real cashier does).
        "cashier_id":       "admin",
        "terminal_id":      "reg-01",
        "completed_at":     "2026-05-09T14:23:11Z",
        "is_phone_order":   True,
        "backorder":        True,
        "payment_summary": {
            "method":         "card",
            "subtotal_cents": 1999 * qty,
            "tax_cents":      162 * qty,
            "shipping_cents": 500,
            "total_cents":    2161 * qty + 500,
            "tenders": [
                {
                    "type":         "card",
                    "amount_cents": 2161 * qty + 500,
                    "card_brand":   "Visa",
                    "card_last4":   "1111",
                    "auth_code":    "000289",
                    "external_ref": "0000005400911209",
                }
            ],
        },
        "lines": [
            {
                "sku":              sku,
                "warehouse_id":     "",
                "bin_id":           "",
                "quantity":         qty,
                "unit_price_cents": 1999,
                "tax_cents":        162 * qty,
                "line_total_cents": 2161 * qty,
            }
        ],
    }
    if order_type is not None:
        body["order_type"] = order_type
    if parent_so_number is not None:
        body["parent_so_number"] = parent_so_number
    if returned_items is not None:
        body["returned_items"] = returned_items
    return body


def _read_so(so_number):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT so_id, status, warehouse_id, order_type, shipped_at,
               order_total, customer_shipping_paid, backorder_opened_at,
               parent_so_id
          FROM sales_orders
         WHERE so_number = %s
        """,
        (so_number,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _read_lines(so_number):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sol.status, sol.quantity_ordered, sol.quantity_allocated,
               sol.quantity_shipped
          FROM sales_order_lines sol
          JOIN sales_orders so ON so.so_id = sol.so_id
         WHERE so.so_number = %s
         ORDER BY sol.line_number
        """,
        (so_number,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _inventory_totals(sku):
    """(sum on_hand, sum allocated) across every inventory row for the sku, so a
    test can assert the backorder moved no stock anywhere."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(inv.quantity_on_hand), 0),
               COALESCE(SUM(inv.quantity_allocated), 0)
          FROM inventory inv
          JOIN items i ON i.item_id = inv.item_id
         WHERE i.sku = %s
        """,
        (sku,),
    )
    row = cur.fetchone()
    cur.close()
    return int(row[0]), int(row[1])


def _insert_parent_so(so_number, *, warehouse_id=1, line_sku="TST-001"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, "
        " order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", "SHIPPED", warehouse_id, "sale", "web", str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.execute("SELECT item_id FROM items WHERE sku = %s", (line_sku,))
    item_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_allocated, quantity_picked, "
        " quantity_packed, quantity_shipped, line_number, status) "
        "VALUES (%s, %s, 1, 0, 0, 0, 1, 1, 'SHIPPED')",
        (so_id, item_id),
    )
    cur.close()
    return so_id


def _rma_row(parent_no):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT so_id, order_type FROM sales_orders WHERE so_number = %s",
        (f"{parent_no}-RMA",),
    )
    r = cur.fetchone()
    cur.close()
    return r


# --- tests -----------------------------------------------------------


class TestBackorderSale:
    def test_creates_waiting_stock_so_at_backorder_wh_with_no_inventory_movement(
        self, client, pos_token
    ):
        bo_id = _ensure_backorder_warehouse()
        before = _inventory_totals("TST-001")

        resp = _post(client, pos_token["plaintext"], _backorder_body(qty=2))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        so_number = resp.get_json()["so_number"]

        so_id, status, wh_id, order_type, shipped_at, order_total, \
            shipping_paid, opened_at, parent = _read_so(so_number)
        assert status == "WAITING_STOCK"
        assert wh_id == bo_id             # assigned to the backorder wh, not the empty line wh
        assert order_type == "sale"
        assert shipped_at is None         # nothing shipped
        assert opened_at is not None      # dashboard age + ready-to-ship tab
        # Money carried normally (paid now, unlike an admin partial-fulfill BO).
        assert order_total is not None and float(order_total) > 0
        assert shipping_paid is not None and float(shipping_paid) == 5.0

        # No inventory moved anywhere for the sku.
        assert _inventory_totals("TST-001") == before

    def test_lines_land_pending_with_no_allocation(self, client, pos_token):
        _ensure_backorder_warehouse()
        resp = _post(client, pos_token["plaintext"], _backorder_body(qty=3))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        lines = _read_lines(resp.get_json()["so_number"])
        assert len(lines) == 1
        status, ordered, allocated, shipped = lines[0]
        assert status == "PENDING"
        assert ordered == 3
        assert allocated == 0
        assert shipped == 0


class TestBackorderComposesWithOrderType:
    def test_replacement_keeps_order_type_and_is_waiting_stock(
        self, client, pos_token
    ):
        bo_id = _ensure_backorder_warehouse()
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        _insert_parent_so(parent_no)

        resp = _post(
            client,
            pos_token["plaintext"],
            _backorder_body(order_type="replacement", parent_so_number=parent_no),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        so_id, status, wh_id, order_type, *_rest = _read_so(
            resp.get_json()["so_number"]
        )
        assert status == "WAITING_STOCK"
        assert order_type == "replacement"
        assert wh_id == bo_id

    def test_exchange_creates_rma_and_is_waiting_stock(self, client, pos_token):
        _ensure_backorder_warehouse()
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        _insert_parent_so(parent_no, line_sku="TST-001")

        resp = _post(
            client,
            pos_token["plaintext"],
            _backorder_body(
                order_type="exchange",
                parent_so_number=parent_no,
                returned_items=[{"sku": "TST-001", "quantity": 1}],
            ),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        _so_id, status, _wh, order_type, *_rest = _read_so(
            resp.get_json()["so_number"]
        )
        assert status == "WAITING_STOCK"
        assert order_type == "exchange"
        # The exchange still books the return even though the new item backorders:
        # the <parent>-RMA exists (order_type 'return', the RMA lifecycle type).
        rma = _rma_row(parent_no)
        assert rma is not None and rma[1] == "return"


class TestBackorderRejections:
    def test_missing_backorder_warehouse_is_422(self, client, pos_token, monkeypatch):
        # No BACKORDER_WAREHOUSE_CODE and more than one warehouse in the
        # deployment, so the backorder path cannot resolve one to assign to.
        monkeypatch.delenv("BACKORDER_WAREHOUSE_CODE", raising=False)
        resp = _post(client, pos_token["plaintext"], _backorder_body())
        assert resp.status_code == 422, resp.get_data(as_text=True)
        assert resp.get_json()["error_kind"] == "backorder_warehouse_unavailable"

    def test_unknown_sku_is_422(self, client, pos_token):
        _ensure_backorder_warehouse()
        resp = _post(
            client, pos_token["plaintext"], _backorder_body(sku="NOPE-999")
        )
        assert resp.status_code == 422, resp.get_data(as_text=True)
        assert resp.get_json()["error_kind"] == "fulfillment_failed"


class TestNoRegression:
    def test_normal_line_with_empty_bin_still_422(self, client, pos_token):
        # The line-schema relaxation is scoped to backorder: a normal order that
        # drops warehouse/bin must still fail validation at the boundary.
        body = _backorder_body()
        body["backorder"] = False
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 422, resp.get_data(as_text=True)


def _read_events(so_id):
    """Every integration_events row for a sales_order aggregate, newest first."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT event_type, event_version, aggregate_type, aggregate_id,
               warehouse_id, payload
          FROM integration_events
         WHERE aggregate_type = 'sales_order' AND aggregate_id = %s
         ORDER BY event_id DESC
        """,
        (so_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _user_external_id(username):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT external_id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    return str(row[0]) if row else None


class TestBackorderOpenedEmit:
    """A POS create-without-stock backorder emits backorder.opened on the
    integration_events outbox at parity with the admin partial-fulfill BO, so
    a POS backorder reaches the same Teams path. The SO has no parent (it IS
    the backorder), so parent_so_* are null; the cashier is a real Sentry user
    via SSO, so opened_by resolves from cashier_id."""

    def test_emits_backorder_opened_with_null_parent_and_resolved_actor(
        self, client, pos_token, monkeypatch
    ):
        # Validate the payload against the (widened) schema on emit, so this
        # proves the null-parent POS shape is schema-VALID, not merely that a
        # row lands.
        monkeypatch.setenv("SENTRY_VALIDATE_EVENT_SCHEMAS", "true")
        _ensure_backorder_warehouse()
        body = _backorder_body(qty=2)
        resp = _post(client, pos_token["plaintext"], body)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        so_number = resp.get_json()["so_number"]
        so_id = _read_so(so_number)[0]

        events = _read_events(so_id)
        assert len(events) == 1, events
        etype, ever, agg_type, agg_id, wh, payload = events[0]
        assert etype == "backorder.opened"
        assert ever == 1
        assert agg_type == "sales_order"
        assert agg_id == so_id
        # No parent: the POS backorder is a self-standing SO, not a -BO child.
        assert payload["parent_so_external_id"] is None
        assert payload["parent_so_number"] is None
        assert payload["backorder_so_number"] == so_number
        # opened_by resolved from the cashier's Sentry username (SSO).
        assert payload["opened_by_user_external_id"] == _user_external_id("admin")
        assert payload["warehouse_id"] == wh
        assert {it["sku"] for it in payload["items"]} == {"TST-001"}
        assert payload["items"][0]["qty"] == 2

    def test_replay_does_not_double_emit(self, client, pos_token):
        _ensure_backorder_warehouse()
        body = _backorder_body()
        first = _post(client, pos_token["plaintext"], body)
        assert first.status_code == 200, first.get_data(as_text=True)
        so_id = _read_so(first.get_json()["so_number"])[0]
        # Same idempotency_key -> replay; must not emit a second event.
        second = _post(client, pos_token["plaintext"], body)
        assert second.status_code == 200, second.get_data(as_text=True)
        assert len(_read_events(so_id)) == 1


class TestBackorderOpenedSchemaWiden:
    """The parent_so_* widen: a null-parent (POS) payload and a non-null-parent
    (admin) payload both validate; a missing required field still fails."""

    def _payload(self, parent_ext, parent_num):
        return {
            "backorder_so_external_id": "11111111-1111-1111-1111-111111111111",
            "backorder_so_number": "POS-1",
            "parent_so_external_id": parent_ext,
            "parent_so_number": parent_num,
            "warehouse_id": 1,
            "customer_name": "Cust",
            "items": [{"item_external_id": "22222222-2222-2222-2222-222222222222",
                       "sku": "TST-001", "item_name": "T", "qty": 1}],
            "opened_by_user_external_id": "33333333-3333-3333-3333-333333333333",
            "opened_at": "2026-07-27T00:00:00Z",
        }

    def test_null_parent_validates(self):
        from services.events_schema_registry import get_validator
        get_validator("backorder.opened", 1).validate(self._payload(None, None))

    def test_non_null_parent_still_validates(self):
        from services.events_schema_registry import get_validator
        get_validator("backorder.opened", 1).validate(
            self._payload("44444444-4444-4444-4444-444444444444", "SO-9"))

    def test_missing_required_still_fails(self):
        import jsonschema
        from services.events_schema_registry import get_validator
        p = self._payload(None, None)
        del p["items"]
        with pytest.raises(jsonschema.ValidationError):
            get_validator("backorder.opened", 1).validate(p)
