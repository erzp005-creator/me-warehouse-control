"""Service-level tests for sales_order_service.create_rma.

create_rma builds the goods-in RMA SO (order_type='return') off an original
order: so_number "<original>-RMA" via the suffix helper, warehouse/order_source
inherited, one return line per selected item pointing back to its original line.
Setup uses raw-cursor inserts sharing the per-test transaction; the service runs
on the same transaction via the SQLAlchemy connection.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
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


def _insert_so(so_number, *, order_type="sale", status="SHIPPED",
               warehouse_id=1, order_source="web"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, "
        " order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", status, warehouse_id, order_type, order_source,
         str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_so_line(so_id, item_id, *, qty=2, line_number=1, status="SHIPPED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING so_line_id",
        (so_id, item_id, qty, line_number, status),
    )
    so_line_id = cur.fetchone()[0]
    cur.close()
    return so_line_id


class TestCreateRma:
    def test_creates_return_so_with_suffix_and_linked_lines(self, _db_transaction):
        from services.sales_order_service import create_rma
        db = _db_transaction
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        parent_id = _insert_so(parent_no, order_type="sale", order_source="web")
        item_a, item_b = _insert_item(), _insert_item()
        line_a = _insert_so_line(parent_id, item_a, qty=3, line_number=1)
        line_b = _insert_so_line(parent_id, item_b, qty=5, line_number=2)

        result = create_rma(
            db,
            original_so_id=parent_id,
            lines=[
                {"item_id": item_a, "quantity": 1, "original_so_line_id": line_a},
                {"item_id": item_b, "quantity": 2, "original_so_line_id": line_b},
            ],
            created_by="op",
        )

        assert result["so_number"] == f"{parent_no}-RMA"
        rma = db.execute(
            sa_text("SELECT order_type, parent_so_id, status, order_source "
                    "FROM sales_orders WHERE so_id = :s"),
            {"s": result["so_id"]},
        ).fetchone()
        assert rma.order_type == "return"
        assert rma.parent_so_id == parent_id
        assert rma.status == "OPEN"
        assert rma.order_source == "web"  # inherited from the original

        rows = db.execute(
            sa_text("SELECT item_id, quantity_ordered, original_so_line_id, status "
                    "FROM sales_order_lines WHERE so_id = :s ORDER BY line_number"),
            {"s": result["so_id"]},
        ).fetchall()
        assert len(rows) == 2
        assert rows[0].item_id == item_a
        assert rows[0].quantity_ordered == 1
        assert rows[0].original_so_line_id == line_a
        assert rows[0].status == "OPEN"
        assert rows[1].original_so_line_id == line_b
        assert rows[1].quantity_ordered == 2

    def test_missing_original_raises(self, _db_transaction):
        from services.sales_order_service import create_rma
        db = _db_transaction
        with pytest.raises(ValueError):
            create_rma(db, original_so_id=999_999_999, lines=[], created_by="op")
