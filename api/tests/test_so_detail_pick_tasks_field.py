"""so-refinement: GET /admin/sales-orders/<id> exposes pick_tasks.

The frontend revert-status modal populates from this payload without
a second round-trip. Tests pin:

- The pick_tasks key is always present (empty array when no picks).
- Only status='PICKED' rows are returned (PENDING / RELEASED /
  SHORT / SKIPPED are excluded; they belong to other lifecycle UIs).
- Per-row shape: pick_task_id, so_line_id, item_id, sku, item_name,
  bin_id, bin_code, quantity_picked, picked_at, picked_by, status.
- Multiple pick_tasks on the same SO line all appear (the bin
  breakdown the modal renders).
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


def _insert_so(status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders (so_number, customer_name, status, "
        "warehouse_id, external_id) "
        "VALUES (%s, %s, %s, 1, %s) RETURNING so_id",
        (f"SO-PT-{uuid.uuid4().hex[:8]}", "Cust", status, str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012",
         str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, qty_picked=2):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, 'PICKED') RETURNING so_line_id",
        (so_id, item_id, qty_picked, qty_picked, 1),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _insert_pick_task(so_id, sol_id, item_id, bin_id, status="PICKED",
                       qty=2):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, 1, 'OPEN') RETURNING batch_id",
        (f"BATCH-PT-{uuid.uuid4().hex[:8]}",),
    )
    batch_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, "
        "bin_id, quantity_to_pick, quantity_picked, status, pick_sequence, "
        "picked_by, picked_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
        "RETURNING pick_task_id",
        (batch_id, so_id, sol_id, item_id, bin_id, qty, qty, status, 1,
         "operator-a"),
    )
    pt_id = cur.fetchone()[0]
    cur.close()
    return pt_id


class TestPickTasksField:
    def test_get_detail_always_includes_pick_tasks_key(self, client, auth_headers):
        """Even on an OPEN SO with zero picks, the key must be present
        as an empty array so the frontend's array access is safe."""
        resp = client.get("/api/admin/sales-orders/1", headers=auth_headers)
        body = resp.get_json()
        assert "pick_tasks" in body
        assert isinstance(body["pick_tasks"], list)

    def test_picked_so_returns_pick_tasks_with_full_shape(self, client, auth_headers):
        item_id = _insert_item()
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_picked=2)
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   status="PICKED", qty=2)

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        )
        body = resp.get_json()
        tasks = body["pick_tasks"]
        assert len(tasks) == 1
        t = tasks[0]
        # Required keys for the frontend revert modal.
        for key in ("pick_task_id", "so_line_id", "item_id", "sku",
                    "item_name", "bin_id", "bin_code", "quantity_picked",
                    "picked_at", "picked_by", "status"):
            assert key in t, f"missing {key} in pick_task shape"
        assert t["pick_task_id"] == pt_id
        assert t["so_line_id"] == sol_id
        assert t["item_id"] == item_id
        assert t["bin_id"] == 3
        assert t["quantity_picked"] == 2
        assert t["status"] == "PICKED"
        assert t["picked_by"] == "operator-a"
        # picked_at is an ISO timestamp string when set.
        assert t["picked_at"] is not None
        assert isinstance(t["picked_at"], str)

    def test_filters_to_picked_status_only(self, client, auth_headers):
        """PENDING / SHORT / SKIPPED / RELEASED pick_tasks must not
        appear in the array. Operator-facing revert flow is for
        currently-picked inventory only."""
        item_id = _insert_item()
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_picked=2)
        pt_picked = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                       status="PICKED")
        # All four off-status flavours -- none should appear.
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          status="PENDING")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          status="SHORT")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          status="SKIPPED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          status="RELEASED")

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        )
        tasks = resp.get_json()["pick_tasks"]
        ids = {t["pick_task_id"] for t in tasks}
        assert ids == {pt_picked}

    def test_multiple_picked_tasks_all_returned(self, client, auth_headers):
        """The bin breakdown the modal renders: a line picked from
        multiple bins gets a row per bin."""
        item_id = _insert_item()
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_picked=4)
        pt_a = _insert_pick_task(so_id, sol_id, item_id, bin_id=3, qty=2)
        pt_b = _insert_pick_task(so_id, sol_id, item_id, bin_id=4, qty=2)

        resp = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        )
        tasks = resp.get_json()["pick_tasks"]
        assert len(tasks) == 2
        ids = {t["pick_task_id"] for t in tasks}
        assert ids == {pt_a, pt_b}
        bins = {t["bin_id"] for t in tasks}
        assert bins == {3, 4}
