from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _query_one(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


class TestCreateCycleCount:
    def test_create_cycle_count(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["counts"]) == 1
        assert data["counts"][0]["bin_code"] == "A-01-01"
        assert data["counts"][0]["lines"] >= 1, "Should have lines for items in bin"

    def test_create_count_multiple_bins(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3, 4, 5]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["counts"]) == 3

    def test_create_count_empty_bin(self, client, auth_headers):
        # Bin 16 (QC-01) has no inventory in seed data
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [16]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["counts"][0]["lines"] == 0

    def test_create_count_invalid_warehouse(self, client, auth_headers):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 9999, "bin_ids": [3]},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestGetCycleCount:
    def test_get_count_details(self, client, auth_headers):
        # Create a count first
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        resp = client.get(f"/api/inventory/cycle-count/{count_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cycle_count"]["status"] == "PENDING"
        assert len(data["lines"]) >= 1
        # Bin 3 (A-01-01) has item 1 (qty 50) and item 11 (qty 12)
        for line in data["lines"]:
            assert line["expected_quantity"] > 0

    def test_get_count_not_found(self, client, auth_headers):
        resp = client.get("/api/inventory/cycle-count/9999", headers=auth_headers)
        assert resp.status_code == 404


class TestSubmitCycleCount:
    def _create_count_for_bin(self, client, auth_headers, bin_id=3):
        """Create a cycle count and return count_id and lines."""
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [bin_id]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        detail_resp = client.get(
            f"/api/inventory/cycle-count/{count_id}", headers=auth_headers
        )
        return count_id, detail_resp.get_json()["lines"]

    def test_submit_count_no_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        # Submit exact expected quantities
        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "COMPLETED"
        assert data["summary"]["lines_with_variance"] == 0

    def test_submit_count_with_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        # Submit different quantities
        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"] + 5}
            for l in lines
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "VARIANCE"
        assert data["summary"]["lines_with_variance"] > 0
        assert len(data["summary"]["adjustments"]) > 0

    def test_submit_count_negative_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        line = lines[0]
        original_qty = line["expected_quantity"]

        submit_lines = [
            {"count_line_id": line["count_line_id"], "counted_quantity": original_qty - 3}
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        data = resp.get_json()
        adj = data["summary"]["adjustments"][0]
        assert adj["variance"] == -3

        # Verify inventory was NOT changed (pending audit)
        new_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = 3",
            (line["item_id"],),
        )
        assert new_qty == original_qty

        # Verify pending adjustment record exists
        adj_status = _query_val(
            "SELECT status FROM inventory_adjustments WHERE adjustment_id = %s",
            (adj["adjustment_id"],),
        )
        assert adj_status == "PENDING"

    def test_submit_count_positive_variance(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        line = lines[0]
        original_qty = line["expected_quantity"]

        submit_lines = [
            {"count_line_id": line["count_line_id"], "counted_quantity": original_qty + 10}
        ]

        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        data = resp.get_json()
        adj = data["summary"]["adjustments"][0]
        assert adj["variance"] == 10

        # Verify inventory was NOT changed (pending audit)
        new_qty = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = 3",
            (line["item_id"],),
        )
        assert new_qty == original_qty

        # Verify pending adjustment record exists
        adj_status = _query_val(
            "SELECT status FROM inventory_adjustments WHERE adjustment_id = %s",
            (adj["adjustment_id"],),
        )
        assert adj_status == "PENDING"

    def test_submit_count_updates_last_counted_at(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        line = lines[0]

        submit_lines = [
            {"count_line_id": line["count_line_id"], "counted_quantity": line["expected_quantity"]}
        ]
        client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )

        last_counted = _query_val(
            "SELECT last_counted_at FROM inventory WHERE item_id = %s AND bin_id = 3",
            (line["item_id"],),
        )
        assert last_counted is not None, "last_counted_at should be set"

    def test_submit_count_creates_audit_log(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ]
        client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )

        row = _query_one("SELECT log_id FROM audit_log WHERE action_type = 'COUNT'")
        assert row is not None

    def test_submit_count_already_completed(self, client, auth_headers):
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)

        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ]

        # Submit once
        client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )

        # Submit again
        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_submit_dedupes_duplicate_unexpected_in_payload(self, client, auth_headers):
        # The proven prod bug: the same unexpected item arrives twice in one
        # submit. It must yield exactly one line and one pending adjustment,
        # not two (two would double-count inventory on approval).
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        snapshot_ids = tuple(l["item_id"] for l in lines) or (0,)
        unexpected_item = _query_val(
            "SELECT item_id FROM items WHERE item_id NOT IN %s LIMIT 1",
            (snapshot_ids,),
        )
        assert unexpected_item is not None

        submit_lines = [
            {"unexpected": True, "item_id": unexpected_item, "counted_quantity": 1},
            {"unexpected": True, "item_id": unexpected_item, "counted_quantity": 1},
        ]
        resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        line_n = _query_val(
            "SELECT COUNT(*) FROM cycle_count_lines WHERE count_id = %s AND item_id = %s",
            (count_id, unexpected_item),
        )
        assert line_n == 1
        adj_n = _query_val(
            "SELECT COUNT(*) FROM inventory_adjustments WHERE cycle_count_id = %s AND item_id = %s",
            (count_id, unexpected_item),
        )
        assert adj_n == 1

    def test_resubmit_does_not_duplicate_unexpected_lines(self, client, auth_headers):
        # A double-submit (e.g. a double-tapped button) must not duplicate the
        # unexpected lines: the second submit is rejected by the status gate
        # (the count row is locked while the first runs), leaving one line.
        count_id, lines = self._create_count_for_bin(client, auth_headers, bin_id=3)
        snapshot_ids = tuple(l["item_id"] for l in lines) or (0,)
        unexpected_item = _query_val(
            "SELECT item_id FROM items WHERE item_id NOT IN %s LIMIT 1",
            (snapshot_ids,),
        )
        submit_lines = [
            {"count_line_id": l["count_line_id"], "counted_quantity": l["expected_quantity"]}
            for l in lines
        ] + [{"unexpected": True, "item_id": unexpected_item, "counted_quantity": 2}]

        r1 = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        assert r2.status_code == 400

        line_n = _query_val(
            "SELECT COUNT(*) FROM cycle_count_lines WHERE count_id = %s AND item_id = %s",
            (count_id, unexpected_item),
        )
        assert line_n == 1

    def test_create_aggregates_inventory_rows_for_same_item(self, client, auth_headers):
        # A bin holding two inventory rows for one item (different lots) must
        # produce ONE cycle_count_line with the summed quantity, so the
        # (count_id, item_id) invariant holds with the correct expected qty.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT item_id FROM items ORDER BY item_id LIMIT 1")
        item_id = cur.fetchone()[0]
        cur.execute("SELECT bin_id FROM bins WHERE warehouse_id = 1 ORDER BY bin_id DESC LIMIT 1")
        bin_id = cur.fetchone()[0]
        cur.execute("DELETE FROM inventory WHERE bin_id = %s", (bin_id,))
        cur.execute(
            "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, lot_number) "
            "VALUES (%s, %s, 1, 7, 'LOT-A'), (%s, %s, 1, 5, 'LOT-B')",
            (item_id, bin_id, item_id, bin_id),
        )
        cur.close()

        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [bin_id]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        count_id = resp.get_json()["counts"][0]["count_id"]
        detail = client.get(
            f"/api/inventory/cycle-count/{count_id}", headers=auth_headers
        ).get_json()
        item_lines = [l for l in detail["lines"] if l["item_id"] == item_id]
        assert len(item_lines) == 1
        assert item_lines[0]["expected_quantity"] == 12

    def test_unique_constraint_present(self, client, auth_headers):
        # The (count_id, item_id) uniqueness backstop must exist in the schema.
        exists = _query_val(
            "SELECT 1 FROM pg_constraint WHERE conname = 'uq_cycle_count_lines_count_item'"
        )
        assert exists == 1

    def test_inventory_requires_auth(self, client):
        resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
        )
        assert resp.status_code == 401


