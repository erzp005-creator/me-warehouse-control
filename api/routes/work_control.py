"""ME Warehouse Control API.

The routes in this blueprint supervise people and work. They intentionally do
not post inventory, close canonical orders, or create accounting entries.
"""

import hashlib
import io
import math
import uuid
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request, send_file
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
    require_wms_token,
)
from middleware.db import with_db
from schemas.work_control import (
    ClaimNextTaskRequest,
    CreateWorkSkuRequest,
    CreateBatchRequest,
    CreateErrorRequest,
    CreateReceivingDraftRequest,
    CreateTaskRequest,
    ReviewErrorRequest,
    ReviewReceivingDraftRequest,
    SiteGiantWorkloadSnapshotRequest,
    SiteGiantSkuSyncRequest,
    SubmitReceivingDraftRequest,
    TaskTransitionRequest,
    VerifyTaskScanRequest,
)
from services.audit_service import write_audit_log
from services.evidence_storage import EvidenceUnavailableError, evidence_storage
from services.work_control_service import (
    claim_next_task,
    get_current_task,
    get_task,
    record_task_event,
    serialize_task,
    transition_task,
)
from utils.validation import validate_body


work_control_bp = Blueprint("work_control", __name__)


def _username():
    return g.current_user["username"]


def _is_admin():
    return g.current_user.get("role") == "ADMIN"


def _can_supervise():
    return _is_admin() or "work-control" in (g.current_user.get("allowed_pages") or [])


def _can_receive():
    return _is_admin() or "receive" in (g.current_user.get("allowed_functions") or [])


_FUNCTION_TASK_TYPES = {
    "pick": "PICKING",
    "pack": "PACKING",
    "receive": "RECEIVING",
    "putaway": "PUTAWAY",
    "count": "STOCK_CHECK",
}

_PACK_NOTE_CAPACITY = 50
_FORECAST_DEFAULT_MINUTES_PER_50 = {
    "PICKING": 30.0,
    "PACKING": 40.0,
}
_FORECAST_SETTING_KEYS = {
    "PICKING": "work_control_picking_minutes_per_50",
    "PACKING": "work_control_packing_minutes_per_50",
}
_FORECAST_MIN_HISTORY_TASKS = 5
_FORECAST_MIN_HISTORY_ORDERS = 100


