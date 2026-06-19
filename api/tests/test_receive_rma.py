"""Service-level tests for sales_order_service.receive_rma.

receive_rma books goods back against a return SO (the <orig>-RMA): an
item_receipts row, an inventory restock to the destination bin, the return
line's quantity_received + the RMA status advance, and a return.received
emit. Setup builds the RMA via create_rma. The helper takes the externals
(received_by_external_id / source_txn_id) as params, so no request context is
needed.
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


def _insert_so(so_number, *, status="SHIPPED", warehouse_id=1, order_source="web"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, "
        " order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", status, warehouse_id, "sale", order_source,
         str(uuid.uuid4())),
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


def _a_bin(db, warehouse_id=1):
    return db.execute(
        sa_text("SELECT bin_id FROM bins WHERE warehouse_id = :w ORDER BY bin_id LIMIT 1"),
        {"w": warehouse_id},
    ).scalar()


def _make_rma(db, *, qty_ordered=3, qty_to_return=2):
    from services.sales_order_service import create_rma
    parent_id = _insert_so(f"POS-{uuid.uuid4().hex[:8]}")
    item = _insert_item()
    line = _insert_so_line(parent_id, item, qty=qty_ordered)
    rma = create_rma(
        db,
        original_so_id=parent_id,
        lines=[{"item_id": item, "quantity": qty_to_return, "original_so_line_id": line}],
        created_by="admin",
    )
    return rma, item


class TestReceiveRma:
    def test_full_receipt_marks_received_and_restocks(self, _db_transaction):
        from services.sales_order_service import receive_rma
        db = _db_transaction
        rma, item = _make_rma(db, qty_ordered=3, qty_to_return=2)
        bin_id = _a_bin(db)
        before = db.execute(
            sa_text("SELECT COALESCE(SUM(quantity_on_hand),0) FROM inventory "
                    "WHERE item_id = :i AND bin_id = :b"),
            {"i": item, "b": bin_id},
        ).scalar()

        result = receive_rma(
            db, rma_so_id=rma["so_id"], item_id=item, quantity=2,
            warehouse_id=1, bin_id=bin_id, received_by="admin",
            received_by_external_id=str(uuid.uuid4()),
            source_txn_id=str(uuid.uuid4()),
        )

        assert result["status"] == "RECEIVED"
        rcpt = db.execute(
            sa_text("SELECT so_id, quantity_received, bin_id FROM item_receipts "
                    "WHERE receipt_id = :r"),
            {"r": result["receipt_id"]},
        ).fetchone()
        assert rcpt.so_id == rma["so_id"]
        assert rcpt.quantity_received == 2
        assert rcpt.bin_id == bin_id
        assert db.execute(
            sa_text("SELECT quantity_received FROM sales_order_lines WHERE so_id = :s"),
            {"s": rma["so_id"]},
        ).scalar() == 2
        assert db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": rma["so_id"]},
        ).scalar() == "RECEIVED"
        after = db.execute(
            sa_text("SELECT COALESCE(SUM(quantity_on_hand),0) FROM inventory "
                    "WHERE item_id = :i AND bin_id = :b"),
            {"i": item, "b": bin_id},
        ).scalar()
        assert after == before + 2
        assert db.execute(
            sa_text("SELECT COUNT(*) FROM integration_events "
                    "WHERE event_type = 'return.received'"),
        ).scalar() >= 1

    def test_partial_receipt_marks_partially_received(self, _db_transaction):
        from services.sales_order_service import receive_rma
        db = _db_transaction
        rma, item = _make_rma(db, qty_ordered=3, qty_to_return=2)
        bin_id = _a_bin(db)

        result = receive_rma(
            db, rma_so_id=rma["so_id"], item_id=item, quantity=1,
            warehouse_id=1, bin_id=bin_id, received_by="admin",
            received_by_external_id=str(uuid.uuid4()),
            source_txn_id=str(uuid.uuid4()),
        )

        assert result["status"] == "PARTIALLY_RECEIVED"
        assert db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": rma["so_id"]},
        ).scalar() == "PARTIALLY_RECEIVED"

    def test_item_not_on_rma_raises(self, _db_transaction):
        from services.sales_order_service import receive_rma
        db = _db_transaction
        rma, _item = _make_rma(db)
        other_item = _insert_item()
        with pytest.raises(ValueError):
            receive_rma(
                db, rma_so_id=rma["so_id"], item_id=other_item, quantity=1,
                warehouse_id=1, bin_id=_a_bin(db), received_by="admin",
                received_by_external_id=str(uuid.uuid4()),
                source_txn_id=str(uuid.uuid4()),
            )
