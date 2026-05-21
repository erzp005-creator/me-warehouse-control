"""mig 066: pick_tasks.{so_line_id, to_line_id} FK -> ON DELETE SET NULL.

Tests:

- Schema introspection: both FKs declare ON DELETE SET NULL exactly.
- Functional (so_line_id): deleting a sales_order_line with attached
  pick_tasks in every terminal status (PICKED, RELEASED, SHORT,
  SKIPPED, PENDING) succeeds; each pick_task survives with
  so_line_id=NULL and every other audit field intact.
- Functional (to_line_id): same for transfer_order_lines.
- Negative-control: the audit kernel survives line deletion.
  pick_task_id, batch_id, so_id (still set by check constraint),
  item_id, bin_id, quantity_to_pick, quantity_picked, status,
  picked_by, picked_at all preserved post-DELETE.
- Regression for the partial-fulfill case: when partial-fulfill
  shrinks a SOL to zero by DELETE, a RELEASED pick_task no longer
  blocks the operation -- the FK SET NULL fires and the DELETE
  proceeds.
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


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _insert_so(status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders (so_number, customer_name, status, "
        "warehouse_id, external_id) "
        "VALUES (%s, %s, %s, 1, %s) RETURNING so_id",
        (f"SO-FK-{uuid.uuid4().hex[:8]}", "Cust", status, str(uuid.uuid4())),
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
        (f"SKU-FK-{uuid.uuid4().hex[:8]}", "W", "0123456789012",
         str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, qty=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, "
        " line_number, status) "
        "VALUES (%s, %s, %s, %s, 1, 'PENDING') RETURNING so_line_id",
        (so_id, item_id, qty, qty),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _insert_pick_task(so_id, sol_id, item_id, bin_id, status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, 1, 'OPEN') RETURNING batch_id",
        (f"BATCH-FK-{uuid.uuid4().hex[:8]}",),
    )
    batch_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, "
        "bin_id, quantity_to_pick, quantity_picked, status, pick_sequence, "
        "picked_by, picked_at) "
        "VALUES (%s, %s, %s, %s, %s, 2, 2, %s, 1, 'operator-fk', NOW()) "
        "RETURNING pick_task_id",
        (batch_id, so_id, sol_id, item_id, bin_id, status),
    )
    pt_id = cur.fetchone()[0]
    cur.close()
    return pt_id, batch_id


def _insert_to(source_wh=1, dest_wh=2):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transfer_orders "
        "(to_number, source_warehouse_id, destination_warehouse_id, "
        " created_by, external_id) "
        "VALUES (%s, %s, %s, 'fk-test', %s) RETURNING to_id",
        (f"TO-FK-{uuid.uuid4().hex[:8]}", source_wh, dest_wh,
         str(uuid.uuid4())),
    )
    to_id = cur.fetchone()[0]
    cur.close()
    return to_id


def _insert_to_line(to_id, item_id, qty=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transfer_order_lines (to_id, item_id, line_number, "
        "requested_qty) VALUES (%s, %s, 1, %s) RETURNING to_line_id",
        (to_id, item_id, qty),
    )
    tol_id = cur.fetchone()[0]
    cur.close()
    return tol_id


def _insert_to_pick_task(to_id, tol_id, item_id, bin_id, status="PICKED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, 1, 'OPEN') RETURNING batch_id",
        (f"BATCH-TO-FK-{uuid.uuid4().hex[:8]}",),
    )
    batch_id = cur.fetchone()[0]
    # pick_tasks_target_xor requires so_id IS NULL when to_id IS NOT NULL.
    cur.execute(
        "INSERT INTO pick_tasks (batch_id, to_id, to_line_id, item_id, "
        "bin_id, quantity_to_pick, quantity_picked, status, pick_sequence, "
        "picked_by, picked_at) "
        "VALUES (%s, %s, %s, %s, %s, 2, 2, %s, 1, 'operator-fk', NOW()) "
        "RETURNING pick_task_id",
        (batch_id, to_id, tol_id, item_id, bin_id, status),
    )
    pt_id = cur.fetchone()[0]
    cur.close()
    return pt_id


# ----------------------------------------------------------------------
# Schema introspection
# ----------------------------------------------------------------------


class TestSchemaIntrospection:
    def test_so_line_id_fk_has_on_delete_set_null(self, _db_transaction):
        defn = _db_transaction.execute(
            sa_text(
                "SELECT pg_get_constraintdef(oid) AS defn "
                "  FROM pg_constraint "
                " WHERE conrelid = 'pick_tasks'::regclass "
                "   AND conname = 'pick_tasks_so_line_id_fkey'"
            )
        ).fetchone().defn
        assert "ON DELETE SET NULL" in defn

    def test_to_line_id_fk_has_on_delete_set_null(self, _db_transaction):
        defn = _db_transaction.execute(
            sa_text(
                "SELECT pg_get_constraintdef(oid) AS defn "
                "  FROM pg_constraint "
                " WHERE conrelid = 'pick_tasks'::regclass "
                "   AND conname = 'pick_tasks_to_line_id_fkey'"
            )
        ).fetchone().defn
        assert "ON DELETE SET NULL" in defn

    def test_parent_so_id_to_id_stay_restrict(self, _db_transaction):
        """The parent-aggregate FKs are intentionally NOT SET NULL.
        The pick_tasks_target_xor check requires exactly one of
        so_id / to_id to be non-NULL, so cascading either to NULL
        would break the check on any cross-deletion. Parents are
        also never hard-deleted in normal operation (cancel flips
        status; no DELETE on sales_orders / transfer_orders)."""
        defs = _db_transaction.execute(
            sa_text(
                "SELECT conname, pg_get_constraintdef(oid) AS defn "
                "  FROM pg_constraint "
                " WHERE conrelid = 'pick_tasks'::regclass "
                "   AND conname IN ('pick_tasks_so_id_fkey', "
                "                   'pick_tasks_to_id_fkey')"
            )
        ).fetchall()
        for row in defs:
            assert "ON DELETE" not in row.defn, (
                f"{row.conname} unexpectedly has cascade behaviour: {row.defn}"
            )


# ----------------------------------------------------------------------
# Functional: deleting a sales_order_line cascades so_line_id to NULL
# ----------------------------------------------------------------------


class TestSoLineDeleteCascadesToNull:
    @pytest.mark.parametrize(
        "task_status",
        ["PENDING", "PICKED", "SHORT", "SKIPPED", "RELEASED"],
    )
    def test_sol_delete_nulls_so_line_id(self, task_status, _db_transaction):
        item_id = _insert_item()
        so_id = _insert_so()
        sol_id = _insert_so_line(so_id, item_id)
        pt_id, _ = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                       status=task_status)

        # The DELETE must succeed regardless of pick_task status.
        _db_transaction.execute(
            sa_text("DELETE FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        )

        row = _db_transaction.execute(
            sa_text(
                "SELECT so_line_id, status, so_id, item_id, bin_id, "
                "       quantity_picked, picked_by, picked_at "
                "  FROM pick_tasks WHERE pick_task_id = :p"
            ),
            {"p": pt_id},
        ).fetchone()
        # so_line_id was nulled by the FK ON DELETE SET NULL.
        assert row.so_line_id is None
        # Status preserved (the cascade only touches the FK column).
        assert row.status == task_status
        # Audit kernel intact.
        assert row.so_id == so_id
        assert row.item_id == item_id
        assert row.bin_id == 3
        assert row.quantity_picked == 2
        assert row.picked_by == "operator-fk"
        assert row.picked_at is not None


# ----------------------------------------------------------------------
# Functional: deleting a transfer_order_line cascades to_line_id to NULL
# ----------------------------------------------------------------------


class TestToLineDeleteCascadesToNull:
    def test_tol_delete_nulls_to_line_id(self, _db_transaction):
        item_id = _insert_item()
        to_id = _insert_to(source_wh=1, dest_wh=2)
        tol_id = _insert_to_line(to_id, item_id)
        pt_id = _insert_to_pick_task(to_id, tol_id, item_id, bin_id=3,
                                      status="PICKED")

        _db_transaction.execute(
            sa_text("DELETE FROM transfer_order_lines WHERE to_line_id = :t"),
            {"t": tol_id},
        )

        row = _db_transaction.execute(
            sa_text(
                "SELECT to_line_id, status, to_id, item_id, bin_id, "
                "       quantity_picked, picked_by "
                "  FROM pick_tasks WHERE pick_task_id = :p"
            ),
            {"p": pt_id},
        ).fetchone()
        assert row.to_line_id is None
        # Audit kernel still intact.
        assert row.to_id == to_id
        assert row.status == "PICKED"
        assert row.quantity_picked == 2
        assert row.picked_by == "operator-fk"


# ----------------------------------------------------------------------
# Regression: the exact bug that prompted mig 066
# ----------------------------------------------------------------------


class TestPartialFulfillRegressionPriorMig:
    def test_released_pick_task_does_not_block_sol_delete(self, _db_transaction):
        """Before mig 066 this DELETE failed with:

            psycopg2.errors.ForeignKeyViolation:
              update or delete on table "sales_order_lines" violates
              foreign key constraint "pick_tasks_so_line_id_fkey"

        which surfaced through partial-fulfill as
        integrity_constraint_violation when an operator tried to
        short a line that the so-refinement revert flow had previously
        released. The test stages that exact precondition and asserts
        the DELETE now succeeds."""
        item_id = _insert_item()
        so_id = _insert_so()
        sol_id = _insert_so_line(so_id, item_id)
        pt_id, _ = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                       status="RELEASED")

        _db_transaction.execute(
            sa_text("DELETE FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        )

        # The line is gone; the pick_task survives with so_line_id NULL.
        sol_exists = _db_transaction.execute(
            sa_text("SELECT 1 FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert sol_exists is None
        pt_row = _db_transaction.execute(
            sa_text("SELECT so_line_id, status FROM pick_tasks "
                    "WHERE pick_task_id = :p"),
            {"p": pt_id},
        ).fetchone()
        assert pt_row.so_line_id is None
        assert pt_row.status == "RELEASED"