class TestAdjustmentSelfApproval:
    """M3: self-approval check on cycle count adjustments."""

    def _create_variance(self, client, auth_headers):
        """Create a cycle count with variance, returning the adjustment_id."""
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        detail_resp = client.get(
            f"/api/inventory/cycle-count/{count_id}", headers=auth_headers
        )
        lines = detail_resp.get_json()["lines"]

        submit_lines = [
            {"count_line_id": lines[0]["count_line_id"], "counted_quantity": lines[0]["expected_quantity"] + 5}
        ]
        submit_resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={"count_id": count_id, "lines": submit_lines},
            headers=auth_headers,
        )
        adj = submit_resp.get_json()["summary"]["adjustments"][0]
        return adj["adjustment_id"]

    def test_self_approval_allowed_when_setting_off(self, client, auth_headers):
        """Default (setting absent/false): same user can approve their own count."""
        adj_id = self._create_variance(client, auth_headers)

        resp = client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj_id, "action": "approve"}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["approved"] == 1

    def test_self_approval_logs_audit_when_setting_off(self, client, auth_headers):
        """Self-approval without separation logs SELF_APPROVED_COUNT."""
        adj_id = self._create_variance(client, auth_headers)

        client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj_id, "action": "approve"}]},
            headers=auth_headers,
        )

        row = _query_one(
            "SELECT action_type FROM audit_log WHERE action_type = 'SELF_APPROVED_COUNT' AND entity_id = %s",
            (adj_id,),
        )
        assert row is not None

    def test_self_approval_blocked_when_setting_on(self, client, auth_headers):
        """When require_count_approval_separation is true, self-approval returns 403."""
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('require_count_approval_separation', 'true') "
            "ON CONFLICT (key) DO UPDATE SET value = 'true'"
        )
        cur.close()

        adj_id = self._create_variance(client, auth_headers)

        resp = client.post(
            "/api/admin/adjustments/review",
            json={"decisions": [{"adjustment_id": adj_id, "action": "approve"}]},
            headers=auth_headers,
        )
        assert resp.status_code == 403
        assert "Cannot approve your own cycle count" in resp.get_json()["error"]


