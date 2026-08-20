"""Backorders release on any inventory increase, not just a PO receipt.

A backorder could only escape WAITING_STOCK if the stock arrived through the
receiving endpoint. Every other way inventory lands in a warehouse left it
sitting there, even with the item on the shelf in a pickable bin in that exact
warehouse. The matcher itself was already generic over (warehouse_id,
item_id); only its wiring was narrow.

The original backorder-release contract still governs what a release means, and none of it
changes here: flip only when ALL lines are satisfiable, stay silent on partial
satisfaction, never consume inventory, and fire notifications after commit.

Deliberately NOT covered, and pinned as such below: the restock-on-revert
paths. An operator undoing a pick has stock transiently back on the shelf, and
flipping a backorder open underneath them would make it pickable against
inventory that is about to move again.
"""

import json
import uuid

from db_test_context import get_raw_connection

PICKABLE_BIN = 3        # A-01-01, bin_type='Pickable'
NON_PICKABLE_BIN = 1    # RECV-01, bin_type='Staging'


def _exec(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cur.close()


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


def _create_waiting_bo(client, auth_headers, short_qty=2):
    """Zero out item 1, then partial-fulfill SO-2026-001 so the backorder it
    spawns is WAITING_STOCK against on-hand of nothing."""
    _exec(
        "UPDATE inventory SET quantity_on_hand = 0, quantity_allocated = 0 "
        " WHERE item_id = %s",
        (1,),
    )
    sol_id = _query_val(
        "SELECT so_line_id FROM sales_order_lines WHERE so_id = 1 LIMIT 1"
    )
    resp = client.post(
        "/api/admin/sales-orders/1/partial-fulfill",
        json={"lines": [{"so_line_id": sol_id, "short_qty": short_qty}]},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    bo_so_id = resp.get_json()["backorder_so"]["so_id"]
    assert _bo_status(bo_so_id) == "WAITING_STOCK"
    return bo_so_id


def _bo_status(bo_so_id):
    return _query_val(
        "SELECT status FROM sales_orders WHERE so_id = %s", (bo_so_id,)
    )


def _fulfillable_payloads(bo_so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT payload FROM integration_events "
        " WHERE event_type = 'backorder.fulfillable' AND aggregate_id = %s",
        (bo_so_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]


def _released(bo_so_id):
    """(status, event count, the source on the single event if there is one)."""
    payloads = _fulfillable_payloads(bo_so_id)
    source = payloads[0].get("source") if len(payloads) == 1 else None
    return _bo_status(bo_so_id), len(payloads), source


def _direct_adjust(client, auth_headers, *, qty, bin_id=PICKABLE_BIN,
                   item_id=1, adjustment_type="ADD"):
    return client.post(
        "/api/admin/adjustments/direct",
        json={
            "item_id": item_id, "bin_id": bin_id, "warehouse_id": 1,
            "adjustment_type": adjustment_type, "quantity": qty,
            "reason": "found on shelf",
        },
        headers=auth_headers,
    )


def _insert_pending_adjustment(*, item_id, bin_id, quantity_change,
                               cycle_count_id=None, reason_code="CORRECTION"):
    return _query_val(
        """
        INSERT INTO inventory_adjustments
            (item_id, bin_id, warehouse_id, quantity_change, reason_code,
             status, adjusted_by, cycle_count_id, external_id)
        VALUES (%s, %s, 1, %s, %s, 'PENDING', 'admin', %s, gen_random_uuid())
        RETURNING adjustment_id
        """,
        (item_id, bin_id, quantity_change, reason_code, cycle_count_id),
    )


def _approve(client, auth_headers, adj_ids):
    return client.post(
        "/api/admin/adjustments/review",
        json={"decisions": [
            {"adjustment_id": a, "action": "approve"} for a in adj_ids
        ]},
        headers=auth_headers,
    )


class TestDirectAdjustment:
    def test_add_releases_the_backorder(self, client, auth_headers):
        # The headline case: a found-in-the-warehouse unit goes on the books
        # through a direct adjustment, and the backorder waiting on that exact
        # SKU should stop waiting.
        bo = _create_waiting_bo(client, auth_headers)

        resp = _direct_adjust(client, auth_headers, qty=5)
        assert resp.status_code == 201, resp.get_json()

        status, events, source = _released(bo)
        assert status == "OPEN"
        assert events == 1
        assert source == "adjustment"
        assert _query_val(
            "SELECT backorder_fulfillable_at FROM sales_orders WHERE so_id = %s",
            (bo,),
        ) is not None

    def test_partial_add_leaves_it_waiting_and_silent(self, client, auth_headers):
        # Locked spec: notify only when the warehouse can actually pull the
        # whole backorder.
        bo = _create_waiting_bo(client, auth_headers, short_qty=2)

        assert _direct_adjust(client, auth_headers, qty=1).status_code == 201

        assert _released(bo) == ("WAITING_STOCK", 0, None)

    def test_add_into_a_non_pickable_bin_does_not_release(self, client, auth_headers):
        # Stock in a receiving bin is not stock a picker can take. The matcher
        # counts Pickable and PickableStaging only.
        bo = _create_waiting_bo(client, auth_headers)

        resp = _direct_adjust(client, auth_headers, qty=50, bin_id=NON_PICKABLE_BIN)
        assert resp.status_code == 201, resp.get_json()

        assert _released(bo) == ("WAITING_STOCK", 0, None)

    def test_remove_never_releases(self, client, auth_headers):
        bo = _create_waiting_bo(client, auth_headers)
        _exec(
            "UPDATE inventory SET quantity_on_hand = 100 "
            " WHERE item_id = 1 AND bin_id = %s", (PICKABLE_BIN,),
        )

        resp = _direct_adjust(client, auth_headers, qty=1, adjustment_type="REMOVE")
        assert resp.status_code == 201, resp.get_json()

        # A decrement cannot satisfy anything, so the matcher must not run.
        assert _released(bo) == ("WAITING_STOCK", 0, None)

    def test_unrelated_item_does_not_release(self, client, auth_headers):
        bo = _create_waiting_bo(client, auth_headers)

        resp = _direct_adjust(client, auth_headers, qty=50, item_id=2, bin_id=4)
        assert resp.status_code == 201, resp.get_json()

        assert _released(bo) == ("WAITING_STOCK", 0, None)


class TestAdjustmentApprovalQueue:
    """This path lands inventory with raw SQL and never calls add_inventory,
    which is why hooking the release inside add_inventory() would have missed it."""

    def test_approval_releases_the_backorder(self, client, auth_headers):
        bo = _create_waiting_bo(client, auth_headers)
        adj = _insert_pending_adjustment(
            item_id=1, bin_id=PICKABLE_BIN, quantity_change=5,
        )

        resp = _approve(client, auth_headers, [adj])
        assert resp.status_code == 200, resp.get_json()

        status, events, source = _released(bo)
        assert status == "OPEN"
        assert events == 1
        assert source == "adjustment"

    def test_a_rejected_adjustment_releases_nothing(self, client, auth_headers):
        bo = _create_waiting_bo(client, auth_headers)
        adj = _insert_pending_adjustment(
            item_id=1, bin_id=PICKABLE_BIN, quantity_change=5,
        )

        resp = client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj, "action": "reject"}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

        assert _released(bo) == ("WAITING_STOCK", 0, None)

    def test_a_batch_emits_once_per_backorder_not_once_per_row(
        self, client, auth_headers
    ):
        # Two approvals for the same item must not fire the matcher twice and
        # put two backorder.fulfillable rows on the outbox for one backorder.
        bo = _create_waiting_bo(client, auth_headers)
        adjs = [
            _insert_pending_adjustment(item_id=1, bin_id=PICKABLE_BIN, quantity_change=3),
            _insert_pending_adjustment(item_id=1, bin_id=PICKABLE_BIN, quantity_change=4),
        ]

        resp = _approve(client, auth_headers, adjs)
        assert resp.status_code == 200, resp.get_json()

        status, events, _ = _released(bo)
        assert status == "OPEN"
        assert events == 1


class TestInterWarehouseTransfer:
    def test_transfer_into_the_backorders_warehouse_releases_it(
        self, client, auth_headers
    ):
        # Sourcing a backordered item from another warehouse lands it in
        # exactly the warehouse the backorder is waiting on.
        bo = _create_waiting_bo(client, auth_headers)
        # Park stock in warehouse 2 to move across.
        _exec(
            "INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, "
            "                  bin_type, pick_sequence, putaway_sequence, external_id) "
            "SELECT zone_id, 2, 'W2-XFER', 'W2-XFER', 'Pickable', 10, 10, gen_random_uuid() "
            "  FROM zones LIMIT 1 "
            "ON CONFLICT DO NOTHING"
        )
        src_bin = _query_val("SELECT bin_id FROM bins WHERE bin_code = 'W2-XFER'")
        _exec(
            "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) "
            "VALUES (1, %s, 2, 50) "
            "ON CONFLICT (item_id, bin_id, lot_number) "
            "DO UPDATE SET quantity_on_hand = 50",
            (src_bin,),
        )

        resp = client.post(
            "/api/admin/inter-warehouse-transfer",
            json={
                "item_id": 1,
                "from_bin_id": src_bin, "from_warehouse_id": 2,
                "to_bin_id": PICKABLE_BIN, "to_warehouse_id": 1,
                "quantity": 5, "reason": "sourcing a backorder",
            },
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201), resp.get_json()

        status, events, source = _released(bo)
        assert status == "OPEN"
        assert events == 1
        assert source == "transfer"


class TestAdjustmentCsvImport:
    def test_import_releases_once_for_a_repeated_sku(self, client, auth_headers):
        # A file correcting the same SKU on several rows must run the matcher
        # once per (warehouse, item), not once per row.
        bo = _create_waiting_bo(client, auth_headers)
        sku = _query_val("SELECT sku FROM items WHERE item_id = 1")
        bin_code = _query_val(
            "SELECT bin_code FROM bins WHERE bin_id = %s", (PICKABLE_BIN,)
        )

        resp = client.post(
            "/api/admin/import/inventory-adjustments",
            json={"records": [
                {"sku": sku, "warehouse": "APT-LAB", "bin": bin_code,
                 "qty": 3, "reason": "found"},
                {"sku": sku, "warehouse": "APT-LAB", "bin": bin_code,
                 "qty": 4, "reason": "found"},
            ]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

        status, events, source = _released(bo)
        assert status == "OPEN"
        assert events == 1
        assert source == "adjustment"


class TestReceiptPathUnchanged:
    def test_a_receipt_still_releases_and_is_labelled_receipt(
        self, client, auth_headers
    ):
        # The matcher moved out of receiving.py into inventory_service. Its
        # behaviour must not have moved with it, and the receipt path keeps
        # its own source label.
        bo = _create_waiting_bo(client, auth_headers)

        resp = client.post(
            "/api/receiving/receive",
            json={"po_id": 1, "items": [
                {"item_id": 1, "quantity": 5, "bin_id": PICKABLE_BIN},
            ]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

        status, events, source = _released(bo)
        assert status == "OPEN"
        assert events == 1
        assert source == "receipt"


class TestRevertPathsStaySilent:
    """Pins the out-of-scope decision so a later change has to argue with a
    failing test rather than quietly widen the trigger."""

    def test_cancelling_an_order_does_not_release_a_backorder(
        self, client, auth_headers
    ):
        bo = _create_waiting_bo(client, auth_headers)
        # SO-2026-002 is a separate open order on the same item. Cancelling it
        # unwinds any allocation back onto the shelf.
        resp = client.post(
            "/api/admin/sales-orders/2/cancel",
            json={"cancellation_reason": "other"},
            headers=auth_headers,
        )
        assert resp.status_code in (200, 400, 404), resp.get_json()

        # Whatever the cancel did to inventory, it must not have released.
        assert _fulfillable_payloads(bo) == []
