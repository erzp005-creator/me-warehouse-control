"""Regression tests for the pre-allocated-line poison state.

A sales-order line can reach picking already fully reserved -
quantity_allocated == quantity_ordered while quantity_picked == 0 - from POS
phone-order checkout (reserve at checkout), admin reserve-at-creation (the
oversell guard), or a stranded prior allocation. Before the fix, the coverage
and allocation passes keyed on `quantity_ordered > quantity_allocated`, so such
a line got zero pick_tasks; complete_batch's silently-short guard then refused
to finish the batch, stranding the order OPEN ("already in active pick batch")
and poisoning every order in the same batch.

picking_service._normalize_so_reservations now releases the standing
reservation down to the picked floor before planning, so the line is tasked and
completes like any fresh OPEN line, with no inventory double-book.
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


def _so_item_ids(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT item_id FROM sales_order_lines WHERE so_id = %s", (so_id,)
    )
    ids = [r[0] for r in cur.fetchall()]
    cur.close()
    return ids


def _total_allocated(item_ids):
    if not item_ids:
        return 0
    return _query_val(
        "SELECT COALESCE(SUM(quantity_allocated), 0) FROM inventory "
        "WHERE item_id = ANY(%s) AND warehouse_id = 1",
        (item_ids,),
    )


def _reserve_so_at_creation(so_id):
    """Put every line of the SO into the poison shape: fully allocated at the
    line level with quantity_picked == 0, and inventory.quantity_allocated
    bumped to match (a real standing reservation). Returns True only if every
    line could be fully reserved against pickable stock."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT so_line_id, item_id, quantity_ordered "
        "  FROM sales_order_lines WHERE so_id = %s",
        (so_id,),
    )
    lines = cur.fetchall()
    fully_reserved = True
    for so_line_id, item_id, qty in lines:
        remaining = qty
        cur.execute(
            "SELECT inventory_id, (quantity_on_hand - quantity_allocated) AS avail "
            "  FROM inventory "
            " WHERE item_id = %s AND warehouse_id = 1 "
            "   AND (quantity_on_hand - quantity_allocated) > 0 "
            " ORDER BY avail DESC, inventory_id",
            (item_id,),
        )
        for inv_id, avail in cur.fetchall():
            if remaining <= 0:
                break
            take = min(remaining, avail)
            cur.execute(
                "UPDATE inventory SET quantity_allocated = quantity_allocated + %s "
                "WHERE inventory_id = %s",
                (take, inv_id),
            )
            remaining -= take
        if remaining > 0:
            fully_reserved = False
        cur.execute(
            "UPDATE sales_order_lines SET quantity_allocated = %s, quantity_picked = 0 "
            "WHERE so_line_id = %s",
            (qty, so_line_id),
        )
    cur.close()
    return fully_reserved


def _create_batch(client, auth_headers, identifiers):
    return client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": identifiers, "warehouse_id": 1},
        headers=auth_headers,
    )


def _pick_all_tasks(client, auth_headers, batch_id):
    while True:
        nxt = client.get(
            f"/api/picking/batch/{batch_id}/next", headers=auth_headers
        ).get_json()
        if "message" in nxt:
            break
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": nxt["pick_task_id"],
                "scanned_barcode": nxt["upc"],
                "quantity_picked": nxt["quantity_to_pick"],
            },
            headers=auth_headers,
        )


class TestPreAllocatedCreateBatch:
    def test_poison_precondition(self, client, auth_headers):
        # Confirm the setup actually reproduces allocated == ordered, picked 0.
        assert _reserve_so_at_creation(1)
        row = _query_one(
            "SELECT MIN(quantity_allocated - quantity_ordered), MAX(quantity_picked) "
            "FROM sales_order_lines WHERE so_id = 1"
        )
        assert row[0] >= 0  # every line allocated >= ordered
        assert row[1] == 0  # nothing picked yet

    def test_pre_allocated_line_gets_pick_tasks(self, client, auth_headers):
        _reserve_so_at_creation(1)
        resp = _create_batch(client, auth_headers, ["SO-2026-001"])
        assert resp.status_code == 200, resp.get_json()
        batch_id = resp.get_json()["batch_id"]
        # The fix: a task exists for the pre-allocated SO (was 0 before).
        task_count = _query_val(
            "SELECT COUNT(*) FROM pick_tasks WHERE so_id = 1 AND batch_id = %s",
            (batch_id,),
        )
        assert task_count > 0

    def test_pre_allocated_batch_completes(self, client, auth_headers):
        _reserve_so_at_creation(1)
        batch_id = _create_batch(client, auth_headers, ["SO-2026-001"]).get_json()["batch_id"]
        _pick_all_tasks(client, auth_headers, batch_id)
        done = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert done.status_code == 200, done.get_json()
        assert _query_val("SELECT status FROM sales_orders WHERE so_id = 1") == "PICKED"

    def test_no_inventory_double_book(self, client, auth_headers):
        item_ids = _so_item_ids(1)
        _reserve_so_at_creation(1)
        reserved_baseline = _total_allocated(item_ids)
        _create_batch(client, auth_headers, ["SO-2026-001"])
        # Release-then-reallocate nets to the same standing reservation.
        assert _total_allocated(item_ids) == reserved_baseline
        # And the line ends up re-reserved at its ordered quantity.
        assert _query_val(
            "SELECT MIN(quantity_allocated - quantity_ordered) "
            "FROM sales_order_lines WHERE so_id = 1"
        ) == 0


class TestPreAllocatedWaveCreate:
    def test_pre_allocated_wave_gets_tasks_and_completes(self, client, auth_headers):
        _reserve_so_at_creation(1)
        resp = client.post(
            "/api/picking/wave-create",
            json={"so_ids": [1], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        batch_id = resp.get_json()["batch_id"]
        assert _query_val(
            "SELECT COUNT(*) FROM pick_tasks WHERE so_id = 1 AND batch_id = %s",
            (batch_id,),
        ) > 0
        _pick_all_tasks(client, auth_headers, batch_id)
        done = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert done.status_code == 200, done.get_json()
