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


def _create_batch(client, auth_headers, so_ids=None):
    """Create a pick batch for the given SOs (default: SO-2026-001 and SO-2026-002)."""
    identifiers = so_ids or ["SO-2026-001", "SO-2026-002"]
    resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": identifiers, "warehouse_id": 1},
        headers=auth_headers,
    )
    return resp


class TestCreateBatch:
    def test_create_batch_success(self, client, auth_headers):
        resp = _create_batch(client, auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["batch_id"] is not None
        assert data["batch_number"].startswith("BATCH-")
        assert data["total_orders"] == 2
        assert len(data["tasks"]) > 0

    def test_create_batch_pick_path_order(self, client, auth_headers):
        resp = _create_batch(client, auth_headers)
        data = resp.get_json()
        sequences = [t["pick_sequence"] for t in data["tasks"]]
        assert sequences == sorted(sequences), "Tasks should be sorted by pick_sequence"

    def test_create_batch_allocates_inventory(self, client, auth_headers):
        _create_batch(client, auth_headers)
        # Item 1 (TST-001) in bin 3 should have quantity_allocated > 0
        row = _query_one(
            "SELECT quantity_allocated FROM inventory WHERE item_id = 1 AND bin_id = 3"
        )
        assert row[0] > 0, "Inventory should be allocated after batch creation"

    def test_create_batch_keeps_so_open_and_links_batch(self, client, auth_headers):
        _create_batch(client, auth_headers)
        # PICKING was retired in mig 060; the SO stays OPEN and "in
        # picking" is derived from a pick_batch_orders row against an
        # active batch.
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "OPEN"
        in_batch = _query_val(
            """
            SELECT COUNT(*) FROM pick_batch_orders pbo
              JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
             WHERE pbo.so_id = 1 AND pb.status IN ('OPEN', 'IN_PROGRESS')
            """
        )
        assert in_batch == 1

    def test_create_batch_assigns_totes(self, client, auth_headers):
        resp = _create_batch(client, auth_headers)
        data = resp.get_json()
        totes = [o["tote_number"] for o in data["orders"]]
        assert "TOTE-1" in totes
        assert "TOTE-2" in totes

    def test_create_batch_invalid_so(self, client, auth_headers):
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-FAKE"], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_batch_same_user_rescan_auto_cancels_prior(self, client, auth_headers):
        """No-Resume rule: when the same operator re-scans, any prior
        IN_PROGRESS / OPEN batch they own is auto-cancelled with full
        revert before the new batch is created. The historical "already
        in active pick batch" rejection only applies cross-user;
        same-user re-scan is a fresh session, not a conflict."""
        first = _create_batch(client, auth_headers).get_json()
        first_batch_id = first["batch_id"]
        second = _create_batch(client, auth_headers)
        assert second.status_code == 200
        second_batch_id = second.get_json()["batch_id"]
        # The new batch is distinct from the prior one.
        assert second_batch_id != first_batch_id
        # The prior batch was cancelled by the auto-cancel pass.
        first_status = _query_val(
            "SELECT status FROM pick_batches WHERE batch_id = %s",
            (first_batch_id,),
        )
        assert first_status == "CANCELLED"
        # And the new batch is OPEN with real pick_tasks.
        assert second.get_json()["total_items"] > 0


class TestGetBatch:
    def test_get_batch_returns_tasks_in_order(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        sequences = [t["pick_sequence"] for t in data["tasks"]]
        assert sequences == sorted(sequences)

    def test_get_batch_not_found(self, client, auth_headers):
        resp = client.get("/api/picking/batch/9999", headers=auth_headers)
        assert resp.status_code == 404


class TestNextTask:
    def test_get_next_task(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert "pick_task_id" in data
        assert data["status"] == "PENDING"


class TestConfirmPick:
    def test_confirm_pick_success(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["task"]["status"] == "PICKED"

    def test_confirm_pick_wrong_barcode(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": "WRONG-BARCODE",
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Wrong item" in resp.get_json()["error"]

    def test_confirm_pick_already_picked(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        # Pick it once
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )

        # Try to pick again
        resp = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "already" in resp.get_json()["error"].lower()

    def test_confirm_pick_audit_details_carry_expected_and_picked(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]
        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )

        details = _query_val(
            "SELECT details FROM audit_log "
            "WHERE action_type = 'PICK' "
            "  AND (details->>'pick_task_id')::int = %s",
            (task["pick_task_id"],),
        )
        assert details is not None
        assert details["quantity_to_pick"] == task["quantity_to_pick"]
        assert details["quantity_picked"] == task["quantity_to_pick"]
        assert details["sku"] == task["sku"]

    def test_confirm_pick_updates_so_line(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )

        # Check so_line quantity_picked increased
        so_line_id = _query_val(
            "SELECT so_line_id FROM pick_tasks WHERE pick_task_id = %s",
            (task["pick_task_id"],),
        )
        qty_picked = _query_val(
            "SELECT quantity_picked FROM sales_order_lines WHERE so_line_id = %s",
            (so_line_id,),
        )
        assert qty_picked > 0


class TestShortPick:
    def test_short_pick_success(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/short",
            json={"pick_task_id": task["pick_task_id"], "quantity_available": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["task"]["status"] == "SHORT"
        assert data["task"]["shortage"] > 0

    def test_short_pick_zero_available(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()

        resp = client.post(
            "/api/picking/short",
            json={"pick_task_id": task["pick_task_id"], "quantity_available": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["task"]["quantity_picked"] == 0


class TestCompleteBatch:
    def _pick_all_tasks(self, client, auth_headers, batch_id):
        """Pick or short all pending tasks in a batch."""
        while True:
            next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
            data = next_resp.get_json()
            if "message" in data:
                break
            client.post(
                "/api/picking/confirm",
                json={
                    "pick_task_id": data["pick_task_id"],
                    "scanned_barcode": data["upc"],
                    "quantity_picked": data["quantity_to_pick"],
                },
                headers=auth_headers,
            )

    def test_complete_batch_success(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["batch_number"] is not None

        # Check SOs moved to PICKED
        status = _query_val("SELECT status FROM sales_orders WHERE so_id = 1")
        assert status == "PICKED"

    def test_complete_batch_with_pending_tasks(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        # Don't pick any tasks, try to complete
        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "pending" in resp.get_json()["error"].lower()

    def test_complete_batch_not_found(self, client, auth_headers):
        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": 9999},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_get_next_task_all_complete(self, client, auth_headers):
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        assert resp.status_code == 200
        assert "message" in resp.get_json()
        assert "complete" in resp.get_json()["message"].lower()

    def test_picking_requires_auth(self, client):
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-2026-001"], "warehouse_id": 1},
        )
        assert resp.status_code == 401

    def test_complete_batch_refuses_silently_under_picked(self, client, auth_headers):
        """Layer 2A: refuses to complete a batch when any line has
        quantity_picked < quantity_ordered without an explicit short-close
        marker. Simulates the historical under-allocation bug state via
        direct DB mutation; the new Layer 1 pre-flight makes this state
        unreachable through the API, but the guard must still catch it
        defensively (e.g., manual DB intervention, future bugs)."""
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]
        self._pick_all_tasks(client, auth_headers, batch_id)

        # Knock one line back to picked=0 without leaving a SHORT marker.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_order_lines SET quantity_picked = 0 "
            "WHERE so_line_id = (SELECT MIN(so_line_id) FROM sales_order_lines WHERE so_id = 1)"
        )
        cur.close()

        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        err = resp.get_json()["error"].lower()
        assert "under-picked" in err or "short-close marker" in err

    def test_complete_batch_allows_legitimate_short(self, client, auth_headers):
        """Layer 2A: explicit SHORT picks (via /api/picking/short) are a
        legitimate close-out path and must still allow batch completion."""
        create_resp = _create_batch(client, auth_headers)
        batch_id = create_resp.get_json()["batch_id"]

        # Short the first pending task; pick the rest.
        first = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers).get_json()
        client.post(
            "/api/picking/short",
            json={"pick_task_id": first["pick_task_id"], "quantity_available": 0},
            headers=auth_headers,
        )
        # Pick remaining tasks normally
        while True:
            nx = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers).get_json()
            if "message" in nx:
                break
            client.post(
                "/api/picking/confirm",
                json={
                    "pick_task_id": nx["pick_task_id"],
                    "scanned_barcode": nx["upc"],
                    "quantity_picked": nx["quantity_to_pick"],
                },
                headers=auth_headers,
            )

        resp = client.post(
            "/api/picking/complete-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200


class TestInsufficientCoverage:
    """Layer 1: refuses to create a batch when any SO can't be fully
    allocated from pickable inventory; second call with exclude_so_ids
    drops those SOs and commits the remainder."""

    def _create_so_with_unmet_demand(self, so_number, item_id, qty):
        """Insert an OPEN SO whose single line orders more units of `item_id`
        than the warehouse has in pickable bins. Returns the new so_id."""
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by, external_id)
               VALUES (%s, %s, %s, 'OPEN', 1, 'admin', gen_random_uuid()) RETURNING so_id""",
            (so_number, so_number, "Coverage Test Customer"),
        )
        so_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (%s, %s, %s, 1)",
            (so_id, item_id, qty),
        )
        cur.close()
        return so_id

    def test_create_batch_409_when_inventory_insufficient(self, client, auth_headers):
        so_id = self._create_so_with_unmet_demand("SO-SHORT-1", item_id=5, qty=9999)
        resp = client.post(
            "/api/picking/create-batch",
            json={"so_identifiers": ["SO-SHORT-1"], "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["error_type"] == "insufficient_coverage"
        assert any(e["so_id"] == so_id for e in data["unpickable"])

    def test_create_batch_exclude_drops_unpickable(self, client, auth_headers):
        unpickable_so = self._create_so_with_unmet_demand("SO-SHORT-2", item_id=5, qty=9999)
        resp = client.post(
            "/api/picking/create-batch",
            json={
                "so_identifiers": ["SO-2026-001", "SO-SHORT-2"],
                "warehouse_id": 1,
                "exclude_so_ids": [unpickable_so],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        # Only the pickable seed SO survives in the committed batch.
        assert data["total_orders"] == 1
        assert all(o["so_number"] == "SO-2026-001" for o in data["orders"])


# --- Zone/Aisle Conditional Display Tests ---


def test_next_pick_with_zone_and_aisle(client, auth_headers):
    """Bin with zone and aisle returns both in response."""
    batch = _create_batch(client, auth_headers).get_json()
    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] is not None
    # Seed bins have zones (Storage Shelves) and aisles (A, B)
    assert data["zone"] is not None
    assert data["aisle"] is not None


def test_next_pick_with_zone_no_aisle(client, auth_headers):
    """Bin with zone but no aisle returns zone and aisle=null."""
    # The staging bin (RCV-01) has zone (Receiving Area) but no aisle
    # Create an SO that needs an item in a staging-like location
    conn = get_raw_connection()
    cur = conn.cursor()
    # Put some item 1 inventory in the staging bin (bin_id=1, zone=Receiving, no aisle)
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES (1, 1, 1, 50)"
    )
    # Remove item 1 from all non-staging bins so it must pick from staging
    cur.execute("DELETE FROM inventory WHERE item_id = 1 AND bin_id != 1")
    # Change staging bin type so it's pickable
    cur.execute("UPDATE bins SET bin_type = 'Pickable' WHERE bin_id = 1")
    cur.close()

    # Create SO for item 1
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by, external_id)
           VALUES ('SO-NOAISLE', 'SO-NOAISLE', 'Cust', 'OPEN', 1, 'admin', gen_random_uuid()) RETURNING so_id"""
    )
    so_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (%s, 1, 1, 1)",
        (so_id,),
    )
    cur.close()

    batch_resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": ["SO-NOAISLE"], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch = batch_resp.get_json()

    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] == "RECV-01"
    assert data["zone"] == "Receiving Area"
    assert data["aisle"] is None


def test_next_pick_no_zone(client, auth_headers):
    """Bin with no zone returns zone=null and aisle=null."""
    conn = get_raw_connection()
    cur = conn.cursor()
    # Create a bin with no zone (zone_id references are NOT NULL in schema,
    # so we create a bin in an existing zone then set zone_id via direct update)
    # Actually zone_id is NOT NULL, so we need to create the bin properly
    # and then check. The schema requires zone_id, so a "no zone" bin
    # would need a schema change. Instead, test that zone_name=null
    # when the zone record is somehow missing. We can simulate by
    # creating a bin with a zone that has no name... or we can check
    # that the LEFT JOIN handles it.
    # Since zone_id is NOT NULL in schema, we test the null-coercion
    # on empty strings: create a zone with empty name to verify `or None`.
    cur.execute(
        """INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type)
           VALUES (1, 'NONAME', '', 'STORAGE') RETURNING zone_id"""
    )
    zone_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, bin_type, pick_sequence, putaway_sequence, external_id)
           VALUES (%s, 1, 'NOZONE-01', 'BIN-NOZONE-01', 'Pickable', 50, 50, gen_random_uuid()) RETURNING bin_id""",
        (zone_id,),
    )
    bin_id = cur.fetchone()[0]
    # Put inventory in this bin
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) VALUES (1, %s, 1, 100)",
        (bin_id,),
    )
    # Remove item 1 from all other bins
    cur.execute("DELETE FROM inventory WHERE item_id = 1 AND bin_id != %s", (bin_id,))
    cur.close()

    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by, external_id)
           VALUES ('SO-NOZONE', 'SO-NOZONE', 'Cust', 'OPEN', 1, 'admin', gen_random_uuid()) RETURNING so_id"""
    )
    so_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (%s, 1, 1, 1)",
        (so_id,),
    )
    cur.close()

    batch_resp = client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": ["SO-NOZONE"], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch = batch_resp.get_json()

    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] == "NOZONE-01"
    # Empty string zone_name should be coerced to null
    assert data["zone"] is None
    assert data["aisle"] is None


