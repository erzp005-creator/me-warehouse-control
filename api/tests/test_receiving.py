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


class TestPOLookup:
    def test_lookup_po_by_barcode(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_number"] == "PO-2026-001"
        assert data["purchase_order"]["status"] == "OPEN"
        assert len(data["lines"]) == 10, "PO-2026-001 should have 10 lines"

    def test_lookup_po_by_number(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["purchase_order"]["po_id"] == 1

    def test_lookup_po_not_found(self, client, auth_headers):
        resp = client.get("/api/receiving/po/PO-FAKE", headers=auth_headers)
        assert resp.status_code == 404

    def test_lookup_po_closed(self, client, auth_headers):
        # Close the PO directly in the DB, then try to look it up
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("UPDATE purchase_orders SET status = 'CLOSED' WHERE po_id = 1")
        cur.close()

        resp = client.get("/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 400
        assert "closed" in resp.get_json()["error"].lower()


class TestReceiveItems:
    def test_receive_items_success(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 10, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["receipt_ids"]) == 1
        assert data["po_status"] in ("PARTIAL", "RECEIVED")

    def test_receive_updates_inventory(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        client.post("/api/receiving/receive", json=payload, headers=auth_headers)

        row = _query_one(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = 1 AND bin_id = %s",
            (seed_data["staging_bin_id"],),
        )
        assert row is not None, "Inventory row should exist in staging bin"
        assert row[0] == 5

    def test_receive_partial_updates_po_status(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 10, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.get_json()["po_status"] == "PARTIAL"

    def test_receive_all_items_completes_po(self, client, auth_headers, seed_data):
        bid = seed_data["staging_bin_id"]
        # Receive all 10 PO-2026-001 lines fully
        payload = {
            "po_id": 1,
            "items": [
                {"item_id": 1, "quantity": 100, "bin_id": bid},
                {"item_id": 2, "quantity": 100, "bin_id": bid},
                {"item_id": 3, "quantity": 100, "bin_id": bid},
                {"item_id": 4, "quantity": 100, "bin_id": bid},
                {"item_id": 5, "quantity": 50, "bin_id": bid},
                {"item_id": 6, "quantity": 20, "bin_id": bid},
                {"item_id": 7, "quantity": 200, "bin_id": bid},
                {"item_id": 8, "quantity": 30, "bin_id": bid},
                {"item_id": 9, "quantity": 40, "bin_id": bid},
                {"item_id": 10, "quantity": 60, "bin_id": bid},
            ],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.get_json()["po_status"] == "RECEIVED"

    def test_receive_creates_audit_log(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        client.post("/api/receiving/receive", json=payload, headers=auth_headers)

        row = _query_one(
            "SELECT details FROM audit_log "
            "WHERE action_type = 'RECEIVE' AND entity_id = 1 "
            "ORDER BY log_id DESC LIMIT 1"
        )
        assert row is not None, "Audit log entry should exist for receive action"
        details = row[0]
        assert details["quantity"] == 5
        assert details["quantity_received_before"] == 0
        assert details["quantity_ordered"] >= 5

    def test_receive_invalid_po(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 9999,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 404

    def test_receive_invalid_item(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 11, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "not on PO" in resp.get_json()["error"]

    def test_receive_zero_quantity(self, client, auth_headers, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 0, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400

    def test_receive_over_receipt_blocked_by_default(self, client, auth_headers, seed_data):
        # PO line 1 has 100 ordered. Receiving 110 is blocked unless allow_over_receipt=true.
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 110, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400
        assert "Over-receipt" in resp.get_json()["error"]

    def test_receive_missing_body(self, client, auth_headers):
        resp = client.post("/api/receiving/receive", json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_receive_requires_auth(self, client, seed_data):
        payload = {
            "po_id": 1,
            "items": [{"item_id": 1, "quantity": 5, "bin_id": seed_data["staging_bin_id"]}],
        }
        resp = client.post("/api/receiving/receive", json=payload)
        assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Batched receiving: multiple distinct items in one request
# ══════════════════════════════════════════════════════════════════════════════


class TestBatchedReceive:
    """One /receive request carrying several distinct items must update
    every line, land inventory for each, write one audit row + emit one
    event per item, and return the receipt_ids in submission order. A
    batch where any line is rejected (e.g. over-receipt with over-receipt
    disabled) must roll back atomically -- no receipts, no line changes."""

    def test_batch_receive_multiple_items(self, client, auth_headers, seed_data):
        bid = seed_data["staging_bin_id"]
        # Three distinct lines on PO-2026-001 (items 1/2/3 each ordered
        # 100), partial quantities so the PO stays PARTIAL.
        payload = {
            "po_id": 1,
            "items": [
                {"item_id": 1, "quantity": 10, "bin_id": bid},
                {"item_id": 2, "quantity": 20, "bin_id": bid},
                {"item_id": 3, "quantity": 30, "bin_id": bid},
            ],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()

        # One receipt per submitted item, in submission order, distinct.
        receipt_ids = data["receipt_ids"]
        assert len(receipt_ids) == 3
        assert len(set(receipt_ids)) == 3
        assert receipt_ids == sorted(receipt_ids), (
            "receipt_ids should preserve per-item insertion order"
        )
        assert data["po_status"] == "PARTIAL"

        # All three lines updated.
        for item_id, qty in ((1, 10), (2, 20), (3, 30)):
            received = _query_val(
                "SELECT quantity_received FROM purchase_order_lines "
                "WHERE po_id = 1 AND item_id = %s",
                (item_id,),
            )
            assert received == qty, f"line item {item_id} should show {qty} received"

            # Inventory landed in the staging bin for each item.
            on_hand = _query_val(
                "SELECT quantity_on_hand FROM inventory "
                "WHERE item_id = %s AND bin_id = %s",
                (item_id, bid),
            )
            assert on_hand == qty, f"item {item_id} inventory should be {qty}"

        # Exactly one audit row per item for this receive. The audit
        # details JSON carries receipt_id; count rows whose receipt_id
        # matches one we just created.
        audit_count = _query_val(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action_type = 'RECEIVE' AND entity_id = 1 "
            "  AND (details->>'receipt_id')::int = ANY(%s)",
            (receipt_ids,),
        )
        assert audit_count == 3, "one audit row per received item"

        # Exactly one receipt.completed event per receipt.
        event_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            "WHERE event_type = 'receipt.completed' AND aggregate_id = ANY(%s)",
            (receipt_ids,),
        )
        assert event_count == 3, "one receipt.completed event per receipt"

    def test_batch_over_receipt_rolls_back_atomically(self, client, auth_headers, seed_data):
        bid = seed_data["staging_bin_id"]
        # Snapshot pre-call line state for items 1, 2, 3.
        before = {}
        for item_id in (1, 2, 3):
            before[item_id] = _query_val(
                "SELECT quantity_received FROM purchase_order_lines "
                "WHERE po_id = 1 AND item_id = %s",
                (item_id,),
            )

        # Item 1 over-receives (ordered 100, request 150) with
        # allow_over_receipt unset -> the handler returns 400 inside the
        # loop without committing, so the whole batch is rejected. Items
        # 2 and 3 are later in the batch and are never reached; none of
        # the three lines may change and no receipts may land.
        payload = {
            "po_id": 1,
            "items": [
                {"item_id": 1, "quantity": 150, "bin_id": bid},
                {"item_id": 2, "quantity": 20, "bin_id": bid},
                {"item_id": 3, "quantity": 30, "bin_id": bid},
            ],
        }
        resp = client.post("/api/receiving/receive", json=payload, headers=auth_headers)
        assert resp.status_code == 400, resp.get_json()
        assert "Over-receipt" in resp.get_json()["error"]

        # Line quantities unchanged for every item in the batch.
        for item_id in (1, 2, 3):
            after = _query_val(
                "SELECT quantity_received FROM purchase_order_lines "
                "WHERE po_id = 1 AND item_id = %s",
                (item_id,),
            )
            assert after == before[item_id], (
                f"line item {item_id} must be unchanged after a rejected batch"
            )

        # No receipts landed for any item in this PO from this request.
        receipt_count = _query_val(
            "SELECT COUNT(*) FROM item_receipts WHERE po_id = 1"
        )
        assert receipt_count == 0, "no receipt should survive a rolled-back batch"


# ══════════════════════════════════════════════════════════════════════════════
# Bug #2: Cancel receiving should discard ALL progress
# ══════════════════════════════════════════════════════════════════════════════


class TestCancelReceiving:
    """Bug #2: Cancel receiving session should reverse all receipts."""

    def test_cancel_reverses_receipts(self, client, auth_headers, seed_data):
        """Cancelling should undo inventory additions and PO line updates."""
        po_id = seed_data["po_id"]
        bin_id = seed_data["staging_bin_id"]

        # Get initial state of PO line
        resp = client.get(f"/api/receiving/po/PO-2026-001", headers=auth_headers)
        assert resp.status_code == 200
        initial_lines = resp.get_json()["lines"]
        initial_received = initial_lines[0]["quantity_received"]
        item_id = initial_lines[0]["item_id"]

        # Receive some items
        resp = client.post("/api/receiving/receive", json={
            "po_id": po_id,
            "items": [{"item_id": item_id, "quantity": 5, "bin_id": bin_id}],
            "warehouse_id": seed_data["warehouse_id"],
        }, headers=auth_headers)
        assert resp.status_code == 200
        receipt_ids = resp.get_json()["receipt_ids"]
        assert len(receipt_ids) == 1

        # Verify qty increased
        resp = client.get(f"/api/receiving/po/PO-2026-001", headers=auth_headers)
        after_receive = resp.get_json()["lines"]
        item_line = [l for l in after_receive if l["item_id"] == item_id][0]
        assert item_line["quantity_received"] == initial_received + 5

        # Cancel
        resp = client.post("/api/receiving/cancel", json={
            "receipt_ids": receipt_ids,
            "po_id": po_id,
            "warehouse_id": seed_data["warehouse_id"],
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["reversed"] == 1

        # Verify qty reverted
        resp = client.get(f"/api/receiving/po/PO-2026-001", headers=auth_headers)
        after_cancel = resp.get_json()["lines"]
        item_line = [l for l in after_cancel if l["item_id"] == item_id][0]
        assert item_line["quantity_received"] == initial_received

    def test_cancel_empty_list(self, client, auth_headers):
        """Cancelling with no receipt_ids should return 200."""
        resp = client.post("/api/receiving/cancel", json={
            "po_id": 1,
            "receipt_ids": [],
        }, headers=auth_headers)
        assert resp.status_code == 200

    def test_cancel_requires_po_id(self, client, auth_headers):
        """The route must refuse a cancel without po_id so the cross-PO
        guard always has a target to validate against."""
        resp = client.post("/api/receiving/cancel", json={
            "receipt_ids": [1, 2, 3],
        }, headers=auth_headers)
        assert resp.status_code in (400, 422)
