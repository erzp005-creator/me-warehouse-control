"""A completed TO pick batch must auto-submit the transfer
order.

complete_batch historically only handled sales orders, so when a TO
pick batch finished the transfer order was left stranded at
PARTIALLY_PICKED with no approval row -- and because the batch was now
COMPLETED the handheld looped on "Transfer order not started." These
tests pin the auto-submit behaviour the TO branch in complete_batch
adds.

The submit route refactor (submit logic extracted into
transfer_order_service.submit_picks) is covered by TestSubmit in
test_to_submit_approve_reject.py.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context
from services.picking_service import complete_batch  # noqa: E402


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _seed_to(source_wh=1, dest_wh=2, status="PARTIALLY_PICKED"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_orders "
            "(to_number, source_warehouse_id, destination_warehouse_id, "
            " status, created_by, external_id) "
            "VALUES (%s, %s, %s, %s, 't79', %s) RETURNING to_id",
            (f"TO-T79-{uuid.uuid4().hex[:8]}", source_wh, dest_wh, status,
             str(uuid.uuid4())),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _seed_to_line(to_id, item_id=1, line_number=1, committed=5, picked=5,
                  approved=0, status="PICKED"):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transfer_order_lines "
            "(to_id, item_id, line_number, requested_qty, committed_qty, "
            " picked_qty, approved_qty, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING to_line_id",
            (to_id, item_id, line_number, committed, committed, picked,
             approved, status),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _provision_picked_batch(to_id, to_line_ids, assigned_to="picker",
                            warehouse_id=1, bin_id=2):
    """A pick_batches row + non-PENDING pick_tasks carrying to_id /
    to_line_id (so_id NULL) -- the state right after a picker finishes a
    TO batch and is about to complete it. Mirrors admin start-picking,
    but tasks are already PICKED."""
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO pick_batches "
            "(batch_number, warehouse_id, status, assigned_to) "
            "VALUES (%s, %s, 'IN_PROGRESS', %s) RETURNING batch_id",
            (f"BATCH-T79-{uuid.uuid4().hex[:8]}", warehouse_id, assigned_to),
        )
        batch_id = cur.fetchone()[0]
        for seq, to_line_id in enumerate(to_line_ids, 1):
            cur.execute(
                "SELECT item_id, picked_qty FROM transfer_order_lines "
                " WHERE to_line_id = %s",
                (to_line_id,),
            )
            item_id, picked = cur.fetchone()
            cur.execute(
                "INSERT INTO pick_tasks "
                "(batch_id, to_id, to_line_id, item_id, bin_id, "
                " quantity_to_pick, quantity_picked, pick_sequence, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PICKED')",
                (batch_id, to_id, to_line_id, item_id, bin_id, picked,
                 picked, seq),
            )
        return batch_id
    finally:
        cur.close()


def _open_session():
    """Bound to the conftest test transaction; complete_batch commits
    internally so the savepoint flushes into the outer transaction the
    conftest rolls back at end-of-test."""
    import models.database as db
    return db.SessionLocal()


class TestCompleteBatchAutoSubmit:
    def test_fully_picked_to_batch_advances_to_awaiting_approval(self):
        to_id = _seed_to(status="PARTIALLY_PICKED")
        line1 = _seed_to_line(to_id, item_id=1, line_number=1,
                              committed=5, picked=5, status="PICKED")
        line2 = _seed_to_line(to_id, item_id=2, line_number=2,
                              committed=3, picked=3, status="PICKED")
        batch_id = _provision_picked_batch(to_id, [line1, line2])

        session = _open_session()
        try:
            complete_batch(session, batch_id, "picker")
        finally:
            session.close()

        # Header advanced out of PARTIALLY_PICKED on its own.
        status = _query(
            "SELECT status FROM transfer_orders WHERE to_id = %s", (to_id,),
        )[0][0]
        assert status == "AWAITING_APPROVAL"

        # Exactly one PENDING approval snapshotting both lines' picks.
        approvals = _query(
            "SELECT status, lines_snapshot FROM transfer_order_approvals "
            " WHERE to_id = %s", (to_id,),
        )
        assert len(approvals) == 1
        assert approvals[0][0] == "PENDING"
        snap = {ln["to_line_id"]: ln["picked_in_snapshot"]
                for ln in approvals[0][1]["lines"]}
        assert snap == {line1: 5, line2: 3}

        # Submit audit row written under the TO entity.
        audit = _query(
            "SELECT 1 FROM audit_log "
            " WHERE entity_type = 'TO' AND entity_id = %s "
            "   AND action_type = 'TO_SUBMITTED'",
            (to_id,),
        )
        assert audit

        # Batch itself marked completed.
        bstatus = _query(
            "SELECT status FROM pick_batches WHERE batch_id = %s", (batch_id,),
        )[0][0]
        assert bstatus == "COMPLETED"

    def test_partially_picked_to_batch_stays_partially_picked(self):
        # One line fully picked, one still short with no short-close: a
        # legitimate state for a TO (nothing ships). Auto-submit must
        # snapshot the picked units without forcing AWAITING_APPROVAL and
        # without blocking batch completion.
        to_id = _seed_to(status="PARTIALLY_PICKED")
        line1 = _seed_to_line(to_id, item_id=1, line_number=1,
                              committed=5, picked=5, status="PICKED")
        line2 = _seed_to_line(to_id, item_id=2, line_number=2,
                              committed=4, picked=2, status="PARTIALLY_PICKED")
        batch_id = _provision_picked_batch(to_id, [line1, line2])

        session = _open_session()
        try:
            complete_batch(session, batch_id, "picker")
        finally:
            session.close()

        status = _query(
            "SELECT status FROM transfer_orders WHERE to_id = %s", (to_id,),
        )[0][0]
        assert status == "PARTIALLY_PICKED"

        approvals = _query(
            "SELECT lines_snapshot FROM transfer_order_approvals "
            " WHERE to_id = %s", (to_id,),
        )
        assert len(approvals) == 1
        snap = {ln["to_line_id"]: ln["picked_in_snapshot"]
                for ln in approvals[0][0]["lines"]}
        assert snap == {line1: 5, line2: 2}
