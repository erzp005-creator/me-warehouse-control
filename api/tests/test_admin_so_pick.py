"""HTTP + service-layer tests for the admin virtual-pick path.

POST /api/admin/sales-orders/<so_id>/admin-pick takes a batched body
of {so_line_id, bin_id, quantity} entries and applies them as if the
handheld had picked them. End state must be indistinguishable from a
real pick: line counters bump, inventory.quantity_on_hand decrements,
audit ACTION_PICK rows land per pick, pick.confirmed emits when the
SO flips to PICKED.

This file (C1) pins the happy path: single-line pick, full coverage,
SO promotes to PICKED, response shape is what the UI expects.
Edge cases live in C2 (test_admin_so_pick_edge_cases.py).
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


# ----------------------------------------------------------------------
# Seed helpers (raw cursors -- the per-test transaction owns the writes)
# ----------------------------------------------------------------------


def _insert_so(status="OPEN", warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING so_id, external_id",
        (
            f"SO-ADMINPICK-{uuid.uuid4().hex[:8]}",
            "Cust",
            status,
            warehouse_id,
            str(uuid.uuid4()),
        ),
    )
    so_id, external_id = cur.fetchone()
    cur.close()
    return so_id, external_id


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (
            f"SKU-{uuid.uuid4().hex[:8]}",
            "Widget",
            "0123456789012",
            str(uuid.uuid4()),
        ),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_line(so_id, item_id, *, qty_ordered=2, qty_picked=0, qty_allocated=0):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, "
        " quantity_allocated, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, 1, 'OPEN') RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_picked, qty_allocated),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _set_inv(item_id, bin_id, qty_on_hand, qty_allocated=0, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory "
        "(item_id, bin_id, warehouse_id, quantity_on_hand, quantity_allocated) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand, "
        "    quantity_allocated = EXCLUDED.quantity_allocated",
        (item_id, bin_id, warehouse_id, qty_on_hand, qty_allocated),
    )
    cur.close()


def _read_so(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT status, picked_at FROM sales_orders WHERE so_id = %s",
        (so_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {"status": row[0], "picked_at": row[1]}


def _read_line(sol_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_ordered, quantity_picked, quantity_allocated "
        "FROM sales_order_lines WHERE so_line_id = %s",
        (sol_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {
        "quantity_ordered": row[0],
        "quantity_picked": row[1],
        "quantity_allocated": row[2],
    }


def _read_inv(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory "
        "WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    return {
        "quantity_on_hand": row[0],
        "quantity_allocated": row[1],
    }


def _admin_headers(client):
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"},
    )
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


# ----------------------------------------------------------------------
# C1: happy path
# ----------------------------------------------------------------------


class TestAdminPickHappyPath:
    def test_single_line_full_pick_promotes_so(self, client):
        # Seed: one OPEN SO with one line for 2 units, bin holds 5
        # available. Operator admin-picks both units from the bin.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )

        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["message"] == "Admin pick applied"
        assert body["promoted_to_picked"] is True
        assert body["picks_applied"] == 1
        assert len(body["pick_task_ids"]) == 1
        assert body["batch_number"].startswith(f"ADMIN-PICK-{so_id}-")

        # Line counters bumped, inventory decremented at the bin.
        line = _read_line(sol_id)
        assert line["quantity_picked"] == 2
        assert line["quantity_allocated"] >= 2  # picked-floor invariant

        inv = _read_inv(item_id, 3)
        assert inv["quantity_on_hand"] == 3
        # quantity_allocated must NOT have been touched: admin-pick never
        # pre-allocated against this bin (the available check is the
        # safety, not pre-allocation).
        assert inv["quantity_allocated"] == 0

        # SO promoted: status flips, picked_at set.
        so = _read_so(so_id)
        assert so["status"] == "PICKED"
        assert so["picked_at"] is not None


# ----------------------------------------------------------------------
# C2: edge cases -- partial picks, validation errors, all-or-nothing,
# split-bin, event emission, undo round-trip, auth
# ----------------------------------------------------------------------


def _count_audit(so_id, action="PICK"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM audit_log "
        " WHERE entity_type = 'SO' AND entity_id = %s AND action_type = %s",
        (so_id, action),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


def _count_events(so_id, event_type="pick.confirmed"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM integration_events "
        " WHERE aggregate_type = 'sales_order' "
        "   AND aggregate_id = %s AND event_type = %s",
        (so_id, event_type),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


def _count_pick_tasks(so_id, status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM pick_tasks WHERE so_id = %s AND status = %s",
        (so_id, status),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


class TestAdminPickValidation:
    def test_so_not_open_returns_422(self, client):
        # SO already PICKED -- the admin-pick path is for OPEN orders
        # only; mutating a PICKED order goes through the line edit
        # surface, not this endpoint.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_id, _ = _insert_so(status="PICKED")
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 1},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 422, resp.get_json()
        body = resp.get_json()
        assert body["kind"] == "so_not_open"
        assert body["current_status"] == "PICKED"

    def test_over_pick_returns_422(self, client):
        # Line ordered=2, already picked=1, remaining=1. Requesting 2
        # should bounce.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2, qty_picked=1)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 422, resp.get_json()
        body = resp.get_json()
        assert body["kind"] == "over_pick"
        assert body["requested"] == 2
        assert body["remaining"] == 1

    def test_insufficient_available_returns_409(self, client):
        # Bin has 3 on-hand but 2 already allocated to a different SO.
        # Available = 1; requesting 2 must fail with 409 (not 422 --
        # this is a transient inventory state, not operator error).
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=3, qty_allocated=2)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["kind"] == "insufficient_available"

        # Atomic rollback: inventory untouched.
        inv = _read_inv(item_id, 3)
        assert inv["quantity_on_hand"] == 3
        assert inv["quantity_allocated"] == 2

    def test_bin_wrong_warehouse_returns_422(self, client):
        # SO is in warehouse 1; bin lives in warehouse 2. Seed has the
        # warehouse row but no zones/bins under it, so fabricate the
        # zone + bin inside this test's transaction.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO zones "
            "  (zone_name, zone_code, zone_type, warehouse_id) "
            "VALUES (%s, %s, 'Pickable', 2) RETURNING zone_id",
            (
                f"Z-WH2-{uuid.uuid4().hex[:6]}",
                f"ZWH2-{uuid.uuid4().hex[:6]}",
            ),
        )
        zone_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO bins "
            "  (zone_id, warehouse_id, bin_code, bin_barcode, external_id) "
            "VALUES (%s, 2, %s, %s, %s) RETURNING bin_id",
            (
                zone_id,
                f"WH2-{uuid.uuid4().hex[:6]}",
                f"WH2BC-{uuid.uuid4().hex[:6]}",
                str(uuid.uuid4()),
            ),
        )
        wh2_bin_id = cur.fetchone()[0]
        cur.close()

        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_id, _ = _insert_so()  # warehouse 1
        sol_id = _insert_line(so_id, item_id, qty_ordered=1)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": wh2_bin_id, "quantity": 1},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 422, resp.get_json()
        assert resp.get_json()["kind"] == "bin_wrong_warehouse"

    def test_line_not_on_so_returns_422(self, client):
        # so_line_id belongs to a DIFFERENT SO. The endpoint must
        # reject -- otherwise the wrong order's line counters move.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_a, _ = _insert_so()
        so_b, _ = _insert_so()
        sol_b = _insert_line(so_b, item_id, qty_ordered=2)

        resp = client.post(
            f"/api/admin/sales-orders/{so_a}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_b, "bin_id": 3, "quantity": 1},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 422, resp.get_json()
        assert resp.get_json()["kind"] == "line_not_on_so"


class TestAdminPickPartialAndFull:
    def test_partial_pick_keeps_so_open_no_event(self, client):
        # Line ordered=5, admin picks 2. SO stays OPEN -- no event.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=5)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["promoted_to_picked"] is False

        assert _read_so(so_id)["status"] == "OPEN"
        assert _count_events(so_id) == 0
        assert _read_line(sol_id)["quantity_picked"] == 2

    def test_full_pick_emits_pick_confirmed_event(self, client):
        # Single round of full picks promotes the SO and writes
        # exactly one pick.confirmed row to integration_events.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=3)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 3},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["promoted_to_picked"] is True

        assert _read_so(so_id)["status"] == "PICKED"
        assert _count_events(so_id, "pick.confirmed") == 1


class TestAdminPickSplitBin:
    def test_split_bin_picks_sum_per_line(self, client):
        # Line ordered=5; operator splits across bin 3 (3 units) and
        # bin 4 (2 units). Two pick_tasks land, line picked total = 5,
        # SO promotes.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=4)
        _set_inv(item_id, bin_id=4, qty_on_hand=4)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=5)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 3},
                {"so_line_id": sol_id, "bin_id": 4, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["picks_applied"] == 2
        assert body["promoted_to_picked"] is True

        assert _read_line(sol_id)["quantity_picked"] == 5
        assert _read_inv(item_id, 3)["quantity_on_hand"] == 1
        assert _read_inv(item_id, 4)["quantity_on_hand"] == 2
        assert _count_pick_tasks(so_id) == 2
        assert _count_audit(so_id, "PICK") == 2


class TestAdminPickAtomic:
    def test_all_or_nothing_failure_rolls_back_prior_writes(self, client):
        # Two-line submit: line A's bin has enough, line B's bin does
        # not. The whole batch must roll back; line A's quantity_picked
        # must stay 0 and the bin inventory untouched.
        item_a = _insert_item()
        item_b = _insert_item()
        _set_inv(item_a, bin_id=3, qty_on_hand=5)
        _set_inv(item_b, bin_id=4, qty_on_hand=1)  # not enough for 2
        so_id, _ = _insert_so()
        sol_a = _insert_line(so_id, item_a, qty_ordered=2)
        sol_b = _insert_line(so_id, item_b, qty_ordered=2)
        # second insert added a row at line_number=1 again; fix it.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_order_lines SET line_number = 2 WHERE so_line_id = %s",
            (sol_b,),
        )
        cur.close()

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_a, "bin_id": 3, "quantity": 2},
                {"so_line_id": sol_b, "bin_id": 4, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 409, resp.get_json()
        assert resp.get_json()["kind"] == "insufficient_available"

        # Atomic rollback: NEITHER line moved, NEITHER bin decremented.
        assert _read_line(sol_a)["quantity_picked"] == 0
        assert _read_line(sol_b)["quantity_picked"] == 0
        assert _read_inv(item_a, 3)["quantity_on_hand"] == 5
        assert _read_inv(item_b, 4)["quantity_on_hand"] == 1
        assert _read_so(so_id)["status"] == "OPEN"
        assert _count_pick_tasks(so_id) == 0
        assert _count_audit(so_id, "PICK") == 0


class TestAdminPickReleaseRoundTrip:
    def test_synthetic_pick_task_releases_via_revert_endpoint(self, client):
        # An admin-pick that fully promotes the SO to PICKED. The
        # operator then opens the existing Release modal and releases
        # the synthetic pick_task. Inventory should restore, line
        # picked count drops to zero, SO demotes back to OPEN. This
        # is the round-trip the user wants: same Release button.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        pick_resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 2},
            ]},
            headers=_admin_headers(client),
        )
        assert pick_resp.status_code == 200, pick_resp.get_json()
        pick_task_id = pick_resp.get_json()["pick_task_ids"][0]
        assert _read_so(so_id)["status"] == "PICKED"
        assert _read_inv(item_id, 3)["quantity_on_hand"] == 3

        revert_resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={
                "new_status": "OPEN",
                "release_pick_task_ids": [pick_task_id],
            },
            headers=_admin_headers(client),
        )
        assert revert_resp.status_code == 200, revert_resp.get_json()

        # Round-trip invariants.
        assert _read_so(so_id)["status"] == "OPEN"
        assert _read_line(sol_id)["quantity_picked"] == 0
        assert _read_inv(item_id, 3)["quantity_on_hand"] == 5


# ----------------------------------------------------------------------
# C3: GET /sales-orders/<so_id>/lines/<so_line_id>/available-bins
# ----------------------------------------------------------------------


class TestAdminPickAvailableBins:
    def test_returns_bins_with_available_stock(self, client):
        # Item stocked in bin 3 (5 on-hand, 1 allocated -> 4 available)
        # and bin 4 (10 on-hand, 0 allocated -> 10 available). Both
        # should surface.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5, qty_allocated=1)
        _set_inv(item_id, bin_id=4, qty_on_hand=10)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}/lines/{sol_id}/available-bins",
            headers=_admin_headers(client),
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["item_id"] == item_id
        assert body["warehouse_id"] == 1
        bin_ids = [b["bin_id"] for b in body["bins"]]
        assert 3 in bin_ids
        assert 4 in bin_ids
        bin_3 = next(b for b in body["bins"] if b["bin_id"] == 3)
        assert bin_3["quantity_available"] == 4
        assert bin_3["quantity_on_hand"] == 5
        assert bin_3["quantity_allocated"] == 1

    def test_excludes_zero_available_bins(self, client):
        # Bin 3: fully allocated (on_hand 2, allocated 2 -> 0
        # available). Bin 4: has stock. Only bin 4 surfaces.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=2, qty_allocated=2)
        _set_inv(item_id, bin_id=4, qty_on_hand=5)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}/lines/{sol_id}/available-bins",
            headers=_admin_headers(client),
        )
        body = resp.get_json()
        bin_ids = [b["bin_id"] for b in body["bins"]]
        assert 3 not in bin_ids
        assert 4 in bin_ids

    def test_scoped_to_so_warehouse(self, client):
        # Item has stock in a warehouse-2 bin AND in a warehouse-1
        # bin. The SO lives in warehouse 1; only the warehouse-1 bin
        # is allowed to surface.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO zones "
            "  (zone_name, zone_code, zone_type, warehouse_id) "
            "VALUES (%s, %s, 'Pickable', 2) RETURNING zone_id",
            (
                f"Z-WH2-{uuid.uuid4().hex[:6]}",
                f"ZWH2-{uuid.uuid4().hex[:6]}",
            ),
        )
        zone_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO bins "
            "  (zone_id, warehouse_id, bin_code, bin_barcode, external_id) "
            "VALUES (%s, 2, %s, %s, %s) RETURNING bin_id",
            (
                zone_id,
                f"WH2-{uuid.uuid4().hex[:6]}",
                f"WH2BC-{uuid.uuid4().hex[:6]}",
                str(uuid.uuid4()),
            ),
        )
        wh2_bin_id = cur.fetchone()[0]
        cur.close()

        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)              # wh1
        _set_inv(item_id, bin_id=wh2_bin_id, qty_on_hand=10,
                 warehouse_id=2)
        so_id, _ = _insert_so()  # warehouse 1
        sol_id = _insert_line(so_id, item_id, qty_ordered=2)

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}/lines/{sol_id}/available-bins",
            headers=_admin_headers(client),
        )
        body = resp.get_json()
        bin_ids = [b["bin_id"] for b in body["bins"]]
        assert 3 in bin_ids
        assert wh2_bin_id not in bin_ids

    def test_sorts_preferred_bins_first(self, client):
        # Bin 4 has preferred priority 1; bin 3 has no preferred row.
        # Bin 4 must appear ahead of bin 3 in the response, regardless
        # of which has more stock.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=99)
        _set_inv(item_id, bin_id=4, qty_on_hand=1)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO preferred_bins (item_id, bin_id, priority) "
            "VALUES (%s, 4, 1)",
            (item_id,),
        )
        cur.close()
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=1)

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}/lines/{sol_id}/available-bins",
            headers=_admin_headers(client),
        )
        body = resp.get_json()
        bin_ids = [b["bin_id"] for b in body["bins"]]
        # bin 4 (preferred priority 1) ahead of bin 3 (no preference)
        assert bin_ids.index(4) < bin_ids.index(3)

    def test_unknown_line_returns_404(self, client):
        so_id, _ = _insert_so()
        resp = client.get(
            f"/api/admin/sales-orders/{so_id}/lines/9999999/available-bins",
            headers=_admin_headers(client),
        )
        assert resp.status_code == 404

    def test_line_on_different_so_returns_404(self, client):
        # The line exists but on a different SO; the endpoint must
        # treat that as not-found rather than leaking the other SO's
        # line shape.
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_a, _ = _insert_so()
        so_b, _ = _insert_so()
        sol_b = _insert_line(so_b, item_id, qty_ordered=1)

        resp = client.get(
            f"/api/admin/sales-orders/{so_a}/lines/{sol_b}/available-bins",
            headers=_admin_headers(client),
        )
        assert resp.status_code == 404


class TestAdminPickAuth:
    def test_non_admin_without_override_returns_403(self, client):
        # Provision a USER with sales-orders page permission but NO
        # so-full-edit override. The page perm gets them past the
        # decorator; the extra ADMIN-or-override check inside the
        # handler should still 403.
        admin_headers = _admin_headers(client)
        create_resp = client.post(
            "/api/admin/users",
            json={
                "username": f"adminpickuser-{uuid.uuid4().hex[:6]}",
                "password": "password123",
                "full_name": "Pick User",
                "role": "USER",
                "warehouse_ids": [1],
            },
            headers=admin_headers,
        )
        assert create_resp.status_code in (200, 201), create_resp.get_json()
        new_user = create_resp.get_json()
        username = new_user["username"]
        user_id = new_user["user_id"]
        perm_resp = client.put(
            f"/api/admin/users/{user_id}/permissions",
            json={"page_keys": ["sales-orders"]},
            headers=admin_headers,
        )
        assert perm_resp.status_code == 200, perm_resp.get_json()
        login = client.post(
            "/api/auth/login",
            json={"username": username, "password": "password123"},
        )
        user_headers = {
            "Authorization": f"Bearer {login.get_json()['token']}",
        }

        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=5)
        so_id, _ = _insert_so()
        sol_id = _insert_line(so_id, item_id, qty_ordered=1)

        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/admin-pick",
            json={"lines": [
                {"so_line_id": sol_id, "bin_id": 3, "quantity": 1},
            ]},
            headers=user_headers,
        )
        assert resp.status_code == 403, resp.get_json()
