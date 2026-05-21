"""so-refinement: HTTP-layer tests for POST /admin/sales-orders/<id>/revert-status.

The endpoint thinly wraps services.sales_order_service.revert_sales_order_status:
service-level invariants are covered in test_revert_sales_order_status.py;
this file pins the HTTP contract -- status codes, error-body shape,
auth, permission gating. RevertNotAllowed.kind maps to:

  not_found            -> 404
  picked_qty_remaining -> 409
  every other kind     -> 400
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
# Fixture helpers (raw-cursor inserts; per-test transaction owns them).
# ----------------------------------------------------------------------


def _insert_so(status="PICKED", **extra):
    """Insert a sales_orders row. **extra applies as a follow-up UPDATE
    so the base INSERT keeps a static column list (test_external_id_inserts
    scanner constraint)."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, external_id) "
        "VALUES (%s, %s, %s, 1, %s) RETURNING so_id",
        (f"SO-REVERT-EP-{uuid.uuid4().hex[:8]}", "Cust", status,
         str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    for col, val in extra.items():
        cur.execute(
            f"UPDATE sales_orders SET {col} = %s WHERE so_id = %s",
            (val, so_id),
        )
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


def _insert_so_line(so_id, item_id, *, qty_ordered=2, qty_picked=2,
                     status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_picked, 1, status),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _set_inv(item_id, bin_id, qty_on_hand):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, "
        "quantity_on_hand) VALUES (%s, %s, 1, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand",
        (item_id, bin_id, qty_on_hand),
    )
    cur.close()


def _insert_pick_task(so_id, sol_id, item_id, bin_id, qty=2, status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, 1, 'OPEN') RETURNING batch_id",
        (f"BATCH-EP-{uuid.uuid4().hex[:8]}",),
    )
    batch_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, "
        "bin_id, quantity_to_pick, quantity_picked, status, pick_sequence) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING pick_task_id",
        (batch_id, so_id, sol_id, item_id, bin_id, qty, qty, status, 1),
    )
    pt_id = cur.fetchone()[0]
    cur.close()
    return pt_id


def _picked_so_with_release_candidate():
    """Returns (so_id, pick_task_id) for a PICKED SO ready to revert."""
    item_id = _insert_item()
    _set_inv(item_id, bin_id=3, qty_on_hand=8)
    so_id = _insert_so(status="PICKED")
    sol_id = _insert_so_line(so_id, item_id, qty_ordered=2, qty_picked=2,
                              status="PICKED")
    pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3, qty=2,
                               status="PICKED")
    return so_id, pt_id


def _mint_restricted_user(client, username, page_keys):
    """Create a USER (non-ADMIN) with explicit page-permission grants
    and return their auth header. Used to verify the
    @require_admin_or_page_permission gate on the revert endpoint."""
    admin_resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    admin_token = admin_resp.get_json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    create_resp = client.post(
        "/api/admin/users",
        json={
            "username": username,
            "password": "password123",
            "full_name": username.title(),
            "role": "USER",
            "warehouse_ids": [1],
        },
        headers=admin_headers,
    )
    assert create_resp.status_code in (200, 201), create_resp.get_json()
    new_user_id = create_resp.get_json()["user_id"]
    # Page perms land via the dedicated PUT endpoint added in P6.1.
    perm_resp = client.put(
        f"/api/admin/users/{new_user_id}/permissions",
        json={"page_keys": list(page_keys)},
        headers=admin_headers,
    )
    assert perm_resp.status_code == 200, perm_resp.get_json()
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {login.get_json()['token']}"}


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


class TestRevertEndpointHappyPath:
    def test_200_full_release_to_open(self, client, auth_headers):
        so_id, pt_id = _picked_so_with_release_candidate()
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "OPEN", "release_pick_task_ids": [pt_id]},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["from_status"] == "PICKED"
        assert body["to_status"] == "OPEN"
        assert body["unpacked"] is False
        assert body["unshipped"] is False
        assert len(body["released_pick_tasks"]) == 1
        # Returned detail carries the per-pick context.
        det = body["released_pick_tasks"][0]
        assert det["pick_task_id"] == pt_id
        assert det["quantity"] == 2

    def test_200_release_only_no_status_change(self, client, auth_headers):
        """new_status == current_status is accepted (release-only path)."""
        so_id, pt_id = _picked_so_with_release_candidate()
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "PICKED", "release_pick_task_ids": [pt_id]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["from_status"] == "PICKED"
        assert body["to_status"] == "PICKED"


