"""Service contract for void_return_order (RMA soft-delete).

void_return_order soft-deletes a mistakenly created return SO (order_type=
'return'): it stamps sales_orders.voided_at / voided_by, writes a RETURN_VOID
audit row, and leaves inventory + receipts + the parent/refund links alone.
It is deliberately distinct from cancel_sales_order (which unwinds outbound
allocation/picking, wrong for a goods-in return).

Invariants exercised here:
- Happy path: an OPEN, un-received return voids -> voided_at/by set + one
  RETURN_VOID audit row.
- Idempotent: a second void is a no-op (no new audit row).
- Guards refuse with a typed reason: not_a_return, not_open, has_receipts,
  has_refund.
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


def _ensure_user(username="op"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
        (username,
         "$2b$12$placeholderHashForTests000000000000000000000000000000",
         username.title(), "USER", str(uuid.uuid4())),
    )
    user_id = cur.fetchone()[0]
    cur.close()
    return user_id


def _insert_so(*, order_type="return", status="OPEN", warehouse_id=1,
               refund_so_id=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders (so_number, customer_name, status, "
        "warehouse_id, order_type, order_source, refund_so_id, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (f"SO-VOID-{uuid.uuid4().hex[:8]}", "Cust", status, warehouse_id,
         order_type, "web", refund_so_id, str(uuid.uuid4())),
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


def _insert_return_receipt(so_id, item_id, *, bin_id=3, warehouse_id=1, qty=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO item_receipts (so_id, item_id, quantity_received, "
        "bin_id, warehouse_id, received_by, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (so_id, item_id, qty, bin_id, warehouse_id, "op", str(uuid.uuid4())),
    )
    cur.close()


def _audit_void_count(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM audit_log WHERE entity_type = 'SO' "
        "AND entity_id = %s AND action_type = 'RETURN_VOID'",
        (so_id,),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


def _voided(db, so_id):
    return db.execute(
        sa_text("SELECT voided_at, voided_by FROM sales_orders WHERE so_id = :s"),
        {"s": so_id},
    ).fetchone()


class TestHappyPath:
    def test_open_unreceived_return_voids(self, _db_transaction):
        from services.sales_order_service import void_return_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(order_type="return", status="OPEN")

        result = void_return_order(db, so_id=so_id, username="op")

        assert result["already_voided"] is False
        assert result["audit_log_id"] is not None
        row = _voided(db, so_id)
        assert row.voided_at is not None
        assert row.voided_by == "op"
        assert _audit_void_count(so_id) == 1

    def test_second_void_is_idempotent_no_new_audit(self, _db_transaction):
        from services.sales_order_service import void_return_order
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(order_type="return", status="OPEN")

        void_return_order(db, so_id=so_id, username="op")
        again = void_return_order(db, so_id=so_id, username="op")

        assert again["already_voided"] is True
        assert again["audit_log_id"] is None
        assert _audit_void_count(so_id) == 1  # no second row


class TestGuards:
    def test_not_found(self, _db_transaction):
        from services.sales_order_service import (
            void_return_order, ReturnVoidNotAllowed,
        )
        db = _db_transaction
        with pytest.raises(ReturnVoidNotAllowed) as exc:
            void_return_order(db, so_id=999999999, username="op")
        assert exc.value.reason == "not_found"

    def test_non_return_rejected(self, _db_transaction):
        from services.sales_order_service import (
            void_return_order, ReturnVoidNotAllowed,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(order_type="sale", status="OPEN")
        with pytest.raises(ReturnVoidNotAllowed) as exc:
            void_return_order(db, so_id=so_id, username="op")
        assert exc.value.reason == "not_a_return"
        assert _voided(db, so_id).voided_at is None  # untouched

    def test_received_return_rejected(self, _db_transaction):
        from services.sales_order_service import (
            void_return_order, ReturnVoidNotAllowed,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(order_type="return", status="RECEIVED")
        with pytest.raises(ReturnVoidNotAllowed) as exc:
            void_return_order(db, so_id=so_id, username="op")
        assert exc.value.reason == "not_open"
        assert exc.value.current_status == "RECEIVED"

    def test_return_with_receipt_rejected(self, _db_transaction):
        from services.sales_order_service import (
            void_return_order, ReturnVoidNotAllowed,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(order_type="return", status="OPEN")
        item_id = _insert_item()
        _insert_return_receipt(so_id, item_id)  # goods already came back
        with pytest.raises(ReturnVoidNotAllowed) as exc:
            void_return_order(db, so_id=so_id, username="op")
        assert exc.value.reason == "has_receipts"
        assert _voided(db, so_id).voided_at is None

    def test_return_with_refund_link_rejected(self, _db_transaction):
        from services.sales_order_service import (
            void_return_order, ReturnVoidNotAllowed,
        )
        db = _db_transaction
        _ensure_user("op")
        refund_id = _insert_so(order_type="refund", status="OPEN")
        so_id = _insert_so(order_type="return", status="OPEN",
                           refund_so_id=refund_id)
        with pytest.raises(ReturnVoidNotAllowed) as exc:
            void_return_order(db, so_id=so_id, username="op")
        assert exc.value.reason == "has_refund"
        assert _voided(db, so_id).voided_at is None
