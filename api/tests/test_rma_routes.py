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


def test_list_filters_by_order_type_return(client, auth_headers, _db_transaction):
    """The RMA admin page lists only return SOs via order_type=return; the row
    carries order_type + parent_so_id so the page can link the case."""
    parent_no = f"POS-{uuid.uuid4().hex[:8]}"
    parent_id = _insert_so(parent_no)
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=3)
    rma = client.post(
        f"/api/admin/sales-orders/{parent_id}/create-rma",
        json={"lines": [{"item_id": item, "quantity": 2, "original_so_line_id": line}]},
        headers=auth_headers,
    ).get_json()
    rma_number = rma["so_number"]

    listing = client.get(
        "/api/admin/sales-orders?order_type=return&per_page=1000",
        headers=auth_headers,
    ).get_json()
    numbers = {so["so_number"]: so for so in listing["sales_orders"]}
    # The RMA shows up; the parent sale does not.
    assert rma_number in numbers
    assert parent_no not in numbers
    assert numbers[rma_number]["order_type"] == "return"
    assert numbers[rma_number]["parent_so_id"] == parent_id


def test_void_return_removes_it_from_the_rma_list(client, auth_headers, _db_transaction):
    """An ADMIN void soft-deletes an un-received RMA: the route returns 200
    and the RMA drops out of the order_type=return listing, while the row
    persists with voided_at stamped."""
    db = _db_transaction
    parent_no = f"POS-{uuid.uuid4().hex[:8]}"
    parent_id = _insert_so(parent_no)
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=3)
    rma = client.post(
        f"/api/admin/sales-orders/{parent_id}/create-rma",
        json={"lines": [{"item_id": item, "quantity": 2, "original_so_line_id": line}]},
        headers=auth_headers,
    ).get_json()
    rma_so_id = rma["so_id"]
    rma_number = rma["so_number"]

    # Present before the void.
    before = client.get(
        "/api/admin/sales-orders?order_type=return&per_page=1000",
        headers=auth_headers,
    ).get_json()
    assert rma_number in {so["so_number"] for so in before["sales_orders"]}

    # Void it (ADMIN).
    v = client.post(
        f"/api/admin/sales-orders/{rma_so_id}/void-return",
        headers=auth_headers,
    )
    assert v.status_code == 200, v.get_data(as_text=True)
    assert v.get_json()["so_number"] == rma_number

    # Gone from the listing, but the row persists with voided_at set.
    after = client.get(
        "/api/admin/sales-orders?order_type=return&per_page=1000",
        headers=auth_headers,
    ).get_json()
    assert rma_number not in {so["so_number"] for so in after["sales_orders"]}
    assert db.execute(
        sa_text("SELECT voided_at FROM sales_orders WHERE so_id = :s"),
        {"s": rma_so_id},
    ).fetchone().voided_at is not None


def test_get_sales_order_lines_carry_quantity_received(
    client, auth_headers, _db_transaction
):
    """The single-SO GET reports quantity_received per line so the RMA
    receiving screen can show how much of each line is still outstanding."""
    db = _db_transaction
    parent_no = f"POS-{uuid.uuid4().hex[:8]}"
    parent_id = _insert_so(parent_no)
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=3)
    bin_id = db.execute(
        sa_text("SELECT bin_id FROM bins WHERE warehouse_id = 1 ORDER BY bin_id LIMIT 1")
    ).scalar()
    rma = client.post(
        f"/api/admin/sales-orders/{parent_id}/create-rma",
        json={"lines": [{"item_id": item, "quantity": 3, "original_so_line_id": line}]},
        headers=auth_headers,
    ).get_json()
    rma_so_id = rma["so_id"]

    # Before receiving: quantity_received is 0.
    before = client.get(
        f"/api/admin/sales-orders/{rma_so_id}", headers=auth_headers
    ).get_json()
    assert before["lines"][0]["quantity_received"] == 0

    # Receive one unit; the field advances.
    client.post(
        f"/api/admin/sales-orders/{rma_so_id}/receive-return",
        json={"item_id": item, "quantity": 1, "warehouse_id": 1, "bin_id": bin_id},
        headers=auth_headers,
    )
    after = client.get(
        f"/api/admin/sales-orders/{rma_so_id}", headers=auth_headers
    ).get_json()
    assert after["lines"][0]["quantity_received"] == 1


def test_create_rma_persists_memo(client, auth_headers, _db_transaction):
    """An optional memo on the create-RMA request lands on the RMA's
    sales_orders.memo (no new column; memo is from mig 054). Whitespace is
    trimmed, matching the SO memo PATCH."""
    db = _db_transaction
    parent_id = _insert_so(f"POS-{uuid.uuid4().hex[:8]}")
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=3)
    r = client.post(
        f"/api/admin/sales-orders/{parent_id}/create-rma",
        json={
            "lines": [{"item_id": item, "quantity": 1, "original_so_line_id": line}],
            "memo": "  box arrived crushed  ",
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    memo = db.execute(
        sa_text("SELECT memo FROM sales_orders WHERE so_id = :sid"),
        {"sid": r.get_json()["so_id"]},
    ).scalar()
    assert memo == "box arrived crushed"


def test_create_rma_without_memo_is_null(client, auth_headers, _db_transaction):
    db = _db_transaction
    parent_id = _insert_so(f"POS-{uuid.uuid4().hex[:8]}")
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=3)
    r = client.post(
        f"/api/admin/sales-orders/{parent_id}/create-rma",
        json={"lines": [{"item_id": item, "quantity": 1, "original_so_line_id": line}]},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    memo = db.execute(
        sa_text("SELECT memo FROM sales_orders WHERE so_id = :sid"),
        {"sid": r.get_json()["so_id"]},
    ).scalar()
    assert memo is None


def _insert_return_so(so_number, *, status="OPEN", warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, "
        " order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", status, warehouse_id, "return", "web", str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def test_return_excluded_from_picking_queue(client, auth_headers, _db_transaction):
    """include_primary_bin marks the picking-ticket queue (goods-OUT). A
    return must never appear there even when OPEN, but the Returns page
    (order_type=return) still sees it."""
    db = _db_transaction
    base = f"POS-{uuid.uuid4().hex[:8]}"
    sale_id = _insert_so(base)  # order_type='sale'
    db.execute(
        sa_text("UPDATE sales_orders SET status = 'OPEN' WHERE so_id = :sid"),
        {"sid": sale_id},
    )
    return_id = _insert_return_so(f"{base}-RMA")

    queue = client.get(
        "/api/admin/sales-orders?status=OPEN&include_primary_bin=true"
        "&warehouse_id=1&per_page=1000",
        headers=auth_headers,
    ).get_json()["sales_orders"]
    queue_ids = {r["so_id"] for r in queue}
    assert sale_id in queue_ids        # a normal sale prints/picks
    assert return_id not in queue_ids  # the return is kept out at the root

    returns_page = client.get(
        "/api/admin/sales-orders?status=OPEN&order_type=return"
        "&warehouse_id=1&per_page=1000",
        headers=auth_headers,
    ).get_json()["sales_orders"]
    assert return_id in {r["so_id"] for r in returns_page}
