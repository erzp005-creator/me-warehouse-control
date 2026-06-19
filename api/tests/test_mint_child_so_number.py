"""Service-level tests for sales_order_service.mint_child_so_number.

Post-fulfillment children (replacement / exchange / return / refund) take a
readable so_number off the ORIGINAL's number with a per-type SUFFIX, and the
rare second child of a given (parent, order_type) increments. Setup uses
raw-cursor inserts so the per-test transaction owns every byte; the helper runs
on the same transaction via the SQLAlchemy connection (matching production's
g.db).
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

from db_test_context import get_raw_connection


def _insert_so(so_number, *, order_type="sale", parent_so_id=None,
               status="SHIPPED", warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, "
        " parent_so_id, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", status, warehouse_id, order_type,
         parent_so_id, str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


class TestMintChildSoNumber:
    def test_first_child_is_bare_suffix(self, _db_transaction):
        from services.sales_order_service import mint_child_so_number
        db = _db_transaction
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        parent_id = _insert_so(parent_no, order_type="sale")

        assert mint_child_so_number(
            db, parent_so_id=parent_id, parent_so_number=parent_no,
            order_type="replacement",
        ) == f"{parent_no}-REPLACEMENT"

    def test_second_child_increments(self, _db_transaction):
        from services.sales_order_service import mint_child_so_number
        db = _db_transaction
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        parent_id = _insert_so(parent_no, order_type="sale")
        # A first replacement already exists for this parent.
        _insert_so(f"{parent_no}-REPLACEMENT", order_type="replacement",
                   parent_so_id=parent_id)

        assert mint_child_so_number(
            db, parent_so_id=parent_id, parent_so_number=parent_no,
            order_type="replacement",
        ) == f"{parent_no}-REPLACEMENT-2"

    def test_suffix_is_per_order_type(self, _db_transaction):
        from services.sales_order_service import mint_child_so_number
        db = _db_transaction
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        parent_id = _insert_so(parent_no, order_type="sale")
        for order_type, suffix in (
            ("exchange", "EXCHANGE"),
            ("return", "RMA"),
            ("refund", "REFUND"),
        ):
            assert mint_child_so_number(
                db, parent_so_id=parent_id, parent_so_number=parent_no,
                order_type=order_type,
            ) == f"{parent_no}-{suffix}"

    def test_counts_are_isolated_per_type(self, _db_transaction):
        # An existing replacement must not bump the first exchange to -2.
        from services.sales_order_service import mint_child_so_number
        db = _db_transaction
        parent_no = f"POS-{uuid.uuid4().hex[:8]}"
        parent_id = _insert_so(parent_no, order_type="sale")
        _insert_so(f"{parent_no}-REPLACEMENT", order_type="replacement",
                   parent_so_id=parent_id)

        assert mint_child_so_number(
            db, parent_so_id=parent_id, parent_so_number=parent_no,
            order_type="exchange",
        ) == f"{parent_no}-EXCHANGE"

    def test_unsuffixed_order_type_raises(self, _db_transaction):
        from services.sales_order_service import mint_child_so_number
        db = _db_transaction
        with pytest.raises(ValueError):
            mint_child_so_number(
                db, parent_so_id=1, parent_so_number="POS-1", order_type="sale",
            )
