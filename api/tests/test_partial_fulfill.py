"""Partial-fulfill endpoint.

Covers the happy path against an OPEN seeded SO, the PICKED-with-
short-picks variant, the gates that protect the surface (status,
chaining, permission, validation), and the BO SO shape (address copy,
memo prefix, NULL totals, order_type, audit + event emission).
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


def _query_all(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    return rows


def _set_so_status(so_id, status):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sales_orders SET status = %s WHERE so_id = %s",
        (status, so_id),
    )
    cur.close()


def _set_line_picked(so_line_id, picked):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sales_order_lines SET quantity_picked = %s WHERE so_line_id = %s",
        (picked, so_line_id),
    )
    cur.close()


def _so_line_id_for(so_id):
    return _query_val(
        "SELECT so_line_id FROM sales_order_lines WHERE so_id = %s ORDER BY line_number LIMIT 1",
        (so_id,),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestOpenHappyPath:
    def test_open_so_shrinks_and_creates_backorder(self, client, auth_headers):
        # SO-2026-001 is seeded OPEN with item 1, qty 2.
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}], "reason_detail": "test short"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["original_so"]["so_id"] == 1
        assert data["backorder_so"]["so_number"] == "SO-2026-001-BO"
        assert data["backorder_so"]["status"] == "WAITING_STOCK"

        # Original line shrunk from 2 -> 1.
        ordered_after = _query_val(
            "SELECT quantity_ordered FROM sales_order_lines WHERE so_line_id = %s",
            (sol_id,),
        )
        assert ordered_after == 1

        # BO has one line for item 1, qty 1.
        bo_so_id = data["backorder_so"]["so_id"]
        bo_lines = _query_all(
            "SELECT item_id, quantity_ordered, status FROM sales_order_lines WHERE so_id = %s",
            (bo_so_id,),
        )
        assert bo_lines == [(1, 1, "PENDING")]


class TestPickedHappyPath:
    def test_picked_so_with_short_picks_can_be_partial_fulfilled(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        # Simulate the post-batch state: SO is PICKED with one of the
        # two units actually picked (the other short-confirmed during
        # picking).
        _set_so_status(1, "PICKED")
        _set_line_picked(sol_id, 1)

        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        # Original now has qty_ordered=1, qty_picked=1.
        row = _query_one(
            "SELECT quantity_ordered, quantity_picked FROM sales_order_lines WHERE so_line_id = %s",
            (sol_id,),
        )
        assert row == (1, 1)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class TestStatusGate:
    def test_packed_rejected(self, client, auth_headers):
        _set_so_status(1, "PACKED")
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Can only partial-fulfill" in resp.get_json()["error"]

    def test_shipped_rejected(self, client, auth_headers):
        _set_so_status(1, "SHIPPED")
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_waiting_stock_rejected(self, client, auth_headers):
        _set_so_status(1, "WAITING_STOCK")
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestBoChainingCap:
    def test_so_with_parent_id_rejected(self, client, auth_headers):
        # Promote SO-2026-002 to a "backorder" by stamping parent_so_id.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_orders SET parent_so_id = 1, order_type = 'backorder' WHERE so_id = 2"
        )
        cur.close()
        sol_id = _so_line_id_for(2)

        resp = client.post(
            "/api/admin/sales-orders/2/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "backorder" in resp.get_json()["error"].lower()


class TestOrderTypeGate:
    """order_type widening: partial-fulfill is allowed on every type
    EXCEPT return (inbound RMA) and backorder (chaining cap). The
    replacement/exchange children -- which carry a parent_so_id -- were
    previously rejected by the parent_so_id cap; they are now eligible.
    """

    def _stamp_type(self, so_id, order_type, parent_so_id=1):
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_orders SET order_type = %s, parent_so_id = %s "
            "WHERE so_id = %s",
            (order_type, parent_so_id, so_id),
        )
        cur.close()

    def test_replacement_child_allowed(self, client, auth_headers):
        # SO-2026-002 (so_id 2) becomes a replacement child of SO 1. It
        # carries a parent_so_id, which used to block partial-fulfill.
        self._stamp_type(2, "replacement")
        sol_id = _so_line_id_for(2)
        resp = client.post(
            "/api/admin/sales-orders/2/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        bo = resp.get_json()["backorder_so"]
        assert bo["status"] == "WAITING_STOCK"
        # The spawned BO is a backorder linked to the replacement SO.
        row = _query_one(
            "SELECT order_type, parent_so_id FROM sales_orders WHERE so_id = %s",
            (bo["so_id"],),
        )
        assert row == ("backorder", 2)

    def test_exchange_child_allowed(self, client, auth_headers):
        self._stamp_type(2, "exchange")
        sol_id = _so_line_id_for(2)
        resp = client.post(
            "/api/admin/sales-orders/2/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

    def test_zero_dollar_replacement_partial_fulfills_cleanly(self, client, auth_headers):
        # R2: a replacement is same-SKU $0. Confirm the order_total-
        # agnostic partial-fulfill path succeeds and the BO keeps the
        # NULL-total invariant even when the parent total is 0.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_orders SET order_type = 'replacement', parent_so_id = 1, "
            "order_total = 0 WHERE so_id = 2"
        )
        cur.close()
        sol_id = _so_line_id_for(2)
        resp = client.post(
            "/api/admin/sales-orders/2/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        bo_so_id = resp.get_json()["backorder_so"]["so_id"]
        total = _query_val(
            "SELECT order_total FROM sales_orders WHERE so_id = %s", (bo_so_id,)
        )
        assert total is None

    def test_return_rejected(self, client, auth_headers):
        self._stamp_type(2, "return")
        sol_id = _so_line_id_for(2)
        resp = client.post(
            "/api/admin/sales-orders/2/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "return" in resp.get_json()["error"].lower()

    def test_refund_allowed_by_type_gate(self, client, auth_headers):
        # The refund exclusion was removed; a refund SO passes the type
        # gate (the status/ledger gates keep it unreachable in practice).
        self._stamp_type(2, "refund")
        sol_id = _so_line_id_for(2)
        resp = client.post(
            "/api/admin/sales-orders/2/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()


class TestValidation:
    def test_short_qty_exceeds_unshipped_rejected(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        # SO-2026-001 has item 1 qty 2. Short 5 -> rejected.
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 5}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "exceeds unshipped qty" in resp.get_json()["error"]

    def test_invalid_so_line_id_rejected(self, client, auth_headers):
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": 999999, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "not on SO" in resp.get_json()["error"]

    def test_zero_short_qty_rejected_at_schema_layer(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 0}]},
            headers=auth_headers,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Shrink semantics
# ---------------------------------------------------------------------------


class TestShrinkSemantics:
    def test_shrink_to_zero_hard_deletes_line(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 2}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        # Line is gone.
        remaining = _query_val(
            "SELECT COUNT(*) FROM sales_order_lines WHERE so_line_id = %s",
            (sol_id,),
        )
        assert remaining == 0


# ---------------------------------------------------------------------------
# BO SO shape
# ---------------------------------------------------------------------------


class TestBoSoShape:
    def test_bo_has_order_type_backorder_and_parent_link(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        bo_so_id = resp.get_json()["backorder_so"]["so_id"]

        row = _query_one(
            "SELECT order_type, parent_so_id, status, "
            "       order_total, customer_shipping_paid, "
            "       customer_name, ship_method "
            "  FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        assert row[0] == "backorder"
        assert row[1] == 1
        assert row[2] == "WAITING_STOCK"
        assert row[3] is None  # order_total NULL on BO
        assert row[4] is None  # customer_shipping_paid NULL on BO
        assert row[5] == "Test Customer 1"  # customer copied
        assert row[6] == "GROUND"  # ship_method copied

    def test_bo_memo_prepended(self, client, auth_headers):
        # Set a memo on the parent first.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_orders SET memo = 'original note' WHERE so_id = 1"
        )
        cur.close()
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        bo_so_id = resp.get_json()["backorder_so"]["so_id"]
        memo = _query_val(
            "SELECT memo FROM sales_orders WHERE so_id = %s", (bo_so_id,)
        )
        assert memo.startswith("[BACKORDER from SO-2026-001]")
        assert "original note" in memo

    def test_bo_backorder_opened_at_stamped(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        bo_so_id = resp.get_json()["backorder_so"]["so_id"]
        opened_at = _query_val(
            "SELECT backorder_opened_at FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        assert opened_at is not None


# ---------------------------------------------------------------------------
# Inventory adjustment
# ---------------------------------------------------------------------------


class TestInventoryAdjustment:
    def test_on_hand_nonzero_writes_short_adjustment(self, client, auth_headers):
        # Item 1 in the seed has inventory. Verify a SHORT row lands.
        sol_id = _so_line_id_for(1)
        before = _query_val(
            "SELECT COUNT(*) FROM inventory_adjustments WHERE reason_code = 'SHORT'"
        )
        client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        after = _query_val(
            "SELECT COUNT(*) FROM inventory_adjustments WHERE reason_code = 'SHORT'"
        )
        assert after == before + 1

    def test_on_hand_zero_no_adjustment_row(self, client, auth_headers):
        # Drop on-hand for item 1 to zero across all bins.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("UPDATE inventory SET quantity_on_hand = 0 WHERE item_id = 1")
        cur.close()
        sol_id = _so_line_id_for(1)
        before = _query_val(
            "SELECT COUNT(*) FROM inventory_adjustments WHERE reason_code = 'SHORT'"
        )
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        after = _query_val(
            "SELECT COUNT(*) FROM inventory_adjustments WHERE reason_code = 'SHORT'"
        )
        assert after == before  # no new SHORT row


# ---------------------------------------------------------------------------
# Audit + event emission
# ---------------------------------------------------------------------------


class TestAuditAndEvents:
    def test_two_audit_rows_one_event_emitted(self, client, auth_headers):
        sol_id = _so_line_id_for(1)
        resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        bo_so_id = resp.get_json()["backorder_so"]["so_id"]

        audit_rows = _query_all(
            "SELECT entity_id, action_type, details "
            "  FROM audit_log "
            " WHERE action_type = 'PARTIAL_FULFILL' "
            " ORDER BY log_id",
        )
        # One row on original, one on BO.
        entity_ids = [r[0] for r in audit_rows]
        assert 1 in entity_ids
        assert bo_so_id in entity_ids

        # backorder.opened event landed on integration_events.
        event_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.opened' AND aggregate_id = %s",
            (bo_so_id,),
        )
        assert event_count == 1
