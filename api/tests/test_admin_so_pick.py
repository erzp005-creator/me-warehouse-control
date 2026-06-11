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
