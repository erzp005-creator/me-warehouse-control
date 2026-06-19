"""Route tests for the admin RMA create + receive endpoints.

Exercises both endpoints end to end through the test client (auth, body
validation, request context, and the real return.received emit on receive).
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text as sa_text

from db_test_context import get_raw_connection


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012", str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so(so_number, *, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, "
        " order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", "SHIPPED", warehouse_id, "sale", "web", str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_so_line(so_id, item_id, *, qty=3, line_number=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING so_line_id",
        (so_id, item_id, qty, line_number, "SHIPPED"),
    )
    so_line_id = cur.fetchone()[0]
    cur.close()
    return so_line_id


def test_create_then_receive_rma_via_routes(client, auth_headers, _db_transaction):
    db = _db_transaction
    parent_no = f"POS-{uuid.uuid4().hex[:8]}"
    parent_id = _insert_so(parent_no)
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=3)
    bin_id = db.execute(
        sa_text("SELECT bin_id FROM bins WHERE warehouse_id = 1 ORDER BY bin_id LIMIT 1")
    ).scalar()

    # Create the RMA for 2 of the 3 units.
    r = client.post(
        f"/api/admin/sales-orders/{parent_id}/create-rma",
        json={"lines": [{"item_id": item, "quantity": 2, "original_so_line_id": line}]},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    body = r.get_json()
    rma_so_id = body["so_id"]
    assert body["so_number"] == f"{parent_no}-RMA"

    # Receive both units into the warehouse-1 destination bin.
    r2 = client.post(
        f"/api/admin/sales-orders/{rma_so_id}/receive-return",
        json={"item_id": item, "quantity": 2, "warehouse_id": 1, "bin_id": bin_id},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json()["status"] == "RECEIVED"

    # The return.received event was emitted in the real request context.
    assert db.execute(
        sa_text("SELECT COUNT(*) FROM integration_events "
                "WHERE event_type = 'return.received'")
    ).scalar() >= 1


def test_create_rma_unknown_original_returns_400(client, auth_headers):
    r = client.post(
        "/api/admin/sales-orders/999999999/create-rma",
        json={"lines": [{"item_id": 1, "quantity": 1}]},
        headers=auth_headers,
    )
    assert r.status_code == 400, r.get_data(as_text=True)
