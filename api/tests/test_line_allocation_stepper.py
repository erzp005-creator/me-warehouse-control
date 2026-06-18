"""Tests for the inline quantity_allocated stepper:
PATCH /api/admin/sales-orders/<so_id>/lines/<so_line_id>/allocation.

A rarely-used manual knob to nudge a line's standing reservation. Lowering
releases inventory.quantity_allocated; raising reserves from available stock.
The result is clamped to [quantity_picked, quantity_ordered], and the line and
inventory reservations move in lockstep.
"""

from db_test_context import get_raw_connection


def _query_one(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


def _query_val(sql, params=None):
    row = _query_one(sql, params)
    return row[0] if row else None


def _exec(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cur.close()


def _first_line():
    """(so_line_id, item_id) of SO-2026-001's first line."""
    row = _query_one(
        "SELECT so_line_id, item_id FROM sales_order_lines "
        "WHERE so_id = 1 ORDER BY so_line_id LIMIT 1"
    )
    return row[0], row[1]


def _item_allocated(item_id):
    return _query_val(
        "SELECT COALESCE(SUM(quantity_allocated), 0) FROM inventory "
        "WHERE item_id = %s AND warehouse_id = 1",
        (item_id,),
    )


def _patch(client, auth_headers, so_line_id, delta):
    return client.patch(
        f"/api/admin/sales-orders/1/lines/{so_line_id}/allocation",
        json={"delta": delta},
        headers=auth_headers,
    )


class TestAllocationStepper:
    def test_decrease_releases_inventory(self, client, auth_headers):
        so_line_id, item_id = _first_line()
        # Reserve one unit at the line + inventory level.
        _exec(
            "UPDATE sales_order_lines SET quantity_allocated = 1, quantity_picked = 0 "
            "WHERE so_line_id = %s",
            (so_line_id,),
        )
        _exec(
            "UPDATE inventory SET quantity_allocated = quantity_allocated + 1 "
            "WHERE inventory_id = (SELECT inventory_id FROM inventory "
            "WHERE item_id = %s AND warehouse_id = 1 AND quantity_on_hand > 0 "
            "ORDER BY inventory_id LIMIT 1)",
            (item_id,),
        )
        inv_before = _item_allocated(item_id)

        resp = _patch(client, auth_headers, so_line_id, -1)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["quantity_allocated"] == 0
        assert _query_val(
            "SELECT quantity_allocated FROM sales_order_lines WHERE so_line_id = %s",
            (so_line_id,),
        ) == 0
        assert _item_allocated(item_id) == inv_before - 1

    def test_increase_reserves_inventory(self, client, auth_headers):
        so_line_id, item_id = _first_line()
        _exec(
            "UPDATE sales_order_lines SET quantity_allocated = 0, quantity_picked = 0 "
            "WHERE so_line_id = %s",
            (so_line_id,),
        )
        inv_before = _item_allocated(item_id)
        resp = _patch(client, auth_headers, so_line_id, 1)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["quantity_allocated"] == 1
        assert _item_allocated(item_id) == inv_before + 1

    def test_clamp_not_below_picked(self, client, auth_headers):
        so_line_id, _ = _first_line()
        _exec(
            "UPDATE sales_order_lines SET quantity_allocated = 1, quantity_picked = 1 "
            "WHERE so_line_id = %s",
            (so_line_id,),
        )
        resp = _patch(client, auth_headers, so_line_id, -5)
        assert resp.status_code == 200
        # Clamped at the picked floor (1); nothing moved.
        assert _query_val(
            "SELECT quantity_allocated FROM sales_order_lines WHERE so_line_id = %s",
            (so_line_id,),
        ) == 1

    def test_clamp_not_above_ordered(self, client, auth_headers):
        so_line_id, _ = _first_line()
        ordered = _query_val(
            "SELECT quantity_ordered FROM sales_order_lines WHERE so_line_id = %s",
            (so_line_id,),
        )
        _exec(
            "UPDATE sales_order_lines SET quantity_allocated = %s, quantity_picked = 0 "
            "WHERE so_line_id = %s",
            (ordered, so_line_id),
        )
        resp = _patch(client, auth_headers, so_line_id, 5)
        assert resp.status_code == 200
        assert _query_val(
            "SELECT quantity_allocated FROM sales_order_lines WHERE so_line_id = %s",
            (so_line_id,),
        ) == ordered

    def test_unknown_line_404(self, client, auth_headers):
        resp = _patch(client, auth_headers, 99999999, -1)
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        resp = client.patch(
            "/api/admin/sales-orders/1/lines/1/allocation", json={"delta": -1}
        )
        assert resp.status_code == 401
