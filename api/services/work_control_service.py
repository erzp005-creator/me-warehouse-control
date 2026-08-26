"""Transactional task orchestration for ME Warehouse Control."""

import json

from sqlalchemy import text

from services.audit_service import write_audit_log


ACTIVE_STATUSES = ("CLAIMED", "IN_PROGRESS", "PAUSED")

_TASK_SELECT = """
    SELECT wt.task_id, wt.batch_id, wt.warehouse_id, wt.task_type, wt.status,
           wt.priority, wt.assigned_to, wt.claimed_by, wt.source_ref,
           wt.order_count, wt.sku_count, wt.unit_count, wt.complexity_note,
           wt.available_at, wt.claimed_at, wt.started_at, wt.completed_at,
           wt.active_seconds, wt.paused_seconds, wt.last_event_at,
           wt.created_at, wt.updated_at,
           wb.pack_note_ref, wb.platform, wb.source_system
      FROM work_tasks wt
      LEFT JOIN work_batches wb ON wb.batch_id = wt.batch_id
"""


def _iso(value):
    return value.isoformat() if value is not None else None


def serialize_task(row):
    if row is None:
        return None
    return {
        "task_id": row.task_id,
        "batch_id": row.batch_id,
        "warehouse_id": row.warehouse_id,
        "task_type": row.task_type,
        "status": row.status,
        "priority": row.priority,
        "assigned_to": row.assigned_to,
        "claimed_by": row.claimed_by,
        "source_ref": row.source_ref,
        "pack_note_ref": row.pack_note_ref,
        "platform": row.platform,
        "source_system": row.source_system,
        "order_count": row.order_count,
        "sku_count": row.sku_count,
        "unit_count": row.unit_count,
        "complexity_note": row.complexity_note,
        "available_at": _iso(row.available_at),
        "claimed_at": _iso(row.claimed_at),
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "active_seconds": row.active_seconds,
        "paused_seconds": row.paused_seconds,
        "last_event_at": _iso(row.last_event_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def get_task(db, task_id, *, for_update=False):
    suffix = " FOR UPDATE OF wt" if for_update else ""
    return db.execute(
        text(_TASK_SELECT + " WHERE wt.task_id = :tid" + suffix),
        {"tid": task_id},
    ).fetchone()


def get_current_task(db, username):
    return db.execute(
        text(
            _TASK_SELECT
            + " WHERE wt.claimed_by = :username "
              "AND wt.status IN ('CLAIMED','IN_PROGRESS','PAUSED') "
              "ORDER BY wt.task_id LIMIT 1"
        ),
        {"username": username},
    ).fetchone()


def record_task_event(db, task_id, event_type, username, *, reason_code=None,
                      notes=None, metadata=None, device_id=None):
    db.execute(
        text(
            """
            INSERT INTO work_task_events
                (task_id, event_type, user_id, reason_code, notes, metadata, device_id)
            VALUES
                (:task_id, :event_type, :user_id, :reason_code, :notes,
                 CAST(:metadata AS jsonb), :device_id)
            """
        ),
        {
            "task_id": task_id,
            "event_type": event_type,
            "user_id": username,
            "reason_code": reason_code,
            "notes": notes,
            "metadata": json.dumps(metadata) if metadata else None,
            "device_id": device_id,
        },
    )


def claim_next_task(db, warehouse_id, username, *, task_types=None, device_id=None):
    """Atomically claim one task, or return the user's existing active task.

    `FOR UPDATE SKIP LOCKED` allows many handhelds to ask for work at once
    without ever assigning the same task twice.
    """
    current = get_current_task(db, username)
    if current is not None:
        return current, False

    type_clause = ""
    params = {"wid": warehouse_id, "username": username}
    if task_types:
        type_clause = " AND candidate.task_type = ANY(:task_types)"
        params["task_types"] = list(task_types)

    candidate = db.execute(
        text(
            """
            SELECT candidate.task_id
              FROM work_tasks candidate
             WHERE candidate.warehouse_id = :wid
               AND candidate.status IN ('QUEUED','ASSIGNED')
               AND candidate.available_at <= NOW()
               AND (candidate.assigned_to IS NULL OR candidate.assigned_to = :username)
               AND NOT (
                    candidate.batch_id IS NOT NULL
                    AND candidate.task_type IN ('PICKING', 'PACKING')
                    AND EXISTS (
                        SELECT 1
                          FROM work_tasks counterpart
                         WHERE counterpart.batch_id = candidate.batch_id
                           AND counterpart.task_type IN ('PICKING', 'PACKING')
                           AND counterpart.task_type <> candidate.task_type
                           AND counterpart.claimed_by = :username
                    )
               )
            """
            + type_clause
            + """
             ORDER BY CASE WHEN candidate.assigned_to = :username THEN 0 ELSE 1 END,
                      candidate.priority DESC, candidate.available_at ASC,
                      candidate.task_id ASC
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """
        ),
        params,
    ).fetchone()
    if candidate is None:
        return None, False

    db.execute(
        text(
            """
            UPDATE work_tasks
               SET status = 'CLAIMED', claimed_by = :username,
                   claimed_at = NOW(), last_event_at = NOW(), updated_at = NOW()
             WHERE task_id = :tid
            """
        ),
        {"username": username, "tid": candidate.task_id},
    )
    record_task_event(
        db, candidate.task_id, "CLAIMED", username,
        metadata={"auto_assigned": True}, device_id=device_id,
    )
    claimed = get_task(db, candidate.task_id)
    if claimed.batch_id is not None:
        db.execute(
            text(
                "UPDATE work_batches SET status = 'IN_PROGRESS', updated_at = NOW() "
                "WHERE batch_id = :bid AND status = 'OPEN'"
            ),
            {"bid": claimed.batch_id},
        )
    write_audit_log(
        db, "WORK_TASK_CLAIMED", "WORK_TASK", candidate.task_id,
        username, warehouse_id,
        {"task_type": claimed.task_type, "batch_id": claimed.batch_id},
        device_id=device_id,
    )
    return get_task(db, candidate.task_id), True


def transition_task(db, task_id, username, action, *, is_admin=False,
                    reason_code=None, notes=None, device_id=None):
    task = get_task(db, task_id, for_update=True)
    if task is None:
        raise LookupError("Task not found")
    if not is_admin and task.claimed_by != username:
        raise PermissionError("Task is not claimed by this employee")

    transitions = {
        "START": ({"CLAIMED"}, "IN_PROGRESS", "STARTED"),
        "PAUSE": ({"IN_PROGRESS"}, "PAUSED", "PAUSED"),
        "RESUME": ({"PAUSED"}, "IN_PROGRESS", "RESUMED"),
        "COMPLETE": ({"IN_PROGRESS"}, "COMPLETED", "COMPLETED"),
        "CANCEL": ({"QUEUED", "ASSIGNED", "CLAIMED", "IN_PROGRESS", "PAUSED"}, "CANCELLED", "CANCELLED"),
    }
    if action == "EXCEPTION":
        if task.status not in ACTIVE_STATUSES:
            raise ValueError(f"Cannot record an exception while task is {task.status}")
        record_task_event(
            db, task_id, "EXCEPTION", username,
            reason_code=reason_code, notes=notes, device_id=device_id,
        )
        db.execute(
            text("UPDATE work_tasks SET last_event_at = NOW(), updated_at = NOW() WHERE task_id = :tid"),
            {"tid": task_id},
        )
        write_audit_log(
            db, "WORK_TASK_EXCEPTION", "WORK_TASK", task_id, username,
            task.warehouse_id,
            {"reason_code": reason_code, "notes": notes}, device_id=device_id,
        )
        return get_task(db, task_id)

    allowed, next_status, event_type = transitions[action]
    if task.status not in allowed:
        raise ValueError(f"Cannot {action.lower()} task while status is {task.status}")
    if action == "CANCEL" and not is_admin:
        raise PermissionError("Only an administrator can cancel a task")
    if action == "START" and task.task_type in ("PICKING", "PACKING"):
        verified = db.execute(
            text(
                """
                SELECT 1 FROM work_task_events
                 WHERE task_id = :tid AND event_type = 'VERIFIED'
                   AND user_id = :username
                 LIMIT 1
                """
            ),
            {"tid": task_id, "username": username},
        ).fetchone()
        if verified is None:
            raise ValueError("Scan one courier barcode from this Pack Note before starting")

    if action == "START":
        set_sql = """
            status = 'IN_PROGRESS', started_at = COALESCE(started_at, NOW()),
            active_started_at = NOW(), pause_started_at = NULL
        """
    elif action == "PAUSE":
        set_sql = """
            status = 'PAUSED',
            active_seconds = active_seconds + GREATEST(0, EXTRACT(EPOCH FROM (NOW() - active_started_at))::int),
            active_started_at = NULL, pause_started_at = NOW()
        """
    elif action == "RESUME":
        set_sql = """
            status = 'IN_PROGRESS',
            paused_seconds = paused_seconds + GREATEST(0, EXTRACT(EPOCH FROM (NOW() - pause_started_at))::int),
            pause_started_at = NULL, active_started_at = NOW()
        """
    elif action == "COMPLETE":
        set_sql = """
            status = 'COMPLETED', completed_at = NOW(),
            active_seconds = active_seconds + GREATEST(0, EXTRACT(EPOCH FROM (NOW() - active_started_at))::int),
            active_started_at = NULL, pause_started_at = NULL
        """
    else:  # CANCEL
        if task.status == "IN_PROGRESS":
            active_delta = "GREATEST(0, EXTRACT(EPOCH FROM (NOW() - active_started_at))::int)"
            pause_delta = "0"
        elif task.status == "PAUSED":
            active_delta = "0"
            pause_delta = "GREATEST(0, EXTRACT(EPOCH FROM (NOW() - pause_started_at))::int)"
        else:
            active_delta = pause_delta = "0"
        set_sql = f"""
            status = 'CANCELLED', completed_at = NOW(),
            active_seconds = active_seconds + {active_delta},
            paused_seconds = paused_seconds + {pause_delta},
            active_started_at = NULL, pause_started_at = NULL
        """

    db.execute(
        text(
            f"UPDATE work_tasks SET {set_sql}, last_event_at = NOW(), "
            "updated_at = NOW() WHERE task_id = :tid"
        ),
        {"tid": task_id},
    )
    record_task_event(
        db, task_id, event_type, username,
        reason_code=reason_code, notes=notes, device_id=device_id,
    )
    write_audit_log(
        db, f"WORK_TASK_{event_type}", "WORK_TASK", task_id, username,
        task.warehouse_id,
        {"from_status": task.status, "to_status": next_status,
         "reason_code": reason_code, "notes": notes},
        device_id=device_id,
    )

    if task.batch_id is not None and next_status in ("COMPLETED", "CANCELLED"):
        remaining = db.execute(
            text(
                "SELECT COUNT(*) FROM work_tasks WHERE batch_id = :bid "
                "AND status NOT IN ('COMPLETED','CANCELLED')"
            ),
            {"bid": task.batch_id},
        ).scalar()
        if remaining == 0:
            db.execute(
                text(
                    "UPDATE work_batches SET status = 'COMPLETED', updated_at = NOW() "
                    "WHERE batch_id = :bid"
                ),
                {"bid": task.batch_id},
            )
    return get_task(db, task_id)
