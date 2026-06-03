"""Tests for the admin Picking Batches surface: list the active pick
batches that hold the cross-pick lock, and release a stuck SO batch.

Delete is SO-only; TO batches are listed read-only and the delete
endpoint refuses them (TO picking has no cross-pick lock and its unwind
lives on the Transfer Orders screen).
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


def _create_so_batch(client, auth_headers, identifiers=None):
    """Create a real SO pick batch through the picker endpoint so
    inventory is genuinely allocated (lets us prove the release frees
    it). Returns the create-batch response JSON."""
    resp = client.post(
        "/api/picking/create-batch",
        json={
            "so_identifiers": identifiers or ["SO-2026-001"],
            "warehouse_id": 1,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _create_to(to_number, source_warehouse_id=1, dest_warehouse_id=2,
               status="OPEN", lines=None):
    if lines is None:
        lines = [(1, 2)]
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO transfer_orders
              (to_number, source_warehouse_id, destination_warehouse_id,
               status, created_by, external_id)
           VALUES (%s, %s, %s, %s, 'admin', gen_random_uuid())
           RETURNING to_id""",
        (to_number, source_warehouse_id, dest_warehouse_id, status),
    )
    to_id = cur.fetchone()[0]
    line_ids = []
    for idx, (item_id, committed_qty) in enumerate(lines, 1):
        cur.execute(
            """INSERT INTO transfer_order_lines
                  (to_id, item_id, line_number, requested_qty,
                   committed_qty, status)
               VALUES (%s, %s, %s, %s, %s, 'PENDING')
               RETURNING to_line_id""",
            (to_id, item_id, idx, committed_qty, committed_qty),
        )
        line_ids.append(cur.fetchone()[0])
    cur.close()
    return to_id, line_ids


def _provision_to_batch(to_id, to_line_ids, assigned_to="admin",
                        warehouse_id=1, bin_id=2):
    """Mirror admin start-picking: a pick_batches row + PENDING pick_tasks
    carrying to_id/to_line_id (so_id NULL)."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO pick_batches
              (batch_number, warehouse_id, status, assigned_to)
           VALUES (%s, %s, 'OPEN', %s) RETURNING batch_id""",
        (f"BATCH-TO-TEST-{to_id}", warehouse_id, assigned_to),
    )
    batch_id = cur.fetchone()[0]
    for seq, to_line_id in enumerate(to_line_ids, 1):
        cur.execute(
            """SELECT item_id, committed_qty FROM transfer_order_lines
                WHERE to_line_id = %s""",
            (to_line_id,),
        )
        item_id, qty = cur.fetchone()
        cur.execute(
            """INSERT INTO pick_tasks
                  (batch_id, to_id, to_line_id, item_id, bin_id,
                   quantity_to_pick, pick_sequence, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')""",
            (batch_id, to_id, to_line_id, item_id, bin_id, qty, seq),
        )
    cur.close()
    return batch_id


def _active_batch_count_for_so(so_number):
    """How many active (OPEN/IN_PROGRESS) batches reference this SO -- i.e.
    is it still locked out of picking."""
    return _query_val(
        """
        SELECT COUNT(*)
          FROM pick_batch_orders pbo
          JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
          JOIN sales_orders so ON so.so_id = pbo.so_id
         WHERE so.so_number = %s
           AND pb.status IN ('OPEN', 'IN_PROGRESS')
        """,
        (so_number,),
    )


# --- List ---