class TestPendingAdjustmentsList:
    """Approval screen feed: /admin/adjustments/pending must carry the
    expected / counted / variance figures the operator approves against."""

    def _create_variance(self, client, auth_headers, delta=5):
        create_resp = client.post(
            "/api/inventory/cycle-count/create",
            json={"warehouse_id": 1, "bin_ids": [3]},
            headers=auth_headers,
        )
        count_id = create_resp.get_json()["counts"][0]["count_id"]

        detail_resp = client.get(
            f"/api/inventory/cycle-count/{count_id}", headers=auth_headers
        )
        line = detail_resp.get_json()["lines"][0]
        expected = line["expected_quantity"]

        submit_resp = client.post(
            "/api/inventory/cycle-count/submit",
            json={
                "count_id": count_id,
                "lines": [
                    {"count_line_id": line["count_line_id"], "counted_quantity": expected + delta}
                ],
            },
            headers=auth_headers,
        )
        adj = submit_resp.get_json()["summary"]["adjustments"][0]
        return adj["adjustment_id"], expected, expected + delta

    def test_pending_carries_expected_counted_variance(self, client, auth_headers):
        adj_id, expected, counted = self._create_variance(client, auth_headers, delta=5)

        resp = client.get("/api/admin/adjustments/pending", headers=auth_headers)
        assert resp.status_code == 200
        rows = resp.get_json()["adjustments"]

        match = next((a for a in rows if a["adjustment_id"] == adj_id), None)
        assert match is not None, "submitted variance should appear in the pending queue"
        assert match["expected_quantity"] == expected
        assert match["counted_quantity"] == counted
        # Variance the operator sees == counted - expected == quantity_change.
        assert match["counted_quantity"] - match["expected_quantity"] == match["quantity_change"]
        assert match["quantity_change"] == 5
