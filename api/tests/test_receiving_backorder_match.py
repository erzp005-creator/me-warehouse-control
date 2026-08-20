"""Receipt -> backorder.fulfillable matcher.

When receipt.completed lands inventory in a warehouse, the matcher in
receiving.py looks at WAITING_STOCK backorders in that warehouse and
flips any whose lines are now fully satisfiable to OPEN. Emits
backorder.fulfillable per flipped BO.

Partial satisfaction stays WAITING_STOCK with no event (the operator
explicitly chose "notify only when fully available").
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


def _zero_inventory_for_item(item_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE inventory SET quantity_on_hand = 0, quantity_allocated = 0 "
        " WHERE item_id = %s",
        (item_id,),
    )
    cur.close()


def _create_bo_from_so1(client, auth_headers, short_qty):
    """Drop inventory for item 1 to zero, then partial-fulfill SO-2026-001
    so the resulting BO is WAITING_STOCK with on-hand=0."""
    _zero_inventory_for_item(1)
    sol_id = _query_val(
        "SELECT so_line_id FROM sales_order_lines WHERE so_id = 1 LIMIT 1"
    )
    resp = client.post(
        "/api/admin/sales-orders/1/partial-fulfill",
        json={"lines": [{"so_line_id": sol_id, "short_qty": short_qty}]},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["backorder_so"]["so_id"]


def _receive(client, auth_headers, *, po_id=1, item_id=1, qty=5, bin_id=3):
    return client.post(
        "/api/receiving/receive",
        json={
            "po_id": po_id,
            "items": [{"item_id": item_id, "quantity": qty, "bin_id": bin_id}],
        },
        headers=auth_headers,
    )


class TestFulfillable:
    def test_receipt_satisfies_bo_flips_to_open_and_emits(self, client, auth_headers):
        bo_so_id = _create_bo_from_so1(client, auth_headers, short_qty=2)

        # Sanity: BO is WAITING_STOCK with no fulfillable timestamp.
        before = _query_one(
            "SELECT status, backorder_fulfillable_at FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        assert before[0] == "WAITING_STOCK"
        assert before[1] is None

        # Receive 5 of item 1 (more than the 2 the BO needs).
        resp = _receive(client, auth_headers, qty=5)
        assert resp.status_code == 200, resp.get_json()

        # BO flipped.
        after = _query_one(
            "SELECT status, backorder_fulfillable_at FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        assert after[0] == "OPEN"
        assert after[1] is not None

        # backorder.fulfillable on the outbox.
        emit_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.fulfillable' AND aggregate_id = %s",
            (bo_so_id,),
        )
        assert emit_count == 1

    def test_partial_receipt_stays_waiting_no_event(self, client, auth_headers):
        # SO-2026-001 only has qty 2, so we can only short up to 2.
        # Force on-hand to 0, partial-fulfill 2, then receive only 1
        # (BO needs 2; receipt of 1 is partial; BO stays WAITING).
        bo_so_id = _create_bo_from_so1(client, auth_headers, short_qty=2)

        resp = _receive(client, auth_headers, qty=1)
        assert resp.status_code == 200, resp.get_json()

        status = _query_val(
            "SELECT status FROM sales_orders WHERE so_id = %s", (bo_so_id,)
        )
        assert status == "WAITING_STOCK"

        emit_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.fulfillable' AND aggregate_id = %s",
            (bo_so_id,),
        )
        assert emit_count == 0

    def test_receipt_for_unrelated_item_does_not_flip_bo(self, client, auth_headers):
        bo_so_id = _create_bo_from_so1(client, auth_headers, short_qty=2)
        # Receive item 2 (not the BO's item). BO stays WAITING.
        resp = _receive(client, auth_headers, item_id=2, qty=5, bin_id=4)
        assert resp.status_code == 200, resp.get_json()
        status = _query_val(
            "SELECT status FROM sales_orders WHERE so_id = %s", (bo_so_id,)
        )
        assert status == "WAITING_STOCK"

    def test_receipt_flips_pos_backorder_with_natural_order_type(
        self, client, auth_headers
    ):
        # A POS create-without-stock backorder keeps its natural order_type
        # ('sale', not 'backorder'). The receiving hook matches on
        # status=WAITING_STOCK + warehouse + item alone, so it must clear this
        # SO exactly like an admin -BO. Insert one directly (WAITING_STOCK, a
        # PENDING line on item 1, warehouse 1) and prove the receipt flips it.
        import uuid

        _zero_inventory_for_item(1)
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
        cur.execute(
            "INSERT INTO sales_order_lines "
            "(so_id, item_id, quantity_ordered, quantity_allocated, quantity_picked, "
            " quantity_packed, quantity_shipped, line_number, status) "
            "VALUES (%s, 1, 2, 0, 0, 0, 0, 1, 'PENDING')",
            (pos_bo_id,),
        )
        cur.close()

        resp = _receive(client, auth_headers, qty=5)
        assert resp.status_code == 200, resp.get_json()

        after = _query_one(
            "SELECT status, backorder_fulfillable_at FROM sales_orders WHERE so_id = %s",
            (pos_bo_id,),
        )
        assert after[0] == "OPEN"
        assert after[1] is not None