class TestListPickBatches:
    def test_requires_warehouse_id(self, client, auth_headers):
        resp = client.get("/api/admin/pick-batches", headers=auth_headers)
        assert resp.status_code == 400

    def test_requires_auth(self, client):
        resp = client.get("/api/admin/pick-batches?warehouse_id=1")
        assert resp.status_code == 401

    def test_lists_active_so_batch(self, client, auth_headers):
        created = _create_so_batch(client, auth_headers, ["SO-2026-001"])
        resp = client.get(
            "/api/admin/pick-batches?warehouse_id=1", headers=auth_headers
        )
        assert resp.status_code == 200
        batches = resp.get_json()["pick_batches"]
        row = next(b for b in batches if b["batch_id"] == created["batch_id"])
        assert row["kind"] == "SO"
        assert row["status"] == "OPEN"
        assert "SO-2026-001" in row["orders"]
        assert row["to_number"] is None
        assert row["total_tasks"] > 0
        assert row["completed_tasks"] == 0
        assert row["pending_tasks"] == row["total_tasks"]

    def test_lists_to_batch_kind(self, client, auth_headers):
        to_id, line_ids = _create_to("TO-PB-LIST-1")
        batch_id = _provision_to_batch(to_id, line_ids)
        resp = client.get(
            "/api/admin/pick-batches?warehouse_id=1", headers=auth_headers
        )
        assert resp.status_code == 200
        batches = resp.get_json()["pick_batches"]
        row = next(b for b in batches if b["batch_id"] == batch_id)
        assert row["kind"] == "TO"
        assert row["to_number"] == "TO-PB-LIST-1"
        assert row["orders"] == []

    def test_excludes_other_warehouse(self, client, auth_headers):
        created = _create_so_batch(client, auth_headers, ["SO-2026-001"])
        # Warehouse 2 should not see warehouse 1's batch.
        resp = client.get(
            "/api/admin/pick-batches?warehouse_id=2", headers=auth_headers
        )
        ids = [b["batch_id"] for b in resp.get_json()["pick_batches"]]
        assert created["batch_id"] not in ids

    def test_excludes_terminal_batch_after_release(self, client, auth_headers):
        created = _create_so_batch(client, auth_headers, ["SO-2026-001"])
        client.post(
            f"/api/admin/pick-batches/{created['batch_id']}/delete",
            headers=auth_headers,
        )
        resp = client.get(
            "/api/admin/pick-batches?warehouse_id=1", headers=auth_headers
        )
        ids = [b["batch_id"] for b in resp.get_json()["pick_batches"]]
        assert created["batch_id"] not in ids


# --- Delete (release) ---

class TestDeletePickBatch:
    def test_releases_so_batch_and_frees_order(self, client, auth_headers):
        created = _create_so_batch(client, auth_headers, ["SO-2026-001"])
        batch_id = created["batch_id"]
        assert _active_batch_count_for_so("SO-2026-001") == 1

        resp = client.post(
            f"/api/admin/pick-batches/{batch_id}/delete", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["released_tasks"] > 0
        assert "SO-2026-001" in data["released_orders"]

        # Batch is terminal and the SO no longer sits in any active batch,
        # so the cross-pick lock is gone.
        assert _query_val(
            "SELECT status FROM pick_batches WHERE batch_id = %s", (batch_id,)
        ) == "CANCELLED"
        assert _active_batch_count_for_so("SO-2026-001") == 0

    def test_release_unwinds_inventory_allocation(self, client, auth_headers):
        # quantity_allocated booked at create time must return to zero
        # after release (PENDING tasks free their reservation).
        before = _query_val(
            "SELECT COALESCE(SUM(quantity_allocated), 0) FROM inventory "
            "WHERE warehouse_id = 1"
        )
        created = _create_so_batch(client, auth_headers, ["SO-2026-001"])
        during = _query_val(
            "SELECT COALESCE(SUM(quantity_allocated), 0) FROM inventory "
            "WHERE warehouse_id = 1"
        )
        assert during > before
        client.post(
            f"/api/admin/pick-batches/{created['batch_id']}/delete",
            headers=auth_headers,
        )
        after = _query_val(
            "SELECT COALESCE(SUM(quantity_allocated), 0) FROM inventory "
            "WHERE warehouse_id = 1"
        )
        assert after == before

    def test_refuses_to_batch(self, client, auth_headers):
        to_id, line_ids = _create_to("TO-PB-DEL-1")
        batch_id = _provision_to_batch(to_id, line_ids)
        resp = client.post(
            f"/api/admin/pick-batches/{batch_id}/delete", headers=auth_headers
        )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "to_batch_not_deletable"
        # Untouched: still OPEN.
        assert _query_val(
            "SELECT status FROM pick_batches WHERE batch_id = %s", (batch_id,)
        ) == "OPEN"

    def test_unknown_batch_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/pick-batches/99999999/delete", headers=auth_headers
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        resp = client.post("/api/admin/pick-batches/1/delete")
        assert resp.status_code == 401

    def test_idempotent_on_already_released(self, client, auth_headers):
        created = _create_so_batch(client, auth_headers, ["SO-2026-001"])
        batch_id = created["batch_id"]
        first = client.post(
            f"/api/admin/pick-batches/{batch_id}/delete", headers=auth_headers
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/admin/pick-batches/{batch_id}/delete", headers=auth_headers
        )
        assert second.status_code == 200
        assert second.get_json()["released_tasks"] == 0