# ----------------------------------------------------------------------
# 4xx behaviour
# ----------------------------------------------------------------------


class TestRevertEndpointErrors:
    def test_404_so_not_found(self, client, auth_headers):
        resp = client.post(
            "/api/admin/sales-orders/9999999/revert-status",
            json={"new_status": "OPEN", "release_pick_task_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["kind"] == "not_found"

    def test_409_picked_qty_remaining(self, client, auth_headers):
        """Target below PICKED with held picks -> 409 + picked_qty_remaining.
        The frontend uses this kind to keep the modal open."""
        so_id, _pt = _picked_so_with_release_candidate()
        # Don't include the pick_task_id -> the SO still has
        # quantity_picked=2, and OPEN demands zero.
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "OPEN", "release_pick_task_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["kind"] == "picked_qty_remaining"
        assert body["remaining_picked"] == 2
        assert body["target_status"] == "OPEN"

    def test_400_forward_transition(self, client, auth_headers):
        so_id = _insert_so(status="PICKED")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "PACKED", "release_pick_task_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["kind"] == "not_backward"
        assert body["current_status"] == "PICKED"
        assert body["target_status"] == "PACKED"

    def test_400_invalid_target_status(self, client, auth_headers):
        so_id = _insert_so(status="PICKED")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "BOGUS", "release_pick_task_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["kind"] == "invalid_status"

    def test_400_cancelled_so(self, client, auth_headers):
        so_id = _insert_so(status="CANCELLED")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "OPEN", "release_pick_task_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["kind"] == "cancelled"

    def test_400_pick_task_not_on_this_so(self, client, auth_headers):
        """The endpoint must reject pick_task_ids that belong to a
        different SO so an operator cannot release someone else's
        inventory by guessing IDs."""
        so_id_target, _ = _picked_so_with_release_candidate()
        other_so, other_pt = _picked_so_with_release_candidate()
        # Try to release other_so's pick_task against so_id_target.
        resp = client.post(
            f"/api/admin/sales-orders/{so_id_target}/revert-status",
            json={"new_status": "OPEN",
                  "release_pick_task_ids": [other_pt]},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["kind"] == "pick_task_missing"
        assert other_pt in body["missing_ids"]

    def test_422_validation_error_missing_new_status(self, client, auth_headers):
        """The @validate_body(RevertSalesOrderStatusRequest) decorator
        rejects payloads that don't carry new_status. This is the
        pydantic-level guard, distinct from the service-level kind
        checks above."""
        so_id = _insert_so(status="PICKED")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"release_pick_task_ids": []},
            headers=auth_headers,
        )
        assert resp.status_code == 400  # validation_error returns 400
        body = resp.get_json()
        # @validate_body surfaces field-level details; expect new_status
        # to appear in the locator.
        details = body.get("details") or []
        locs = [d.get("loc") for d in details]
        assert any("new_status" in (loc or []) for loc in locs), body


# ----------------------------------------------------------------------
# Auth / permission gating
# ----------------------------------------------------------------------


class TestRevertEndpointAuth:
    def test_401_no_token(self, client):
        so_id = _insert_so(status="PICKED")
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "OPEN", "release_pick_task_ids": []},
        )
        assert resp.status_code == 401

    def test_403_user_without_sales_orders_permission(self, client):
        """A USER role without the sales-orders page key must get 403
        from @require_admin_or_page_permission. Use a user that has
        a different page key (warehouses) to prove the gate is keyed
        on sales-orders specifically, not just "has any page perm"."""
        so_id, _ = _picked_so_with_release_candidate()
        headers = _mint_restricted_user(
            client,
            username=f"noperm-{uuid.uuid4().hex[:6]}",
            page_keys=["warehouses"],
        )
        resp = client.post(
            f"/api/admin/sales-orders/{so_id}/revert-status",
            json={"new_status": "OPEN", "release_pick_task_ids": []},
            headers=headers,
        )
        assert resp.status_code == 403, resp.get_json()