def _forecast_setting(value, fallback):
    """Parse a supervisor forecast setting without letting bad data break the queue."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if 1 <= parsed <= 240 else fallback


def _workload_forecast(db, warehouse_id, unprinted_packages):
    """Estimate Pack Note work without creating tasks before SiteGiant prints.

    Baselines are supervisor-adjustable. Once a stage has enough real completed
    work, a median normalized batch duration replaces its baseline. Tasks under
    one active minute are excluded so setup/simulation records cannot calibrate
    live forecasts.
    """
    setting_rows = db.execute(
        text(
            """
            SELECT key, value FROM app_settings
             WHERE key IN (:picking_key, :packing_key)
            """
        ),
        {
            "picking_key": _FORECAST_SETTING_KEYS["PICKING"],
            "packing_key": _FORECAST_SETTING_KEYS["PACKING"],
        },
    ).mappings().all()
    settings = {row["key"]: row["value"] for row in setting_rows}

    history_rows = db.execute(
        text(
            """
            SELECT task_type,
                   COUNT(*) AS completed_tasks,
                   COALESCE(SUM(order_count), 0) AS completed_orders,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY active_seconds * 50.0 / NULLIF(order_count, 0)
                   ) / 60.0 AS median_minutes_per_50
              FROM work_tasks
             WHERE warehouse_id = :warehouse_id
               AND task_type IN ('PICKING', 'PACKING')
               AND status = 'COMPLETED'
               AND completed_at >= NOW() - INTERVAL '30 days'
               AND order_count > 0
               AND active_seconds >= 60
             GROUP BY task_type
            """
        ),
        {"warehouse_id": warehouse_id},
    ).mappings().all()
    history = {row["task_type"]: row for row in history_rows}

    rates = {}
    for task_type in ("PICKING", "PACKING"):
        baseline = _forecast_setting(
            settings.get(_FORECAST_SETTING_KEYS[task_type]),
            _FORECAST_DEFAULT_MINUTES_PER_50[task_type],
        )
        sample = history.get(task_type)
        sample_tasks = int(sample["completed_tasks"] or 0) if sample else 0
        sample_orders = int(sample["completed_orders"] or 0) if sample else 0
        use_history = bool(
            sample
            and sample_tasks >= _FORECAST_MIN_HISTORY_TASKS
            and sample_orders >= _FORECAST_MIN_HISTORY_ORDERS
            and sample["median_minutes_per_50"] is not None
        )
        minutes_per_50 = (
            float(sample["median_minutes_per_50"])
            if use_history else baseline
        )
        rates[task_type] = {
            "minutes_per_50": round(minutes_per_50, 1),
            "source": "recent_history" if use_history else "baseline",
            "sample_tasks": sample_tasks,
            "sample_orders": sample_orders,
        }

    package_count = max(0, int(unprinted_packages or 0))
    batch_sizes = [
        min(_PACK_NOTE_CAPACITY, package_count - offset)
        for offset in range(0, package_count, _PACK_NOTE_CAPACITY)
    ]
    pick_rate = rates["PICKING"]["minutes_per_50"]
    pack_rate = rates["PACKING"]["minutes_per_50"]
    pick_minutes = package_count * pick_rate / _PACK_NOTE_CAPACITY
    pack_minutes = package_count * pack_rate / _PACK_NOTE_CAPACITY

    # Two-stage flow-shop estimate: packing batch N can begin only after that
    # batch is picked and the packer has finished batch N-1.
    pick_finished = 0.0
    pack_finished = 0.0
    for batch_size in batch_sizes:
        pick_finished += pick_rate * batch_size / _PACK_NOTE_CAPACITY
        pack_finished = max(pick_finished, pack_finished)
        pack_finished += pack_rate * batch_size / _PACK_NOTE_CAPACITY

    return {
        "unprinted_packages": package_count,
        "pack_note_capacity": _PACK_NOTE_CAPACITY,
        "estimated_pack_notes": math.ceil(package_count / _PACK_NOTE_CAPACITY),
        "estimated_picking_minutes": math.ceil(pick_minutes),
        "estimated_packing_minutes": math.ceil(pack_minutes),
        "estimated_total_labor_minutes": math.ceil(pick_minutes + pack_minutes),
        "estimated_one_picker_one_packer_minutes": math.ceil(pack_finished),
        "rates": rates,
        "history_threshold": {
            "completed_tasks": _FORECAST_MIN_HISTORY_TASKS,
            "completed_orders": _FORECAST_MIN_HISTORY_ORDERS,
            "lookback_days": 30,
        },
    }


def _serialize_workload_snapshot(row):
    remaining = int(row.pending_packages) + int(row.to_process_packages)
    visible_total = (
        remaining
        + int(row.printed_packages)
        + int(row.pending_pickup_packages)
    )
    return {
        "snapshot_id": row.snapshot_id,
        "warehouse_id": row.warehouse_id,
        "captured_at": row.captured_at.isoformat(),
        "period_start": row.period_start.isoformat() if row.period_start else None,
        "period_end": row.period_end.isoformat() if row.period_end else None,
        "period_label": row.period_label,
        "pending_packages": row.pending_packages,
        "to_process_packages": row.to_process_packages,
        "printed_packages": row.printed_packages,
        "pending_pickup_packages": row.pending_pickup_packages,
        "dashboard_order_count": row.dashboard_order_count,
        "remaining_packages": remaining,
        "visible_total_packages": visible_total,
        "unprocessed_percent": round(remaining * 100 / visible_total, 1)
        if visible_total else 0.0,
    }


def _authorized_task_types(db, requested=None):
    """Return task types this employee may claim from their live grants."""
    if _is_admin():
        return requested
    row = db.execute(
        text("SELECT allowed_functions FROM users WHERE user_id = :uid"),
        {"uid": g.current_user["user_id"]},
    ).fetchone()
    functions = set(row.allowed_functions or []) if row else set()
    allowed = {_FUNCTION_TASK_TYPES[key] for key in functions if key in _FUNCTION_TASK_TYPES}
    if requested is None:
        return sorted(allowed)
    denied = set(requested) - allowed
    if denied:
        raise PermissionError(f"Task type is not granted: {', '.join(sorted(denied))}")
    return requested


def _task_access(task):
    if task is None:
        return False, (jsonify({"error": "Task not found"}), 404)
    return check_warehouse_access(task.warehouse_id)


def _serialize_batch(row, orders=None, tasks=None):
    return {
        "batch_id": row.batch_id,
        "warehouse_id": row.warehouse_id,
        "source_system": row.source_system,
        "pack_note_ref": row.pack_note_ref,
        "platform": row.platform,
        "order_count": row.declared_order_count,
        "priority": row.priority,
        "status": row.status,
        "available_at": row.available_at.isoformat(),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "orders": orders or [],
        "tasks": tasks or [],
    }


def _load_batch(db, batch_id):
    return db.execute(
        text("SELECT * FROM work_batches WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).fetchone()


def _load_batch_orders(db, batch_id):
    rows = db.execute(
        text(
            """
            SELECT batch_order_id, order_number, courier_barcode, platform,
                   sku_count, unit_count, created_at
              FROM work_batch_orders
             WHERE batch_id = :bid
             ORDER BY batch_order_id
            """
        ),
        {"bid": batch_id},
    ).fetchall()
    return [
        {
            "batch_order_id": r.batch_order_id,
            "order_number": r.order_number,
            "courier_barcode": r.courier_barcode,
            "platform": r.platform,
            "sku_count": r.sku_count,
            "unit_count": r.unit_count,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Batches and scan lookup
# ---------------------------------------------------------------------------


@work_control_bp.route("/batches", methods=["POST"])
@require_auth
@require_admin_or_page_permission("work-control")
@validate_body(CreateBatchRequest)
@with_db
def create_batch(validated):
    if not _is_admin():
        return jsonify({"error": "Administrator access required"}), 403
    username = _username()
    existing = g.db.execute(
        text(
            """
            SELECT batch_id FROM work_batches
             WHERE warehouse_id = :wid AND source_system = :source
               AND pack_note_ref = :ref
            """
        ),
        {
            "wid": validated.warehouse_id,
            "source": validated.source_system,
            "ref": validated.pack_note_ref,
        },
    ).fetchone()
    if existing:
        return jsonify({"error": "batch_already_exists", "batch_id": existing.batch_id}), 409

    warehouse = g.db.execute(
        text("SELECT warehouse_id FROM warehouses WHERE warehouse_id = :wid"),
        {"wid": validated.warehouse_id},
    ).fetchone()
    if warehouse is None:
        return jsonify({"error": "Warehouse not found"}), 404

    try:
        batch = g.db.execute(
            text(
                """
                INSERT INTO work_batches
                    (warehouse_id, source_system, pack_note_ref, platform,
                     priority, declared_order_count, created_by)
                VALUES (:wid, :source, :ref, :platform, :priority,
                        :declared_order_count, :created_by)
                RETURNING *
                """
            ),
            {
                "wid": validated.warehouse_id,
                "source": validated.source_system,
                "ref": validated.pack_note_ref,
                "platform": validated.platform,
                "priority": validated.priority,
                "declared_order_count": (
                    validated.declared_order_count or len(validated.orders)
                ),
                "created_by": username,
            },
        ).fetchone()
        for order in validated.orders:
            g.db.execute(
                text(
                    """
                    INSERT INTO work_batch_orders
                        (batch_id, order_number, courier_barcode, platform,
                         sku_count, unit_count)
                    VALUES (:bid, :order_number, :barcode, :platform,
                            :sku_count, :unit_count)
                    """
                ),
                {
                    "bid": batch.batch_id,
                    "order_number": order.order_number,
                    "barcode": order.courier_barcode,
                    "platform": order.platform or validated.platform,
                    "sku_count": order.sku_count,
                    "unit_count": order.unit_count,
                },
            )

        order_count = validated.declared_order_count or len(validated.orders)
        sku_count = sum(o.sku_count for o in validated.orders)
        unit_count = sum(o.unit_count for o in validated.orders)
        task_ids = []
        for task_type in validated.task_types:
            task = g.db.execute(
                text(
                    """
                    INSERT INTO work_tasks
                        (batch_id, warehouse_id, task_type, priority, source_ref,
                         idempotency_key, order_count, sku_count, unit_count,
                         created_by)
                    VALUES (:bid, :wid, :task_type, :priority, :source_ref,
                            :idempotency_key, :order_count, :sku_count,
                            :unit_count, :created_by)
                    RETURNING task_id
                    """
                ),
                {
                    "bid": batch.batch_id,
                    "wid": batch.warehouse_id,
                    "task_type": task_type,
                    "priority": batch.priority,
                    "source_ref": batch.pack_note_ref,
                    "idempotency_key": f"batch:{batch.batch_id}:{task_type}:v1",
                    "order_count": order_count,
                    "sku_count": sku_count,
                    "unit_count": unit_count,
                    "created_by": username,
                },
            ).fetchone()
            task_ids.append(task.task_id)
            record_task_event(
                g.db, task.task_id, "CREATED", username,
                metadata={"batch_id": batch.batch_id, "task_type": task_type},
            )
        write_audit_log(
            g.db, "WORK_BATCH_CREATED", "WORK_BATCH", batch.batch_id,
            username, batch.warehouse_id,
            {"pack_note_ref": batch.pack_note_ref, "order_count": order_count,
             "task_ids": task_ids},
        )
        g.db.commit()
    except IntegrityError:
        g.db.rollback()
        return jsonify({"error": "batch_constraint_violation"}), 409

    task_rows = [serialize_task(get_task(g.db, tid)) for tid in task_ids]
    return jsonify(
        _serialize_batch(batch, _load_batch_orders(g.db, batch.batch_id), task_rows)
    ), 201


@work_control_bp.route("/batches", methods=["GET"])
@require_auth
@require_admin_or_page_permission("work-control")
@with_db
def list_batches():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    status = request.args.get("status")
    clauses = ["wb.warehouse_id = :wid"]
    params = {"wid": warehouse_id}
    if status:
        clauses.append("wb.status = :status")
        params["status"] = status.upper()
    rows = g.db.execute(
        text(
            """
            SELECT wb.*,
                   COALESCE(
                       wb.declared_order_count,
                       COUNT(DISTINCT wbo.batch_order_id)::int
                   ) AS order_count,
                   COUNT(DISTINCT wt.task_id) AS task_count,
                   COUNT(DISTINCT wt.task_id) FILTER (
                       WHERE wt.status = 'COMPLETED'
                   ) AS completed_task_count
              FROM work_batches wb
              LEFT JOIN work_batch_orders wbo ON wbo.batch_id = wb.batch_id
              LEFT JOIN work_tasks wt ON wt.batch_id = wb.batch_id
             WHERE """
            + " AND ".join(clauses)
            + """
             GROUP BY wb.batch_id
             ORDER BY CASE wb.status
                          WHEN 'IN_PROGRESS' THEN 0 WHEN 'OPEN' THEN 1 ELSE 2
                      END,
                      wb.priority DESC, wb.created_at DESC
             LIMIT 500
            """
        ),
        params,
    ).mappings().all()
    return jsonify({"batches": [
        {
            **{
                key: (value.isoformat() if hasattr(value, "isoformat") else value)
                for key, value in row.items()
            },
            "order_count": int(row["order_count"] or 0),
            "task_count": int(row["task_count"] or 0),
            "completed_task_count": int(row["completed_task_count"] or 0),
        }
        for row in rows
    ]})


@work_control_bp.route("/batches/<int:batch_id>", methods=["GET"])
@require_auth
@with_db
def get_batch(batch_id):
    batch = _load_batch(g.db, batch_id)
    if batch is None:
        return jsonify({"error": "Batch not found"}), 404
    allowed, response = check_warehouse_access(batch.warehouse_id)
    if not allowed:
        return response
    task_rows = g.db.execute(
        text("SELECT task_id FROM work_tasks WHERE batch_id = :bid ORDER BY task_id"),
        {"bid": batch_id},
    ).fetchall()
    return jsonify(_serialize_batch(
        batch,
        _load_batch_orders(g.db, batch_id),
        [serialize_task(get_task(g.db, r.task_id)) for r in task_rows],
    ))


@work_control_bp.route("/scan/<barcode>", methods=["GET"])
@require_auth
@with_db
def scan_batch(barcode):
    candidates = g.db.execute(
        text(
            """
            SELECT wb.*
              FROM work_batches wb
             WHERE (
                    wb.pack_note_ref = :barcode
                    OR EXISTS (
                        SELECT 1
                          FROM work_batch_orders wbo
                         WHERE wbo.batch_id = wb.batch_id
                           AND (
                               wbo.courier_barcode = :barcode
                               OR wbo.order_number = :barcode
                           )
                    )
               )
               AND wb.status <> 'CANCELLED'
             ORDER BY CASE WHEN wb.status IN ('OPEN','IN_PROGRESS') THEN 0 ELSE 1 END,
                      wb.created_at DESC
             LIMIT 2
            """
        ),
        {"barcode": barcode},
    ).fetchall()
    if not candidates:
        return jsonify({"error": "Batch not found for scanned barcode"}), 404
    batch = candidates[0]
    allowed, response = check_warehouse_access(batch.warehouse_id)
    if not allowed:
        return response
    if len(candidates) > 1 and candidates[0].status in ("OPEN", "IN_PROGRESS") \
            and candidates[1].status in ("OPEN", "IN_PROGRESS"):
        return jsonify({"error": "ambiguous_barcode", "barcode": barcode}), 409
    task_rows = g.db.execute(
        text("SELECT task_id FROM work_tasks WHERE batch_id = :bid ORDER BY task_id"),
        {"bid": batch.batch_id},
    ).fetchall()
    return jsonify({
        "scanned_barcode": barcode,
        "batch": _serialize_batch(
            batch,
            _load_batch_orders(g.db, batch.batch_id),
            [serialize_task(get_task(g.db, r.task_id)) for r in task_rows],
        ),
    })


# ---------------------------------------------------------------------------
# Task queue and employee transitions
# ---------------------------------------------------------------------------


@work_control_bp.route("/tasks", methods=["POST"])
@require_auth
@require_admin_or_page_permission("work-control")
@validate_body(CreateTaskRequest)
@with_db
def create_task(validated):
    if not _is_admin():
        return jsonify({"error": "Administrator access required"}), 403
    username = _username()
    if validated.batch_id is not None:
        batch = _load_batch(g.db, validated.batch_id)
        if batch is None:
            return jsonify({"error": "Batch not found"}), 404
        if batch.warehouse_id != validated.warehouse_id:
            return jsonify({"error": "Batch belongs to another warehouse"}), 400
    try:
        row = g.db.execute(
            text(
                """
                INSERT INTO work_tasks
                    (batch_id, warehouse_id, task_type, priority, assigned_to,
                     source_ref, order_count, sku_count, unit_count,
                     complexity_note, idempotency_key, created_by, status)
                VALUES (:batch_id, :wid, :task_type, :priority, :assigned_to,
                        :source_ref, :order_count, :sku_count, :unit_count,
                        :complexity_note, :idempotency_key, :created_by,
                        CASE WHEN :assigned_to IS NULL THEN 'QUEUED' ELSE 'ASSIGNED' END)
                RETURNING task_id
                """
            ),
            {**validated.model_dump(), "wid": validated.warehouse_id,
             "created_by": username},
        ).fetchone()
        record_task_event(
            g.db, row.task_id,
            "ASSIGNED" if validated.assigned_to else "CREATED",
            username,
            metadata={"assigned_to": validated.assigned_to},
        )
        write_audit_log(
            g.db, "WORK_TASK_CREATED", "WORK_TASK", row.task_id, username,
            validated.warehouse_id,
            {"task_type": validated.task_type, "batch_id": validated.batch_id,
             "assigned_to": validated.assigned_to},
        )
        g.db.commit()
    except IntegrityError:
        g.db.rollback()
        return jsonify({"error": "task_constraint_violation"}), 409
    return jsonify({"task": serialize_task(get_task(g.db, row.task_id))}), 201


@work_control_bp.route("/tasks/current", methods=["GET"])
@require_auth
@with_db
def current_task():
    row = get_current_task(g.db, _username())
    return jsonify({"task": serialize_task(row)})


@work_control_bp.route("/tasks/claim-next", methods=["POST"])
@require_auth
@validate_body(ClaimNextTaskRequest)
@with_db
def claim_next(validated):
    try:
        task_types = _authorized_task_types(g.db, validated.task_types)
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    if task_types == []:
        return jsonify({"task": None, "newly_claimed": False})
    row, newly_claimed = claim_next_task(
        g.db, validated.warehouse_id, _username(),
        task_types=task_types, device_id=validated.device_id,
    )
    g.db.commit()
    return jsonify({"task": serialize_task(row), "newly_claimed": newly_claimed})


@work_control_bp.route("/tasks/<int:task_id>/transition", methods=["POST"])
@require_auth
@validate_body(TaskTransitionRequest)
@with_db
def task_transition(task_id, validated):
    task = get_task(g.db, task_id)
    allowed, response = _task_access(task)
    if not allowed:
        return response
    try:
        updated = transition_task(
            g.db, task_id, _username(), validated.action,
            is_admin=_is_admin(), reason_code=validated.reason_code,
            notes=validated.notes, device_id=validated.device_id,
        )
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    next_row = None
    if validated.action == "COMPLETE" and validated.claim_next:
        try:
            next_task_types = _authorized_task_types(g.db, validated.next_task_types)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        next_row, _ = claim_next_task(
            g.db, updated.warehouse_id, _username(),
            task_types=next_task_types,
            device_id=validated.device_id,
        )
    g.db.commit()
    return jsonify({"task": serialize_task(updated), "next_task": serialize_task(next_row)})


@work_control_bp.route("/tasks/<int:task_id>/verify-scan", methods=["POST"])
@require_auth
@validate_body(VerifyTaskScanRequest)
@with_db
def verify_task_scan(task_id, validated):
    task = get_task(g.db, task_id, for_update=True)
    allowed, response = _task_access(task)
    if not allowed:
        return response
    if task.task_type not in ("PICKING", "PACKING"):
        return jsonify({"error": "This task does not require a Pack Note barcode"}), 409
    if task.status != "CLAIMED":
        return jsonify({"error": f"Task cannot be verified while {task.status}"}), 409
    if not _is_admin() and task.claimed_by != _username():
        return jsonify({"error": "Task belongs to another employee"}), 403
    matched = g.db.execute(
        text(
            """
            SELECT wbo.batch_order_id, wbo.order_number, wbo.courier_barcode,
                   CASE
                       WHEN wb.pack_note_ref = :barcode THEN 'PACK_NOTE'
                       WHEN wbo.courier_barcode = :barcode THEN 'COURIER_BARCODE'
                       ELSE 'ORDER_NUMBER'
                   END AS scan_kind
              FROM work_batches wb
              LEFT JOIN work_batch_orders wbo
                ON wbo.batch_id = wb.batch_id
               AND (
                    wbo.courier_barcode = :barcode
                    OR wbo.order_number = :barcode
               )
             WHERE wb.batch_id = :bid
               AND (wb.pack_note_ref = :barcode OR wbo.batch_order_id IS NOT NULL)
             ORDER BY wbo.batch_order_id NULLS FIRST
             LIMIT 1
            """
        ),
        {"bid": task.batch_id, "barcode": validated.barcode},
    ).fetchone()
    if matched is None:
        return jsonify({"error": "Barcode is not part of this Pack Note"}), 409
    previous = g.db.execute(
        text(
            "SELECT 1 FROM work_task_events WHERE task_id = :tid "
            "AND event_type = 'VERIFIED' AND user_id = :username LIMIT 1"
        ),
        {"tid": task_id, "username": _username()},
    ).fetchone()
    if previous is None:
        record_task_event(
            g.db, task_id, "VERIFIED", _username(),
            reason_code="BATCH_BARCODE_SCANNED",
            notes=f"Scanned {validated.barcode}",
            metadata={
                "batch_order_id": matched.batch_order_id,
                "order_number": matched.order_number,
                "scan_kind": matched.scan_kind,
            },
            device_id=validated.device_id,
        )
        write_audit_log(
            g.db, "WORK_TASK_BATCH_VERIFIED", "WORK_TASK", task_id,
            _username(), task.warehouse_id,
            {
                "batch_id": task.batch_id,
                "batch_order_id": matched.batch_order_id,
                "scan_kind": matched.scan_kind,
            },
            device_id=validated.device_id,
        )
        g.db.commit()
    return jsonify({
        "task": serialize_task(get_task(g.db, task_id)),
        "matched_order": {
            "batch_order_id": matched.batch_order_id,
            "order_number": matched.order_number,
            "scan_kind": matched.scan_kind,
        },
    })


@work_control_bp.route("/tasks/queue", methods=["GET"])
@require_auth
@require_admin_or_page_permission("work-control")
@with_db
def task_queue():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    status = request.args.get("status")
    clauses = ["wt.warehouse_id = :wid"]
    params = {"wid": warehouse_id}
    if status:
        clauses.append("wt.status = :status")
        params["status"] = status.upper()
    if not _is_admin():
        clauses.append("(wt.assigned_to = :username OR wt.claimed_by = :username)")
        params["username"] = _username()
    rows = g.db.execute(
        text(
            "SELECT wt.task_id FROM work_tasks wt WHERE "
            + " AND ".join(clauses)
            + " ORDER BY wt.priority DESC, wt.available_at, wt.task_id LIMIT 500"
        ),
        params,
    ).fetchall()
    return jsonify({"tasks": [serialize_task(get_task(g.db, r.task_id)) for r in rows]})


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@work_control_bp.route("/errors", methods=["POST"])
@require_auth
@validate_body(CreateErrorRequest)
@with_db
def create_error(validated):
    allowed, response = check_warehouse_access(validated.warehouse_id)
    if not allowed:
        return response
    if not _is_admin() and validated.task_id is None:
        return jsonify({"error": "Employees can report issues only from their own task"}), 403
    batch_id = validated.batch_id
    if validated.task_id:
        task = get_task(g.db, validated.task_id)
        if task is None or task.warehouse_id != validated.warehouse_id:
            return jsonify({"error": "Task not found"}), 404
        if not _is_admin() and _username() not in (task.assigned_to, task.claimed_by):
            return jsonify({"error": "Task belongs to another employee"}), 403
        if batch_id is not None and task.batch_id != batch_id:
            return jsonify({"error": "Task and batch do not match"}), 409
        batch_id = batch_id or task.batch_id
    batch_order = None
    if validated.batch_order_id:
        batch_order = g.db.execute(
            text(
                """
                SELECT wbo.*, wb.warehouse_id
                  FROM work_batch_orders wbo
                  JOIN work_batches wb ON wb.batch_id = wbo.batch_id
                 WHERE wbo.batch_order_id = :boid
                """
            ),
            {"boid": validated.batch_order_id},
        ).fetchone()
        if batch_order is None or batch_order.warehouse_id != validated.warehouse_id:
            return jsonify({"error": "Batch order not found"}), 404
        if batch_id is not None and batch_order.batch_id != batch_id:
            return jsonify({"error": "Batch order belongs to another batch"}), 409
        batch_id = batch_order.batch_id
    elif batch_id and (validated.courier_barcode or validated.order_number):
        batch_order = g.db.execute(
            text(
                """
                SELECT wbo.*, wb.warehouse_id
                  FROM work_batch_orders wbo
                  JOIN work_batches wb ON wb.batch_id = wbo.batch_id
                 WHERE wbo.batch_id = :bid
                   AND ((:barcode IS NOT NULL AND (wbo.courier_barcode = :barcode OR wbo.order_number = :barcode))
                     OR (:order_number IS NOT NULL AND wbo.order_number = :order_number))
                 ORDER BY wbo.batch_order_id
                 LIMIT 1
                """
            ),
            {
                "bid": batch_id,
                "barcode": validated.courier_barcode,
                "order_number": validated.order_number,
            },
        ).fetchone()
        if batch_order is None:
            return jsonify({"error": "Order or courier barcode is not part of this Pack Note"}), 409
    if batch_id:
        batch = _load_batch(g.db, batch_id)
        if batch is None or batch.warehouse_id != validated.warehouse_id:
            return jsonify({"error": "Batch not found"}), 404

    workers = {"PICKING": None, "PACKING": None}
    if batch_id:
        rows = g.db.execute(
            text(
                """
                SELECT task_type, claimed_by FROM work_tasks
                 WHERE batch_id = :bid AND task_type IN ('PICKING','PACKING')
                   AND claimed_by IS NOT NULL
                 ORDER BY task_id DESC
                """
            ),
            {"bid": batch_id},
        ).fetchall()
        for row in rows:
            if workers[row.task_type] is None:
                workers[row.task_type] = row.claimed_by

    payload = validated.model_dump()
    payload["batch_id"] = batch_id
    if batch_order is not None:
        payload["batch_order_id"] = batch_order.batch_order_id
        payload["order_number"] = payload["order_number"] or batch_order.order_number
        payload["courier_barcode"] = payload["courier_barcode"] or batch_order.courier_barcode
    result = g.db.execute(
        text(
            """
            INSERT INTO work_errors
                (warehouse_id, task_id, batch_id, batch_order_id, error_type,
                 severity, discovered_stage, reported_by, picker_user_id,
                 packer_user_id, courier_barcode, order_number, sku, quantity,
                 description)
            VALUES
                (:warehouse_id, :task_id, :batch_id, :batch_order_id,
                 :error_type, :severity, :discovered_stage, :reported_by,
                 :picker_user_id, :packer_user_id, :courier_barcode,
                 :order_number, :sku, :quantity, :description)
            RETURNING error_id, created_at
            """
        ),
        {
            **payload,
            "reported_by": _username(),
            "picker_user_id": workers["PICKING"],
            "packer_user_id": workers["PACKING"],
        },
    ).fetchone()
    if validated.task_id:
        record_task_event(
            g.db, validated.task_id, "EXCEPTION", _username(),
            reason_code=validated.error_type, notes=validated.description,
            metadata={"error_id": result.error_id},
        )
    write_audit_log(
        g.db, "WORK_ERROR_REPORTED", "WORK_ERROR", result.error_id,
        _username(), validated.warehouse_id,
        {"task_id": validated.task_id, "batch_id": batch_id,
         "error_type": validated.error_type, "severity": validated.severity},
    )
    g.db.commit()
    return jsonify({
        "error_id": result.error_id,
        "status": "PENDING",
        "responsibility": "UNCONFIRMED",
        "picker_user_id": workers["PICKING"],
        "packer_user_id": workers["PACKING"],
        "created_at": result.created_at.isoformat(),
    }), 201


@work_control_bp.route("/errors", methods=["GET"])
@require_auth
@require_admin_or_page_permission("work-control")
@with_db
def list_errors():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    status = request.args.get("status")
    clauses = ["we.warehouse_id = :wid"]
    params = {"wid": warehouse_id}
    if status:
        clauses.append("we.status = :status")
        params["status"] = status.upper()
    rows = g.db.execute(
        text(
            """
            SELECT we.*, wb.pack_note_ref,
                   COALESCE(
                       (
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'evidence_id', ev.evidence_id,
                                   'content_type', ev.content_type,
                                   'note', ev.note,
                                   'created_at', ev.created_at
                               ) ORDER BY ev.created_at
                           )
                             FROM work_evidence ev
                            WHERE ev.error_id = we.error_id
                       ), '[]'::jsonb
                   ) AS evidence
              FROM work_errors we
            """
            "LEFT JOIN work_batches wb ON wb.batch_id = we.batch_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY we.created_at DESC LIMIT 500"
        ),
        params,
    ).mappings().all()
    return jsonify({"errors": [
        {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
        for row in rows
    ]})


@work_control_bp.route("/errors/<int:error_id>/review", methods=["POST"])
@require_auth
@require_admin_or_page_permission("work-control")
@validate_body(ReviewErrorRequest)
@with_db
def review_error(error_id, validated):
    row = g.db.execute(
        text("SELECT * FROM work_errors WHERE error_id = :eid FOR UPDATE"),
        {"eid": error_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "Error case not found"}), 404
    if row.status != "PENDING":
        return jsonify({"error": f"Error case is already {row.status}"}), 409
    g.db.execute(
        text(
            """
            UPDATE work_errors
               SET status = :status, responsibility = :responsibility,
                   resolution_notes = :notes, reviewed_by = :reviewer,
                   reviewed_at = NOW(), updated_at = NOW()
             WHERE error_id = :eid
            """
        ),
        {
            "status": validated.status,
            "responsibility": validated.responsibility,
            "notes": validated.resolution_notes,
            "reviewer": _username(),
            "eid": error_id,
        },
    )
    write_audit_log(
        g.db, "WORK_ERROR_REVIEWED", "WORK_ERROR", error_id, _username(),
        row.warehouse_id,
        {"status": validated.status, "responsibility": validated.responsibility,
         "resolution_notes": validated.resolution_notes},
    )
    g.db.commit()
    return jsonify({"error_id": error_id, **validated.model_dump(), "reviewed_by": _username()})


# ---------------------------------------------------------------------------
# Local SKU identity catalog (read from SiteGiant, never written back)
# ---------------------------------------------------------------------------


def _serialize_work_sku(row):
    if row is None:
        return None
    return {
        "sku_catalog_id": row.sku_catalog_id,
        "warehouse_id": row.warehouse_id,
        "sku": row.sku,
        "item_name": row.item_name,
        "source_system": row.source_system,
        "source_item_id": row.source_item_id,
        "source_item_url": row.source_item_url,
        "image_url": row.image_url,
        "needs_review": row.needs_review,
        "last_evidence_id": row.last_evidence_id,
        "last_received_at": (
            row.last_received_at.isoformat() if row.last_received_at else None
        ),
        "synced_at": row.synced_at.isoformat() if row.synced_at else None,
    }


@work_control_bp.route("/skus", methods=["GET"])
@require_auth
@with_db
def search_work_skus():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    allowed, response = check_warehouse_access(warehouse_id)
    if not allowed:
        return response
    query = (request.args.get("q") or "").strip()
    if len(query) > 128:
        return jsonify({"error": "SKU search is too long"}), 422
    limit = min(max(request.args.get("limit", default=12, type=int) or 12, 1), 50)
    params = {"wid": warehouse_id, "limit": limit}
    search_clause = ""
    rank_sql = "c.sku_normalized, c.item_name"
    if query:
        params.update({
            "exact": query.upper(),
            "prefix": f"{query.upper()}%",
            "contains": f"%{query}%",
        })
        search_clause = """
          AND (
              c.sku_normalized ILIKE :prefix
              OR c.item_name ILIKE :contains
          )
        """
        rank_sql = """
            CASE
                WHEN c.sku_normalized = :exact THEN 0
                WHEN c.sku_normalized ILIKE :prefix THEN 1
                ELSE 2
            END,
            c.sku_normalized
        """
    rows = g.db.execute(
        text(
            f"""
            SELECT c.sku_catalog_id, c.warehouse_id, c.sku, c.item_name,
                   c.source_system, c.source_item_id, c.source_item_url,
                   c.image_url, c.needs_review, c.synced_at,
                   latest.evidence_id AS last_evidence_id,
                   latest.created_at AS last_received_at
              FROM work_sku_catalog c
              LEFT JOIN LATERAL (
                    SELECT we.evidence_id, we.created_at
                      FROM receiving_draft_lines rdl
                      JOIN receiving_drafts rd
                        ON rd.receiving_id = rdl.receiving_id
                      JOIN work_evidence we
                        ON we.receiving_line_id = rdl.receiving_line_id
                        OR we.receiving_id = rd.receiving_id
                     WHERE rd.warehouse_id = c.warehouse_id
                       AND UPPER(BTRIM(rdl.sku)) = c.sku_normalized
                       AND rd.status IN ('SUBMITTED', 'APPROVED', 'POSTED')
                     ORDER BY we.created_at DESC, we.evidence_id DESC
                     LIMIT 1
              ) latest ON TRUE
             WHERE c.warehouse_id = :wid
               AND c.is_active = TRUE
               {search_clause}
             ORDER BY {rank_sql}
             LIMIT :limit
            """
        ),
        params,
    ).fetchall()
    summary = g.db.execute(
        text(
            """
            SELECT COUNT(*) FILTER (WHERE is_active) AS active_count,
                   COUNT(*) FILTER (WHERE is_active AND needs_review) AS review_count,
                   MAX(synced_at) AS last_synced_at
              FROM work_sku_catalog
             WHERE warehouse_id = :wid
            """
        ),
        {"wid": warehouse_id},
    ).fetchone()
    return jsonify({
        "skus": [_serialize_work_sku(row) for row in rows],
        "catalog": {
            "active_count": summary.active_count,
            "needs_review_count": summary.review_count,
            "last_synced_at": (
                summary.last_synced_at.isoformat() if summary.last_synced_at else None
            ),
        },
    })


@work_control_bp.route("/skus", methods=["POST"])
@require_auth
@validate_body(CreateWorkSkuRequest)
@with_db
def create_work_sku(validated):
    allowed, response = check_warehouse_access(validated.warehouse_id)
    if not allowed:
        return response
    if not _can_receive():
        return jsonify({"error": "Receiving access is required to add a SKU"}), 403
    try:
        row = g.db.execute(
            text(
                """
                INSERT INTO work_sku_catalog
                    (warehouse_id, sku, item_name, source_system, needs_review,
                     is_active, created_by)
                VALUES (:wid, :sku, :item_name, 'manual', TRUE, TRUE, :created_by)
                RETURNING sku_catalog_id, warehouse_id, sku, item_name,
                          source_system, source_item_id, source_item_url,
                          image_url, needs_review, synced_at,
                          NULL::BIGINT AS last_evidence_id,
                          NULL::TIMESTAMPTZ AS last_received_at
                """
            ),
            {
                "wid": validated.warehouse_id,
                "sku": validated.sku,
                "item_name": validated.item_name,
                "created_by": _username(),
            },
        ).fetchone()
    except IntegrityError:
        g.db.rollback()
        existing = g.db.execute(
            text(
                """
                SELECT sku_catalog_id, warehouse_id, sku, item_name,
                       source_system, source_item_id, source_item_url,
                       image_url, needs_review, synced_at,
                       NULL::BIGINT AS last_evidence_id,
                       NULL::TIMESTAMPTZ AS last_received_at
                  FROM work_sku_catalog
                 WHERE warehouse_id = :wid
                   AND sku_normalized = :sku
                """
            ),
            {"wid": validated.warehouse_id, "sku": validated.sku},
        ).fetchone()
        return jsonify({
            "error": "SKU already exists",
            "sku": _serialize_work_sku(existing),
        }), 409
    write_audit_log(
        g.db, "WORK_SKU_CREATED", "WORK_SKU", row.sku_catalog_id,
        _username(), validated.warehouse_id,
        {"sku": validated.sku, "source_system": "manual", "needs_review": True},
    )
    g.db.commit()
    return jsonify({"sku": _serialize_work_sku(row)}), 201


@work_control_bp.route("/sitegiant/skus/sync", methods=["POST"])
@require_wms_token
@validate_body(SiteGiantSkuSyncRequest)
@with_db
def sync_sitegiant_skus(validated):
    allowed_warehouses = set(g.current_token.get("warehouse_ids") or [])
    if validated.warehouse_id not in allowed_warehouses:
        return jsonify({"error": "warehouse_scope_violation"}), 403
    sync_run = str(validated.sync_run_id)
    for item in validated.items:
        g.db.execute(
            text(
                """
                INSERT INTO work_sku_catalog
                    (warehouse_id, sku, item_name, source_system,
                     source_item_id, source_item_url, image_url,
                     needs_review, is_active, last_sync_run, synced_at,
                     created_by)
                VALUES
                    (:wid, :sku, :item_name, 'sitegiant',
                     :source_item_id, :source_item_url, :image_url,
                     FALSE, TRUE, :sync_run, :synced_at, 'sitegiant-bridge')
                ON CONFLICT (warehouse_id, sku_normalized) DO UPDATE
                    SET sku = EXCLUDED.sku,
                        item_name = EXCLUDED.item_name,
                        source_system = 'sitegiant',
                        source_item_id = EXCLUDED.source_item_id,
                        source_item_url = EXCLUDED.source_item_url,
                        image_url = EXCLUDED.image_url,
                        needs_review = FALSE,
                        is_active = TRUE,
                        last_sync_run = EXCLUDED.last_sync_run,
                        synced_at = EXCLUDED.synced_at,
                        updated_at = NOW()
                """
            ),
            {
                "wid": validated.warehouse_id,
                "sku": item.sku,
                "item_name": item.item_name,
                "source_item_id": item.source_item_id,
                "source_item_url": item.source_item_url,
                "image_url": item.image_url,
                "sync_run": sync_run,
                "synced_at": validated.captured_at,
            },
        )
    deactivated = 0
    completed = validated.page == validated.total_pages
    if completed:
        result = g.db.execute(
            text(
                """
                UPDATE work_sku_catalog
                   SET is_active = FALSE, updated_at = NOW()
                 WHERE warehouse_id = :wid
                   AND source_system = 'sitegiant'
                   AND is_active = TRUE
                   AND last_sync_run IS DISTINCT FROM :sync_run
                   AND (synced_at IS NULL OR synced_at <= :synced_at)
                """
            ),
            {
                "wid": validated.warehouse_id,
                "sync_run": sync_run,
                "synced_at": validated.captured_at,
            },
        )
        deactivated = result.rowcount
        write_audit_log(
            g.db, "SITEGIANT_SKU_SYNC_COMPLETED", "WAREHOUSE",
            validated.warehouse_id,
            f"sitegiant-token-{g.current_token['token_id']}",
            validated.warehouse_id,
            {
                "sync_run_id": sync_run,
                "total_items": validated.total_items,
                "total_pages": validated.total_pages,
                "deactivated": deactivated,
            },
        )
    g.db.commit()
    return jsonify({
        "ok": True,
        "page": validated.page,
        "total_pages": validated.total_pages,
        "accepted": len(validated.items),
        "completed": completed,
        "deactivated": deactivated,
    })


# ---------------------------------------------------------------------------
# Draft GRN / receiving counts
# ---------------------------------------------------------------------------


def _load_receiving_draft(db, receiving_id):
    header = db.execute(
        text("SELECT * FROM receiving_drafts WHERE receiving_id = :rid"),
        {"rid": receiving_id},
    ).mappings().first()
    if header is None:
        return None
    lines = db.execute(
        text(
            "SELECT * FROM receiving_draft_lines WHERE receiving_id = :rid "
            "ORDER BY receiving_line_id"
        ),
        {"rid": receiving_id},
    ).mappings().all()
    evidence = db.execute(
        text(
            """
            SELECT we.evidence_id, we.receiving_id, we.receiving_line_id,
                   we.content_type, we.note, we.created_at
              FROM work_evidence we
             WHERE we.receiving_id = :rid
                OR we.receiving_line_id IN (
                    SELECT receiving_line_id FROM receiving_draft_lines
                     WHERE receiving_id = :rid
                )
             ORDER BY we.created_at
            """
        ),
        {"rid": receiving_id},
    ).mappings().all()
    def serialise(row):
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
    result = serialise(header)
    result["lines"] = [serialise(row) for row in lines]
    result["evidence"] = [serialise(row) for row in evidence]
    return result


@work_control_bp.route("/receiving-drafts", methods=["POST"])
@require_auth
@validate_body(CreateReceivingDraftRequest)
@with_db
def create_receiving_draft(validated):
    allowed, response = check_warehouse_access(validated.warehouse_id)
    if not allowed:
        return response
    task = None
    if validated.task_id:
        task = get_task(g.db, validated.task_id, for_update=True)
        if task is None or task.warehouse_id != validated.warehouse_id:
            return jsonify({"error": "Receiving task not found"}), 404
        if task.task_type != "RECEIVING":
            return jsonify({"error": "Task is not a receiving task"}), 409
        if not _is_admin() and task.claimed_by != _username():
            return jsonify({"error": "Receiving task belongs to another employee"}), 403
        if task.status != "IN_PROGRESS":
            return jsonify({"error": "Receiving task must be started before counting"}), 409
        existing = g.db.execute(
            text("SELECT receiving_id, status FROM receiving_drafts WHERE task_id = :tid"),
            {"tid": task.task_id},
        ).fetchone()
        if existing is not None:
            return jsonify({
                "error": "Receiving draft already exists for this task",
                "receiving_id": existing.receiving_id,
                "status": existing.status,
            }), 409

    header = g.db.execute(
        text(
            """
            INSERT INTO receiving_drafts
                (task_id, warehouse_id, source_system, po_number, supplier_ref,
                 status, counted_by, notes, submitted_at)
            VALUES (:task_id, :wid, :source, :po_number, :supplier_ref,
                    'DRAFT', :counted_by, :notes, NULL)
            RETURNING receiving_id
            """
        ),
        {
            "task_id": validated.task_id,
            "wid": validated.warehouse_id,
            "source": validated.source_system,
            "po_number": validated.po_number,
            "supplier_ref": validated.supplier_ref,
            "counted_by": _username(),
            "notes": validated.notes,
        },
    ).fetchone()
    for line in validated.lines:
        catalog = g.db.execute(
            text(
                """
                SELECT sku_catalog_id, item_name
                  FROM work_sku_catalog
                 WHERE warehouse_id = :wid
                   AND sku_normalized = :sku
                   AND is_active = TRUE
                """
            ),
            {"wid": validated.warehouse_id, "sku": line.sku},
        ).fetchone()
        if catalog is None and line.item_name:
            g.db.execute(
                text(
                    """
                    INSERT INTO work_sku_catalog
                        (warehouse_id, sku, item_name, source_system,
                         needs_review, is_active, created_by)
                    VALUES (:wid, :sku, :item_name, 'manual', TRUE, TRUE, :created_by)
                    ON CONFLICT (warehouse_id, sku_normalized) DO NOTHING
                    """
                ),
                {
                    "wid": validated.warehouse_id,
                    "sku": line.sku,
                    "item_name": line.item_name,
                    "created_by": _username(),
                },
            )
            catalog = g.db.execute(
                text(
                    """
                    SELECT sku_catalog_id, item_name
                      FROM work_sku_catalog
                     WHERE warehouse_id = :wid
                       AND sku_normalized = :sku
                    """
                ),
                {"wid": validated.warehouse_id, "sku": line.sku},
            ).fetchone()
        expected = line.expected_quantity
        short = max((expected or 0) - line.received_quantity, 0) if expected is not None else 0
        over = max(line.received_quantity - (expected or 0), 0) if expected is not None else 0
        g.db.execute(
            text(
                """
                INSERT INTO receiving_draft_lines
                    (receiving_id, sku, sku_catalog_id, item_name,
                     expected_quantity, received_quantity,
                     good_quantity, damaged_quantity, short_quantity,
                     over_quantity, notes)
                VALUES (:rid, :sku, :sku_catalog_id, :item_name,
                        :expected, :received, :good, :damaged,
                        :short, :over, :notes)
                """
            ),
            {
                "rid": header.receiving_id,
                "sku": line.sku,
                "sku_catalog_id": catalog.sku_catalog_id if catalog else None,
                "item_name": catalog.item_name if catalog else line.item_name,
                "expected": expected,
                "received": line.received_quantity,
                "good": line.good_quantity,
                "damaged": line.damaged_quantity,
                "short": short,
                "over": over,
                "notes": line.notes,
            },
        )
    write_audit_log(
        g.db, "RECEIVING_DRAFT_CREATED",
        "RECEIVING_DRAFT", header.receiving_id, _username(), validated.warehouse_id,
        {"task_id": validated.task_id, "po_number": validated.po_number,
         "line_count": len(validated.lines)},
    )
    g.db.commit()
    return jsonify({
        "receiving": _load_receiving_draft(g.db, header.receiving_id),
        "next_task": None,
    }), 201


@work_control_bp.route("/receiving-drafts/<int:receiving_id>/submit", methods=["POST"])
@require_auth
@validate_body(SubmitReceivingDraftRequest)
@with_db
def submit_receiving_draft(receiving_id, validated):
    """Submit a counted receipt to the stock clerk without posting stock.

    The employee creates the draft first, uploads one or more photos, then
    submits it.  Submission completes the linked receiving task and can claim
    the employee's next task in the same transaction.
    """
    row = g.db.execute(
        text("SELECT * FROM receiving_drafts WHERE receiving_id = :rid FOR UPDATE"),
        {"rid": receiving_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "Receiving draft not found"}), 404
    allowed, response = check_warehouse_access(row.warehouse_id)
    if not allowed:
        return response
    if not _is_admin() and row.counted_by != _username():
        return jsonify({"error": "Receiving draft belongs to another employee"}), 403
    if row.status != "DRAFT":
        return jsonify({"error": f"Receiving draft is already {row.status}"}), 409

    evidence_count = g.db.execute(
        text(
            """
            SELECT COUNT(*)
              FROM work_evidence we
             WHERE we.receiving_id = :rid
                OR we.receiving_line_id IN (
                    SELECT receiving_line_id FROM receiving_draft_lines
                     WHERE receiving_id = :rid
                )
            """
        ),
        {"rid": receiving_id},
    ).scalar()
    if evidence_count == 0:
        return jsonify({"error": "At least one receiving photo is required before submission"}), 409
    header_evidence_count = g.db.execute(
        text("SELECT COUNT(*) FROM work_evidence WHERE receiving_id = :rid"),
        {"rid": receiving_id},
    ).scalar()
    missing_line_photos = g.db.execute(
        text(
            """
            SELECT COUNT(*)
              FROM receiving_draft_lines rdl
             WHERE rdl.receiving_id = :rid
               AND NOT EXISTS (
                    SELECT 1 FROM work_evidence we
                     WHERE we.receiving_line_id = rdl.receiving_line_id
               )
            """
        ),
        {"rid": receiving_id},
    ).scalar()
    if header_evidence_count == 0 and missing_line_photos > 0:
        return jsonify({
            "error": "Take one arrival photo for every SKU before submission"
        }), 409

    task = None
    if row.task_id is not None:
        task = get_task(g.db, row.task_id, for_update=True)
        if task is None:
            return jsonify({"error": "Linked receiving task not found"}), 409
        if not _is_admin() and task.claimed_by != _username():
            return jsonify({"error": "Receiving task belongs to another employee"}), 403
        if task.status != "IN_PROGRESS":
            return jsonify({"error": "Receiving task must be started before submission"}), 409

    g.db.execute(
        text(
            """
            UPDATE receiving_drafts
               SET status = 'SUBMITTED', submitted_at = NOW(), updated_at = NOW()
             WHERE receiving_id = :rid
            """
        ),
        {"rid": receiving_id},
    )
    write_audit_log(
        g.db, "RECEIVING_DRAFT_SUBMITTED", "RECEIVING_DRAFT", receiving_id,
        _username(), row.warehouse_id,
        {"task_id": row.task_id, "photo_count": evidence_count},
        device_id=validated.device_id,
    )

    next_task = None
    if task is not None:
        transition_task(
            g.db, task.task_id, _username(), "COMPLETE",
            is_admin=_is_admin(), notes=f"Draft GRN {receiving_id} submitted",
            device_id=validated.device_id,
        )
        if validated.claim_next:
            try:
                next_task_types = _authorized_task_types(g.db, validated.next_task_types)
            except PermissionError as exc:
                return jsonify({"error": str(exc)}), 403
            next_task, _ = claim_next_task(
                g.db, row.warehouse_id, _username(),
                task_types=next_task_types, device_id=validated.device_id,
            )
    g.db.commit()
    return jsonify({
        "receiving": _load_receiving_draft(g.db, receiving_id),
        "next_task": serialize_task(next_task),
    })


@work_control_bp.route("/receiving-drafts", methods=["GET"])
@require_auth
@with_db
def list_receiving_drafts():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    allowed, response = check_warehouse_access(warehouse_id)
    if not allowed:
        return response
    clauses = ["warehouse_id = :wid"]
    params = {"wid": warehouse_id}
    if not _can_supervise():
        clauses.append("counted_by = :username")
        params["username"] = _username()
    status = request.args.get("status")
    if status:
        clauses.append("status = :status")
        params["status"] = status.upper()
    rows = g.db.execute(
        text(
            "SELECT receiving_id FROM receiving_drafts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT 500"
        ),
        params,
    ).fetchall()
    return jsonify({"receiving_drafts": [
        _load_receiving_draft(g.db, r.receiving_id) for r in rows
    ]})


@work_control_bp.route("/receiving-drafts/<int:receiving_id>/review", methods=["POST"])
@require_auth
@require_admin_or_page_permission("work-control")
@validate_body(ReviewReceivingDraftRequest)
@with_db
def review_receiving_draft(receiving_id, validated):
    row = g.db.execute(
        text("SELECT * FROM receiving_drafts WHERE receiving_id = :rid FOR UPDATE"),
        {"rid": receiving_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "Receiving draft not found"}), 404
    allowed = {
        "SUBMITTED": {"APPROVED", "REJECTED"},
        "APPROVED": {"POSTED"},
    }
    if validated.status not in allowed.get(row.status, set()):
        return jsonify({"error": f"Cannot change {row.status} to {validated.status}"}), 409
    g.db.execute(
        text(
            """
            UPDATE receiving_drafts
               SET status = :status, review_notes = :notes,
                   reviewed_by = :reviewer, reviewed_at = NOW(), updated_at = NOW()
             WHERE receiving_id = :rid
            """
        ),
        {"status": validated.status, "notes": validated.review_notes,
         "reviewer": _username(), "rid": receiving_id},
    )
    recount_task_id = None
    if validated.status == "REJECTED" and row.task_id is not None:
        original = get_task(g.db, row.task_id)
        if original is not None:
            recount = g.db.execute(
                text(
                    """
                    INSERT INTO work_tasks
                        (warehouse_id, task_type, status, priority, assigned_to,
                         source_ref, order_count, sku_count, unit_count,
                         complexity_note, idempotency_key, created_by)
                    VALUES
                        (:wid, 'RECEIVING', 'ASSIGNED', :priority, :assigned_to,
                         :source_ref, :order_count, :sku_count, :unit_count,
                         :note, :idempotency_key, :created_by)
                    ON CONFLICT (idempotency_key) DO UPDATE
                        SET updated_at = work_tasks.updated_at
                    RETURNING task_id
                    """
                ),
                {
                    "wid": row.warehouse_id,
                    "priority": min(100, original.priority + 10),
                    "assigned_to": row.counted_by,
                    "source_ref": row.po_number or row.supplier_ref or original.source_ref,
                    "order_count": original.order_count,
                    "sku_count": original.sku_count,
                    "unit_count": original.unit_count,
                    "note": f"Recount required for rejected Draft GRN {receiving_id}",
                    "idempotency_key": f"receiving-recount:{receiving_id}:v1",
                    "created_by": _username(),
                },
            ).fetchone()
            recount_task_id = recount.task_id
            record_task_event(
                g.db, recount_task_id, "ASSIGNED", _username(),
                reason_code="RECEIVING_REJECTED",
                notes=f"Draft GRN {receiving_id} requires recount",
                metadata={"receiving_id": receiving_id, "assigned_to": row.counted_by},
            )
    write_audit_log(
        g.db, f"RECEIVING_DRAFT_{validated.status}", "RECEIVING_DRAFT",
        receiving_id, _username(), row.warehouse_id,
        {"previous_status": row.status, "review_notes": validated.review_notes,
         "recount_task_id": recount_task_id},
    )
    g.db.commit()
    return jsonify({
        "receiving": _load_receiving_draft(g.db, receiving_id),
        "recount_task_id": recount_task_id,
    })


# ---------------------------------------------------------------------------
# Evidence upload / retrieval
# ---------------------------------------------------------------------------


_IMAGE_SIGNATURES = {
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP",
    "image/heic": lambda b: len(b) >= 12 and b[4:8] == b"ftyp" and b[8:12] in (b"heic", b"heix", b"hevc", b"mif1"),
}
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
}


def _detect_image_type(data):
    for content_type, predicate in _IMAGE_SIGNATURES.items():
        if predicate(data):
            return content_type
    return None


def _evidence_entity(db, *, error_id=None, receiving_id=None, receiving_line_id=None):
    if error_id:
        return db.execute(
            text("SELECT warehouse_id, reported_by AS owner FROM work_errors WHERE error_id = :id"),
            {"id": error_id},
        ).fetchone()
    if receiving_id:
        return db.execute(
            text("SELECT warehouse_id, counted_by AS owner FROM receiving_drafts WHERE receiving_id = :id"),
            {"id": receiving_id},
        ).fetchone()
    if receiving_line_id:
        return db.execute(
            text(
                """
                SELECT rd.warehouse_id, rd.counted_by AS owner
                  FROM receiving_draft_lines rdl
                  JOIN receiving_drafts rd ON rd.receiving_id = rdl.receiving_id
                 WHERE rdl.receiving_line_id = :id
                """
            ),
            {"id": receiving_line_id},
        ).fetchone()
    return None


@work_control_bp.route("/evidence", methods=["POST"])
@require_auth
@with_db
def upload_evidence():
    upload = request.files.get("photo")
    if upload is None:
        return jsonify({"error": "photo file is required"}), 400
    ids = {
        "error_id": request.form.get("error_id", type=int),
        "receiving_id": request.form.get("receiving_id", type=int),
        "receiving_line_id": request.form.get("receiving_line_id", type=int),
    }
    if sum(value is not None for value in ids.values()) != 1:
        return jsonify({"error": "exactly one evidence target is required"}), 400
    entity = _evidence_entity(g.db, **ids)
    if entity is None:
        return jsonify({"error": "Evidence target not found"}), 404
    allowed, response = check_warehouse_access(entity.warehouse_id)
    if not allowed:
        return response
    if not _is_admin() and entity.owner != _username():
        return jsonify({"error": "Evidence target belongs to another employee"}), 403

    data = upload.stream.read(10 * 1024 * 1024 + 1)
    if not data:
        return jsonify({"error": "Photo is empty"}), 400
    if len(data) > 10 * 1024 * 1024:
        return jsonify({"error": "Photo exceeds 10 MB"}), 413
    content_type = _detect_image_type(data)
    if content_type is None:
        return jsonify({"error": "Only JPEG, PNG, WebP and HEIC photos are accepted"}), 415

    storage_key = f"{uuid.uuid4().hex}{_IMAGE_EXTENSIONS[content_type]}"
    try:
        store = evidence_storage()
        store.put(storage_key, data, content_type)
    except (EvidenceUnavailableError, OSError):
        return jsonify({"error": "Evidence storage is temporarily unavailable"}), 503
    try:
        row = g.db.execute(
            text(
                """
                INSERT INTO work_evidence
                    (warehouse_id, error_id, receiving_id, receiving_line_id,
                     storage_key, original_filename, content_type, byte_size,
                     sha256, note, uploaded_by)
                VALUES (:wid, :error_id, :receiving_id, :receiving_line_id,
                        :storage_key, :original_filename, :content_type,
                        :byte_size, :sha256, :note, :uploaded_by)
                RETURNING evidence_id, created_at
                """
            ),
            {
                "wid": entity.warehouse_id,
                **ids,
                "storage_key": storage_key,
                "original_filename": (secure_filename(upload.filename or "photo") or "photo")[:255],
                "content_type": content_type,
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "note": (request.form.get("note") or "")[:500] or None,
                "uploaded_by": _username(),
            },
        ).fetchone()
        write_audit_log(
            g.db, "WORK_EVIDENCE_UPLOADED", "WORK_EVIDENCE", row.evidence_id,
            _username(), entity.warehouse_id,
            {**ids, "content_type": content_type, "byte_size": len(data)},
        )
        g.db.commit()
    except Exception:
        try:
            store.delete(storage_key)
        finally:
            raise
    return jsonify({
        "evidence_id": row.evidence_id,
        "content_type": content_type,
        "byte_size": len(data),
        "created_at": row.created_at.isoformat(),
    }), 201


@work_control_bp.route("/evidence/<int:evidence_id>", methods=["GET"])
@require_auth
@with_db
def get_evidence(evidence_id):
    row = g.db.execute(
        text("SELECT * FROM work_evidence WHERE evidence_id = :eid"),
        {"eid": evidence_id},
    ).fetchone()
    if row is None:
        return jsonify({"error": "Evidence not found"}), 404
    allowed, response = check_warehouse_access(row.warehouse_id)
    if not allowed:
        return response
    try:
        data = evidence_storage().get(row.storage_key)
    except FileNotFoundError:
        return jsonify({"error": "Evidence file is unavailable"}), 404
    except (EvidenceUnavailableError, OSError):
        return jsonify({"error": "Evidence storage is temporarily unavailable"}), 503
    return send_file(
        io.BytesIO(data), mimetype=row.content_type, as_attachment=False,
        download_name=row.original_filename, conditional=False,
    )


# ---------------------------------------------------------------------------
# SiteGiant hourly workload snapshots (read-only supervision data)
# ---------------------------------------------------------------------------


@work_control_bp.route("/sitegiant/workload-snapshots", methods=["POST"])
@require_wms_token
@validate_body(SiteGiantWorkloadSnapshotRequest)
@with_db
def capture_sitegiant_workload(validated):
    allowed_warehouses = set(g.current_token.get("warehouse_ids") or [])
    if validated.warehouse_id not in allowed_warehouses:
        return jsonify({"error": "warehouse_scope_violation"}), 403

    params = {
        "warehouse_id": validated.warehouse_id,
        "captured_at": validated.captured_at,
        "period_start": validated.period_start,
        "period_end": validated.period_end,
        "period_label": validated.period_label,
        "pending": validated.pending_packages,
        "to_process": validated.to_process_packages,
        "printed": validated.printed_packages,
        "pending_pickup": validated.pending_pickup_packages,
        "dashboard_orders": validated.dashboard_order_count,
        "source_url": validated.source_url.rstrip("/"),
        "idempotency_key": validated.idempotency_key,
        "token_id": g.current_token["token_id"],
    }
    existed = g.db.execute(
        text(
            """
            SELECT 1 FROM sitegiant_workload_snapshots
             WHERE warehouse_id = :warehouse_id
               AND source_system = 'sitegiant'
               AND idempotency_key = :idempotency_key
            """
        ),
        params,
    ).scalar() is not None
    row = g.db.execute(
        text(
            """
            INSERT INTO sitegiant_workload_snapshots
                (warehouse_id, source_system, captured_at, period_start,
                 period_end, period_label, pending_packages,
                 to_process_packages, printed_packages,
                 pending_pickup_packages, dashboard_order_count, source_url,
                 idempotency_key, captured_by_token_id)
            VALUES
                (:warehouse_id, 'sitegiant', :captured_at, :period_start,
                 :period_end, :period_label, :pending, :to_process, :printed,
                 :pending_pickup, :dashboard_orders, :source_url,
                 :idempotency_key, :token_id)
            ON CONFLICT (warehouse_id, source_system, idempotency_key)
            DO UPDATE SET
                captured_at = EXCLUDED.captured_at,
                period_start = EXCLUDED.period_start,
                period_end = EXCLUDED.period_end,
                period_label = EXCLUDED.period_label,
                pending_packages = EXCLUDED.pending_packages,
                to_process_packages = EXCLUDED.to_process_packages,
                printed_packages = EXCLUDED.printed_packages,
                pending_pickup_packages = EXCLUDED.pending_pickup_packages,
                dashboard_order_count = EXCLUDED.dashboard_order_count,
                source_url = EXCLUDED.source_url,
                captured_by_token_id = EXCLUDED.captured_by_token_id
            RETURNING *
            """
        ),
        params,
    ).fetchone()
    g.db.commit()
    return jsonify({
        "snapshot": _serialize_workload_snapshot(row),
        "duplicate": existed,
        "updated": existed,
    }), 200 if existed else 201


@work_control_bp.route("/sitegiant/workload", methods=["GET"])
@require_auth
@require_admin_or_page_permission("work-control")
@with_db
def sitegiant_workload_report():
    warehouse_id = request.args.get("warehouse_id", type=int)
    hours = request.args.get("hours", default=24, type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    if hours < 1 or hours > 168:
        return jsonify({"error": "hours must be between 1 and 168"}), 422
    allowed, response = check_warehouse_access(warehouse_id)
    if not allowed:
        return response

    snapshot_rows = g.db.execute(
        text(
            """
            SELECT * FROM sitegiant_workload_snapshots
             WHERE warehouse_id = :warehouse_id
               AND captured_at >= NOW() - make_interval(hours => :hours)
             ORDER BY captured_at ASC, snapshot_id ASC
            """
        ),
        {"warehouse_id": warehouse_id, "hours": hours},
    ).fetchall()
    snapshots = [_serialize_workload_snapshot(row) for row in snapshot_rows]

    local_midnight = """
        date_trunc('day', NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')
        AT TIME ZONE 'Asia/Kuala_Lumpur'
    """
    task_rows = g.db.execute(
        text(
            f"""
            SELECT task_type, status, COUNT(*) AS task_count,
                   COALESCE(SUM(order_count), 0) AS order_count,
                   COALESCE(SUM(unit_count), 0) AS unit_count
              FROM work_tasks
             WHERE warehouse_id = :warehouse_id
               AND (
                    status NOT IN ('COMPLETED', 'CANCELLED')
                    OR completed_at >= ({local_midnight})
               )
             GROUP BY task_type, status
             ORDER BY task_type, status
            """
        ),
        {"warehouse_id": warehouse_id},
    ).mappings().all()
    task_progress = [
        {
            "task_type": row["task_type"],
            "status": row["status"],
            "task_count": int(row["task_count"] or 0),
            "order_count": int(row["order_count"] or 0),
            "unit_count": int(row["unit_count"] or 0),
        }
        for row in task_rows
    ]

    latest = snapshots[-1] if snapshots else None
    previous = snapshots[-2] if len(snapshots) > 1 else None
    forecast = _workload_forecast(
        g.db, warehouse_id, latest["remaining_packages"] if latest else 0,
    ) if latest else None
    now = datetime.now(timezone.utc)
    age_minutes = None
    if snapshot_rows:
        age_minutes = max(
            0, int((now - snapshot_rows[-1].captured_at).total_seconds() // 60)
        )
    return jsonify({
        "warehouse_id": warehouse_id,
        "hours": hours,
        "latest": latest,
        "snapshots": snapshots,
        "task_progress": task_progress,
        "forecast": forecast,
        "sync": {
            "status": "missing" if latest is None else (
                "stale" if age_minutes is not None and age_minutes > 90 else "current"
            ),
            "age_minutes": age_minutes,
        },
        "change": {
            "remaining_packages": (
                latest["remaining_packages"] - previous["remaining_packages"]
            ) if latest and previous else None,
            "printed_packages": (
                latest["printed_packages"] - previous["printed_packages"]
            ) if latest and previous else None,
        },
    })


# ---------------------------------------------------------------------------
# Objective efficiency report (no score / KPI formula)
# ---------------------------------------------------------------------------


@work_control_bp.route("/reports/efficiency", methods=["GET"])
@require_auth
@require_admin_or_page_permission("work-control")
@with_db
def efficiency_report():
    warehouse_id = request.args.get("warehouse_id", type=int)
    start = request.args.get("start") or date.today().isoformat()
    end = request.args.get("end") or date.today().isoformat()
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        return jsonify({"error": "start/end must be YYYY-MM-DD"}), 422
    if end_date < start_date or (end_date - start_date).days > 90:
        return jsonify({"error": "date range must be between 1 and 91 days"}), 422
    end_exclusive = end_date + timedelta(days=1)
    rows = g.db.execute(
        text(
            """
            SELECT claimed_by AS employee, task_type,
                   COUNT(*) FILTER (WHERE status = 'COMPLETED') AS completed_tasks,
                   SUM(order_count) FILTER (WHERE status = 'COMPLETED') AS orders_handled,
                   SUM(sku_count) FILTER (WHERE status = 'COMPLETED') AS skus_handled,
                   SUM(unit_count) FILTER (WHERE status = 'COMPLETED') AS units_handled,
                   SUM(active_seconds) FILTER (WHERE status = 'COMPLETED') AS active_seconds,
                   SUM(paused_seconds) FILTER (WHERE status = 'COMPLETED') AS paused_seconds,
                   AVG(active_seconds) FILTER (WHERE status = 'COMPLETED') AS average_active_seconds
              FROM work_tasks
             WHERE warehouse_id = :wid
               AND claimed_by IS NOT NULL
               AND completed_at >= :start AND completed_at < :end
             GROUP BY claimed_by, task_type
             ORDER BY claimed_by, task_type
            """
        ),
        {"wid": warehouse_id, "start": start_date, "end": end_exclusive},
    ).mappings().all()
    mistakes = g.db.execute(
        text(
            """
            SELECT employee, stage, COUNT(*) AS confirmed_errors
              FROM (
                    SELECT picker_user_id AS employee, 'PICKING' AS stage
                      FROM work_errors
                     WHERE warehouse_id = :wid AND status = 'CONFIRMED'
                       AND responsibility IN ('PICKER','BOTH')
                       AND picker_user_id IS NOT NULL
                       AND reviewed_at >= :start AND reviewed_at < :end
                    UNION ALL
                    SELECT packer_user_id AS employee, 'PACKING' AS stage
                      FROM work_errors
                     WHERE warehouse_id = :wid AND status = 'CONFIRMED'
                       AND responsibility IN ('PACKER','BOTH')
                       AND packer_user_id IS NOT NULL
                       AND reviewed_at >= :start AND reviewed_at < :end
                   ) attributed
             GROUP BY employee, stage
             ORDER BY employee, stage
            """
        ),
        {"wid": warehouse_id, "start": start_date, "end": end_exclusive},
    ).mappings().all()
    return jsonify({
        "range": {"start": start, "end": end},
        "warehouse_id": warehouse_id,
        "activity": [dict(row) for row in rows],
        "confirmed_errors": [dict(row) for row in mistakes],
        "scoring_applied": False,
    })


def _employee_period_report(db, warehouse_id, employee, start_at, end_at):
    rows = db.execute(
        text(
            """
            SELECT task_type,
                   COUNT(*)::INT AS completed_tasks,
                   COALESCE(SUM(order_count), 0)::BIGINT AS orders_handled,
                   COALESCE(SUM(sku_count), 0)::BIGINT AS skus_handled,
                   COALESCE(SUM(unit_count), 0)::BIGINT AS units_handled,
                   COALESCE(SUM(active_seconds), 0)::BIGINT AS active_seconds,
                   COALESCE(SUM(paused_seconds), 0)::BIGINT AS paused_seconds,
                   COALESCE(ROUND(AVG(active_seconds)), 0)::INT AS average_active_seconds
              FROM work_tasks
             WHERE warehouse_id = :wid
               AND claimed_by = :employee
               AND status = 'COMPLETED'
               AND completed_at >= :start_at
               AND completed_at < :end_at
             GROUP BY task_type
             ORDER BY task_type
            """
        ),
        {
            "wid": warehouse_id,
            "employee": employee,
            "start_at": start_at,
            "end_at": end_at,
        },
    ).mappings().all()
    activity = [
        {
            "task_type": row["task_type"],
            "completed_tasks": int(row["completed_tasks"] or 0),
            "orders_handled": int(row["orders_handled"] or 0),
            "skus_handled": int(row["skus_handled"] or 0),
            "units_handled": int(row["units_handled"] or 0),
            "active_seconds": int(row["active_seconds"] or 0),
            "paused_seconds": int(row["paused_seconds"] or 0),
            "average_active_seconds": int(row["average_active_seconds"] or 0),
        }
        for row in rows
    ]
    summary = {
        "completed_tasks": sum(row["completed_tasks"] for row in activity),
        "orders_handled": sum(row["orders_handled"] for row in activity),
        "skus_handled": sum(row["skus_handled"] for row in activity),
        "units_handled": sum(row["units_handled"] for row in activity),
        "active_seconds": sum(row["active_seconds"] for row in activity),
        "paused_seconds": sum(row["paused_seconds"] for row in activity),
    }
    summary["average_active_seconds"] = (
        round(summary["active_seconds"] / summary["completed_tasks"])
        if summary["completed_tasks"] else 0
    )

    issue_counts = db.execute(
        text(
            """
            SELECT COUNT(*) FILTER (
                       WHERE reported_by = :employee
                         AND created_at >= :start_at AND created_at < :end_at
                   )::INT AS reported_issues,
                   COUNT(*) FILTER (
                       WHERE reported_by = :employee AND status = 'PENDING'
                         AND created_at >= :start_at AND created_at < :end_at
                   )::INT AS pending_reported_issues,
                   COUNT(*) FILTER (
                       WHERE status = 'CONFIRMED'
                         AND reviewed_at >= :start_at AND reviewed_at < :end_at
                         AND (
                              (picker_user_id = :employee AND responsibility IN ('PICKER','BOTH'))
                           OR (packer_user_id = :employee AND responsibility IN ('PACKER','BOTH'))
                         )
                   )::INT AS confirmed_mistakes
              FROM work_errors
             WHERE warehouse_id = :wid
            """
        ),
        {
            "wid": warehouse_id,
            "employee": employee,
            "start_at": start_at,
            "end_at": end_at,
        },
    ).mappings().one()
    summary.update({
        "reported_issues": int(issue_counts["reported_issues"] or 0),
        "pending_reported_issues": int(issue_counts["pending_reported_issues"] or 0),
        "confirmed_mistakes": int(issue_counts["confirmed_mistakes"] or 0),
    })

    recent_rows = db.execute(
        text(
            """
            SELECT wt.task_id, wt.task_type,
                   COALESCE(wb.pack_note_ref, wt.source_ref) AS reference,
                   wt.order_count, wt.sku_count, wt.unit_count,
                   wt.active_seconds, wt.paused_seconds, wt.completed_at
              FROM work_tasks wt
              LEFT JOIN work_batches wb ON wb.batch_id = wt.batch_id
             WHERE wt.warehouse_id = :wid
               AND wt.claimed_by = :employee
               AND wt.status = 'COMPLETED'
               AND wt.completed_at >= :start_at
               AND wt.completed_at < :end_at
             ORDER BY wt.completed_at DESC, wt.task_id DESC
             LIMIT 8
            """
        ),
        {
            "wid": warehouse_id,
            "employee": employee,
            "start_at": start_at,
            "end_at": end_at,
        },
    ).mappings().all()
    recent = [
        {
            **{key: value for key, value in row.items() if key != "completed_at"},
            "completed_at": row["completed_at"].isoformat(),
        }
        for row in recent_rows
    ]
    return {"summary": summary, "activity": activity, "recent": recent}


@work_control_bp.route("/reports/me", methods=["GET"])
@require_auth
@with_db
def employee_personal_report():
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    allowed, response = check_warehouse_access(warehouse_id)
    if not allowed:
        return response

    bounds = g.db.execute(
        text(
            """
            SELECT
                date_trunc('day', NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')
                    AT TIME ZONE 'Asia/Kuala_Lumpur' AS today_start,
                (date_trunc('day', NOW() AT TIME ZONE 'Asia/Kuala_Lumpur') + INTERVAL '1 day')
                    AT TIME ZONE 'Asia/Kuala_Lumpur' AS tomorrow_start,
                date_trunc('week', NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')
                    AT TIME ZONE 'Asia/Kuala_Lumpur' AS week_start,
                (NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::DATE AS today_date,
                date_trunc('week', NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::DATE AS week_date
            """
        )
    ).mappings().one()
    employee = _username()
    today_report = _employee_period_report(
        g.db, warehouse_id, employee,
        bounds["today_start"], bounds["tomorrow_start"],
    )
    week_report = _employee_period_report(
        g.db, warehouse_id, employee,
        bounds["week_start"], bounds["tomorrow_start"],
    )
    today_report["range"] = {
        "start": bounds["today_date"].isoformat(),
        "end": bounds["today_date"].isoformat(),
    }
    week_report["range"] = {
        "start": bounds["week_date"].isoformat(),
        "end": bounds["today_date"].isoformat(),
    }
    return jsonify({
        "warehouse_id": warehouse_id,
        "employee": employee,
        "full_name": g.current_user.get("full_name") or employee,
        "timezone": "Asia/Kuala_Lumpur",
        "periods": {"today": today_report, "week": week_report},
        "scoring_applied": False,
        "ranking_applied": False,
    })

