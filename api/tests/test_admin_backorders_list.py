"""GET /api/admin/backorders.

Tabs: 'waiting' (status=WAITING_STOCK) and 'ready-to-ship'
(status IN OPEN/PICKED/PACKED AND backorder_opened_at IS NOT NULL).
Returns per-BO items[] + days_waiting; ready-to-ship adds
fulfillable_since.
"""

import uuid

from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _create_bo_via_partial_fulfill(client, auth_headers, so_id=1, short_qty=1):
    sol_id = _query_val(
        "SELECT so_line_id FROM sales_order_lines WHERE so_id = %s LIMIT 1",
        (so_id,),
    )
    resp = client.post(
        f"/api/admin/sales-orders/{so_id}/partial-fulfill",
        json={"lines": [{"so_line_id": sol_id, "short_qty": short_qty}]},
        headers=auth_headers,
    )
    return resp.get_json()["backorder_so"]["so_id"]


class TestListBackorders:
    def test_waiting_tab_lists_waiting_bo(self, client, auth_headers):
        bo_so_id = _create_bo_via_partial_fulfill(client, auth_headers)

        resp = client.get("/api/admin/backorders?tab=waiting", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["tab"] == "waiting"

        bo_ids = [b["so_id"] for b in data["backorders"]]
        assert bo_so_id in bo_ids

        # Card payload shape.
        target = next(b for b in data["backorders"] if b["so_id"] == bo_so_id)
        assert target["status"] == "WAITING_STOCK"
        assert target["parent_so_number"] == "SO-2026-001"
        assert target["so_number"] == "SO-2026-001-BO"
        assert target["customer_name"] == "Test Customer 1"
        assert len(target["items"]) == 1
        assert target["items"][0]["sku"] == "TST-001"
        assert target["items"][0]["qty"] == 1
        assert target["days_waiting"] == 0  # just created
        assert target["fulfillable_since"] is None

    def test_default_tab_is_waiting(self, client, auth_headers):
        _create_bo_via_partial_fulfill(client, auth_headers)
        resp = client.get("/api/admin/backorders", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["tab"] == "waiting"

    def test_invalid_tab_rejected(self, client, auth_headers):
        resp = client.get("/api/admin/backorders?tab=bogus", headers=auth_headers)
        assert resp.status_code == 400

    def test_ready_to_ship_tab_shows_flipped_bo(self, client, auth_headers):
        # Force the BO to OPEN with a fulfillable_at stamp (simulating
        # the receipt-hook flip without running a full receipt cycle).
        bo_so_id = _create_bo_via_partial_fulfill(client, auth_headers)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_orders SET status = 'OPEN', "
            "                        backorder_fulfillable_at = NOW() "
            " WHERE so_id = %s",
            (bo_so_id,),
        )
        cur.close()

        # Waiting tab should NOT include it.
        waiting = client.get(
            "/api/admin/backorders?tab=waiting", headers=auth_headers
        ).get_json()
        assert bo_so_id not in [b["so_id"] for b in waiting["backorders"]]

        # Ready-to-ship tab should.
        ready = client.get(
            "/api/admin/backorders?tab=ready-to-ship", headers=auth_headers
        ).get_json()
        assert bo_so_id in [b["so_id"] for b in ready["backorders"]]
        target = next(b for b in ready["backorders"] if b["so_id"] == bo_so_id)
        assert target["fulfillable_since"] is not None

    def test_warehouse_id_filter(self, client, auth_headers):
        _create_bo_via_partial_fulfill(client, auth_headers)
        # Warehouse 1 returns the BO; warehouse 9999 returns empty.
        wh1 = client.get(
            "/api/admin/backorders?tab=waiting&warehouse_id=1",
            headers=auth_headers,
        ).get_json()
        wh_none = client.get(
            "/api/admin/backorders?tab=waiting&warehouse_id=9999",
            headers=auth_headers,
        ).get_json()
        assert len(wh1["backorders"]) >= 1
        assert wh_none["backorders"] == []

    def test_non_backorder_so_not_listed(self, client, auth_headers):
        # SO-2026-002 is a regular sale, not a BO. Listing should
        # NOT include it.
        resp = client.get("/api/admin/backorders?tab=waiting", headers=auth_headers)
        bo_ids = [b["so_id"] for b in resp.get_json()["backorders"]]
        assert 2 not in bo_ids

    def test_pos_backorder_with_natural_order_type_is_listed(
        self, client, auth_headers
    ):
        # A POS create-without-stock backorder keeps its natural order_type
        # ('sale' here, not 'backorder') and is marked only by WAITING_STOCK +
        # backorder_opened_at. Dropping the order_type filter is what lets it
        # show; without the drop this SO would be invisible on the dashboard.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sales_orders "
            "(so_number, customer_name, status, warehouse_id, order_type, "
            " order_source, external_id, backorder_opened_at) "
            "VALUES (%s, 'POS Cust', 'WAITING_STOCK', 1, 'sale', 'pos', %s, NOW()) "
            "RETURNING so_id",
            (f"POS-BO-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())),
        )
        pos_bo_id = cur.fetchone()[0]
        item_id = _query_val("SELECT item_id FROM items WHERE sku = 'TST-001'")
        cur.execute(
            "INSERT INTO sales_order_lines "
            "(so_id, item_id, quantity_ordered, quantity_allocated, quantity_picked, "
            " quantity_packed, quantity_shipped, line_number, status) "
            "VALUES (%s, %s, 1, 0, 0, 0, 0, 1, 'PENDING')",
            (pos_bo_id, item_id),
        )
        cur.close()

        resp = client.get("/api/admin/backorders?tab=waiting", headers=auth_headers)
        assert resp.status_code == 200
        listed = {b["so_id"]: b for b in resp.get_json()["backorders"]}
        assert pos_bo_id in listed
        assert listed[pos_bo_id]["status"] == "WAITING_STOCK"

        # The existing admin -BO backorder still lists too (no regression from
        # dropping the order_type filter).
        admin_bo_id = _create_bo_via_partial_fulfill(client, auth_headers)
        again = client.get(
            "/api/admin/backorders?tab=waiting", headers=auth_headers
        ).get_json()
        ids = [b["so_id"] for b in again["backorders"]]
        assert pos_bo_id in ids and admin_bo_id in ids