def test_next_pick_bin_code_always_present(client, auth_headers):
    """bin_code is never null regardless of zone/aisle state."""
    batch = _create_batch(client, auth_headers).get_json()
    resp = client.get(f"/api/picking/batch/{batch['batch_id']}/next", headers=auth_headers)
    data = resp.get_json()
    assert data["bin_code"] is not None
    assert isinstance(data["bin_code"], str)
    assert len(data["bin_code"]) > 0


class TestCancelBatch:
    """hotfix/picking-revert-allocation: cancel = wipe progress. The
    SO must end up indistinguishable from "never started picking" so
    a re-scan re-allocates cleanly. PENDING reservations are freed on
    both inventory.quantity_allocated and sol.quantity_allocated;
    PICKED units go back to their source bin; SHORT residue is
    likewise unwound. The batch flips to CANCELLED."""

    def _line_state(self, sol_id):
        row = _query_one(
            "SELECT quantity_allocated, quantity_picked "
            "  FROM sales_order_lines WHERE so_line_id = %s",
            (sol_id,),
        )
        return {"quantity_allocated": row[0], "quantity_picked": row[1]}

    def test_cancel_all_pending_releases_allocations(self, client, auth_headers):
        """Cancel before any picks fire: inventory.quantity_allocated
        decrements back to 0 AND sol.quantity_allocated decrements
        back to 0 so a re-scan rebuilds the batch cleanly."""
        create = _create_batch(client, auth_headers).get_json()
        batch_id = create["batch_id"]
        # Pre-cancel: SO 1 line 1 should be allocated (qty 2 from seed).
        sol = _query_one(
            "SELECT so_line_id, quantity_allocated FROM sales_order_lines "
            "WHERE so_id = 1 AND line_number = 1"
        )
        sol_id = sol[0]
        assert sol[1] == 2

        resp = client.post(
            "/api/picking/cancel-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["released_tasks"] >= 1

        # Batch is terminal.
        batch_status = _query_val(
            "SELECT status FROM pick_batches WHERE batch_id = %s", (batch_id,)
        )
        assert batch_status == "CANCELLED"
        # sol.quantity_allocated is 0 (the reservation is gone).
        assert self._line_state(sol_id)["quantity_allocated"] == 0
        # Tasks flipped to SKIPPED (terminal).
        pending = _query_val(
            "SELECT COUNT(*) FROM pick_tasks WHERE batch_id = %s AND status = 'PENDING'",
            (batch_id,),
        )
        assert pending == 0
        # And inventory.quantity_allocated returned to 0 (no other
        # batches are reserving it).
        inv_alloc = _query_val(
            "SELECT quantity_allocated FROM inventory WHERE item_id = 1 AND bin_id = 3"
        )
        assert inv_alloc == 0

    def test_cancel_after_pick_restores_inventory(self, client, auth_headers):
        """Pick one task, then cancel. The picked units return to the
        source bin (add_inventory), sol.quantity_picked and
        quantity_allocated both reset, and the task flips to RELEASED."""
        create = _create_batch(client, auth_headers).get_json()
        batch_id = create["batch_id"]
        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        task = next_resp.get_json()
        pre_pick_on_hand = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = %s",
            (1, 3),  # Item 1 lives in bin 3 in the seed.
        )

        confirm = client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )
        assert confirm.status_code == 200
        # Confirm-time inventory drop.
        mid_pick_on_hand = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = %s",
            (1, 3),
        )
        assert mid_pick_on_hand == pre_pick_on_hand - task["quantity_to_pick"]

        resp = client.post(
            "/api/picking/cancel-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # On-hand restored.
        post_cancel_on_hand = _query_val(
            "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = %s",
            (1, 3),
        )
        assert post_cancel_on_hand == pre_pick_on_hand
        # Picked task flipped to RELEASED (mirrors the revert flow).
        pt_status = _query_val(
            "SELECT status FROM pick_tasks WHERE pick_task_id = %s",
            (task["pick_task_id"],),
        )
        assert pt_status == "RELEASED"
        # The SOL that owned this pick task is back to zero on both
        # counters.
        sol_id = _query_val(
            "SELECT so_line_id FROM pick_tasks WHERE pick_task_id = %s",
            (task["pick_task_id"],),
        )
        state = self._line_state(sol_id)
        assert state["quantity_picked"] == 0
        assert state["quantity_allocated"] == 0

    def test_cancel_already_cancelled_is_noop(self, client, auth_headers):
        """Idempotent re-cancel returns 200 without unwinding anything
        twice (released_tasks reports 0). Inventory and line state
        from the first cancel are preserved."""
        create = _create_batch(client, auth_headers).get_json()
        batch_id = create["batch_id"]
        first = client.post(
            "/api/picking/cancel-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert first.status_code == 200
        sol_id = _query_val(
            "SELECT so_line_id FROM sales_order_lines "
            "WHERE so_id = 1 AND line_number = 1"
        )
        state_before = self._line_state(sol_id)

        second = client.post(
            "/api/picking/cancel-batch",
            json={"batch_id": batch_id},
            headers=auth_headers,
        )
        assert second.status_code == 200
        assert second.get_json()["released_tasks"] == 0
        # State unchanged.
        assert self._line_state(sol_id) == state_before

    def test_new_scan_after_partial_pick_auto_cancels_prior(self, client, auth_headers):
        """End-to-end no-Resume regression: operator scans, picks one
        task, walks away (does not complete), then re-scans the same
        SOs. The prior batch must auto-cancel with full revert: the
        picked unit returns to its source bin, sol.quantity_picked
        and quantity_allocated reset, and the new batch starts from
        the clean post-revert state with real pick_tasks again."""
        first = _create_batch(client, auth_headers).get_json()
        first_batch_id = first["batch_id"]
        # Pick one task from the first batch.
        next_resp = client.get(
            f"/api/picking/batch/{first_batch_id}/next", headers=auth_headers
        )
        task = next_resp.get_json()
        pre_pick_on_hand = _query_val(
            "SELECT quantity_on_hand FROM inventory "
            "WHERE item_id = %s AND bin_id = %s",
            (1, 3),
        )
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": task["pick_task_id"],
                "scanned_barcode": task["upc"],
                "quantity_picked": task["quantity_to_pick"],
            },
            headers=auth_headers,
        )

        # Re-scan. The auto-cancel should fire on the prior batch.
        second = _create_batch(client, auth_headers)
        assert second.status_code == 200

        # Prior batch CANCELLED, picked task RELEASED, inventory
        # restored to its pre-pick value.
        prior_status = _query_val(
            "SELECT status FROM pick_batches WHERE batch_id = %s",
            (first_batch_id,),
        )
        assert prior_status == "CANCELLED"
        prior_task_status = _query_val(
            "SELECT status FROM pick_tasks WHERE pick_task_id = %s",
            (task["pick_task_id"],),
        )
        assert prior_task_status == "RELEASED"
        post_scan_on_hand = _query_val(
            "SELECT quantity_on_hand FROM inventory "
            "WHERE item_id = %s AND bin_id = %s",
            (1, 3),
        )
        assert post_scan_on_hand == pre_pick_on_hand
        # The new batch has real pick_tasks again because the line
        # state was fully reverted before the new allocation ran.
        assert second.get_json()["total_items"] > 0

    def test_cancel_clears_active_batch_guard(self, client, auth_headers):
        """After cancel, the active-batch check in create_pick_batch
        sees no IN_PROGRESS/OPEN batch for the SO, so a fresh scan is
        unblocked (vs. the pre-cancel state where the same SOs would
        hit "already in active pick batch"). Validates that the guard
        clears without actually creating a second batch (which would
        collide on batch_number's second-resolution timestamp)."""
        first = _create_batch(client, auth_headers).get_json()
        first_batch_id = first["batch_id"]
        # Pre-cancel: the SOs are inside an active batch, so re-scan
        # would be rejected by the guard.
        active_pre = _query_val(
            """
            SELECT COUNT(*) FROM pick_batch_orders pbo
              JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
             WHERE pbo.so_id = 1 AND pb.status IN ('OPEN', 'IN_PROGRESS')
            """
        )
        assert active_pre == 1

        cancel = client.post(
            "/api/picking/cancel-batch",
            json={"batch_id": first_batch_id},
            headers=auth_headers,
        )
        assert cancel.status_code == 200

        # Post-cancel: no active batch links remain, so a re-scan
        # would pass the guard and successfully allocate.
        active_post = _query_val(
            """
            SELECT COUNT(*) FROM pick_batch_orders pbo
              JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
             WHERE pbo.so_id = 1 AND pb.status IN ('OPEN', 'IN_PROGRESS')
            """
        )
        assert active_post == 0
        # Line is back to needing allocation (quantity_ordered >
        # quantity_allocated), so create_pick_batch's coverage query
        # will pick it up again.
        sol = _query_one(
            "SELECT quantity_ordered, quantity_allocated FROM sales_order_lines "
            "WHERE so_id = 1 AND line_number = 1"
        )
        assert sol[0] > sol[1]
