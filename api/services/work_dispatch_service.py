"""Automatic workload estimation and employee dispatch for Work Control.

The scheduler assigns execution tasks only. It deliberately does not score
employees, post inventory, or mutate SiteGiant data.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import text

from services.audit_service import write_audit_log
from services.work_control_service import record_task_event


TASK_FUNCTIONS = {
    "PICKING": "pick",
    "PACKING": "pack",
    "RECEIVING": "receive",
    "PUTAWAY": "putaway",
    "STOCK_CHECK": "count",
}
OPEN_STATUSES = ("QUEUED", "ASSIGNED", "CLAIMED", "IN_PROGRESS", "PAUSED")
ACTIVE_STATUSES = ("CLAIMED", "IN_PROGRESS", "PAUSED")
DEFAULT_RATES = {"PICKING": 30.0, "PACKING": 40.0}
RATE_SETTING_KEYS = {
    "PICKING": "work_control_picking_minutes_per_50",
    "PACKING": "work_control_packing_minutes_per_50",
}
COMPLEXITY_MULTIPLIERS = {1: 0.75, 2: 1.0, 3: 1.35, 4: 1.75, 5: 2.25}


def _number(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _task_value(task, key, fallback=None):
    if hasattr(task, "_mapping"):
        return task._mapping.get(key, fallback)
    if isinstance(task, dict):
        return task.get(key, fallback)
    return getattr(task, key, fallback)


def load_dispatch_rates(db, warehouse_id):
    """Use the same calibrated picking/packing baseline as the forecast."""
    rows = db.execute(
        text(
            """
            SELECT key, value FROM app_settings
             WHERE key IN (:pick_key, :pack_key)
            """
        ),
        {
            "pick_key": RATE_SETTING_KEYS["PICKING"],
            "pack_key": RATE_SETTING_KEYS["PACKING"],
        },
    ).mappings().all()
    settings = {row["key"]: row["value"] for row in rows}
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
             WHERE warehouse_id = :wid
               AND task_type IN ('PICKING', 'PACKING')
               AND status = 'COMPLETED'
               AND completed_at >= NOW() - INTERVAL '30 days'
               AND order_count > 0
               AND active_seconds >= 60
             GROUP BY task_type
            """
        ),
        {"wid": warehouse_id},
    ).mappings().all()
    history = {row["task_type"]: row for row in history_rows}

    rates = {}
    for task_type in ("PICKING", "PACKING"):
        baseline = _number(
            settings.get(RATE_SETTING_KEYS[task_type]),
            DEFAULT_RATES[task_type],
        )
        if not 1 <= baseline <= 240:
            baseline = DEFAULT_RATES[task_type]
        sample = history.get(task_type)
        use_history = bool(
            sample
            and int(sample["completed_tasks"] or 0) >= 5
            and int(sample["completed_orders"] or 0) >= 100
            and sample["median_minutes_per_50"] is not None
        )
        rates[task_type] = round(
            _number(sample["median_minutes_per_50"])
            if use_history else baseline,
            1,
        )
    return rates


def estimate_task_minutes(task, rates):
    """Estimate labour minutes from task size and receiving complexity."""
    task_type = _task_value(task, "task_type")
    orders = max(0, int(_task_value(task, "order_count", 0) or 0))
    skus = max(0, int(_task_value(task, "sku_count", 0) or 0))
    units = max(0, int(_task_value(task, "unit_count", 0) or 0))
    level = int(_task_value(task, "complexity_level", 2) or 2)
    multiplier = COMPLEXITY_MULTIPLIERS.get(level, 1.0)

    if task_type in ("PICKING", "PACKING"):
        minutes = rates[task_type] * (orders or 50) / 50.0
    elif task_type == "RECEIVING":
        minutes = (4.0 + skus * 2.5 + units * 0.08) * multiplier
    elif task_type == "PUTAWAY":
        minutes = (3.0 + skus + units * 0.05) * multiplier
    elif task_type == "STOCK_CHECK":
        minutes = (4.0 + skus * 1.5 + units * 0.08) * multiplier
    else:
        minutes = 15.0 * multiplier
    return round(max(1.0, minutes), 1)


def _eligible_workers(db, warehouse_id):
    rows = db.execute(
        text(
            """
            SELECT u.user_id, u.username, u.full_name, u.allowed_functions,
                   COALESCE(ws.availability_status, 'AVAILABLE') AS availability_status,
                   COALESCE(ws.daily_capacity_minutes, 480) AS daily_capacity_minutes,
                   ws.status_note, ws.updated_at AS status_updated_at
              FROM users u
              LEFT JOIN work_worker_status ws
                ON ws.user_id = u.user_id
               AND ws.warehouse_id = :wid
               AND ws.work_date = CURRENT_DATE
             WHERE u.role = 'USER'
               AND u.is_active = TRUE
               AND (u.warehouse_id = :wid OR :wid = ANY(u.warehouse_ids))
             ORDER BY LOWER(u.full_name), LOWER(u.username)
            """
        ),
        {"wid": warehouse_id},
    ).mappings().all()
    workers = []
    for row in rows:
        functions = set(row["allowed_functions"] or [])
        task_types = [
            task_type for task_type, function in TASK_FUNCTIONS.items()
            if function in functions
        ]
        if not task_types:
            continue
        workers.append({
            **dict(row),
            "allowed_task_types": task_types,
        })
    return workers


def _open_tasks(db, warehouse_id, *, for_update=False):
    suffix = " FOR UPDATE OF wt" if for_update else ""
    return db.execute(
        text(
            """
            SELECT wt.task_id, wt.batch_id, wt.task_type, wt.status, wt.priority,
                   wt.assigned_to, wt.claimed_by, wt.source_ref, wt.order_count,
                   wt.sku_count, wt.unit_count, wt.complexity_level,
                   wt.estimated_minutes, wt.assignment_reason, wt.available_at,
                   wt.active_seconds, wt.active_started_at, wt.created_at,
                   wb.pack_note_ref, wb.platform
              FROM work_tasks wt
              LEFT JOIN work_batches wb ON wb.batch_id = wt.batch_id
             WHERE wt.warehouse_id = :wid
               AND wt.status IN ('QUEUED','ASSIGNED','CLAIMED','IN_PROGRESS','PAUSED')
             ORDER BY wt.priority DESC, wt.available_at, wt.task_id
            """ + suffix
        ),
        {"wid": warehouse_id},
    ).mappings().all()


def _remaining_minutes(task, estimated):
    active_seconds = int(task.get("active_seconds") or 0)
    active_started_at = task.get("active_started_at")
    if task.get("status") == "IN_PROGRESS" and active_started_at:
        now = datetime.now(timezone.utc)
        if active_started_at.tzinfo is None:
            active_started_at = active_started_at.replace(tzinfo=timezone.utc)
        active_seconds += max(0, int((now - active_started_at).total_seconds()))
    return round(max(0.0, estimated - active_seconds / 60.0), 1)


def _counterpart_workers(db, warehouse_id):
    rows = db.execute(
        text(
            """
            SELECT batch_id, task_type, COALESCE(claimed_by, assigned_to) AS worker
              FROM work_tasks
             WHERE warehouse_id = :wid
               AND batch_id IS NOT NULL
               AND task_type IN ('PICKING', 'PACKING')
               AND COALESCE(claimed_by, assigned_to) IS NOT NULL
            """
        ),
        {"wid": warehouse_id},
    ).mappings().all()
    result = defaultdict(set)
    for row in rows:
        result[row["batch_id"]].add(row["worker"])
    return result


def auto_dispatch(db, warehouse_id, actor, *, device_id="dispatch-engine"):
    """Assign every ready unowned task to the lowest projected workload."""
    rates = load_dispatch_rates(db, warehouse_id)
    workers = _eligible_workers(db, warehouse_id)
    available = {
        worker["username"]: worker
        for worker in workers
        if worker["availability_status"] == "AVAILABLE"
    }
    tasks = _open_tasks(db, warehouse_id, for_update=True)
    projected = defaultdict(float)
    assigned_counts = defaultdict(int)
    task_minutes = {}

    for task in tasks:
        estimate = _number(task["estimated_minutes"], 0.0)
        if estimate <= 0:
            estimate = estimate_task_minutes(task, rates)
        task_minutes[task["task_id"]] = estimate
        owner = task["claimed_by"] or task["assigned_to"]
        if owner:
            projected[owner] += _remaining_minutes(task, estimate)
            assigned_counts[owner] += 1

    counterpart_workers = _counterpart_workers(db, warehouse_id)
    assignments = []
    for task in tasks:
        if task["status"] != "QUEUED" or task["assigned_to"]:
            continue
        if task["available_at"] and task["available_at"] > datetime.now(timezone.utc):
            continue
        candidates = []
        for username, worker in available.items():
            if task["task_type"] not in worker["allowed_task_types"]:
                continue
            if (
                task["batch_id"] is not None
                and task["task_type"] in ("PICKING", "PACKING")
                and username in counterpart_workers[task["batch_id"]]
            ):
                continue
            candidates.append(worker)
        if not candidates:
            continue
        chosen = min(
            candidates,
            key=lambda worker: (
                projected[worker["username"]],
                assigned_counts[worker["username"]],
                worker["username"].lower(),
            ),
        )
        username = chosen["username"]
        estimate = task_minutes[task["task_id"]]
        before = round(projected[username], 1)
        reason = (
            f"Auto-dispatch: lowest projected workload ({before:g} min before task) "
            f"among {len(candidates)} eligible available staff"
        )
        db.execute(
            text(
                """
                UPDATE work_tasks
                   SET status = 'ASSIGNED', assigned_to = :username,
                       estimated_minutes = :estimate,
                       assignment_reason = :reason, assigned_at = NOW(),
                       assigned_by = :actor, updated_at = NOW()
                 WHERE task_id = :tid
                """
            ),
            {
                "username": username,
                "estimate": estimate,
                "reason": reason,
                "actor": actor,
                "tid": task["task_id"],
            },
        )
        record_task_event(
            db,
            task["task_id"],
            "ASSIGNED",
            actor,
            reason_code="AUTO_DISPATCH",
            notes=reason,
            metadata={
                "assigned_to": username,
                "estimated_minutes": estimate,
                "projected_minutes_before": before,
            },
            device_id=device_id,
        )
        write_audit_log(
            db,
            "WORK_TASK_AUTO_ASSIGNED",
            "WORK_TASK",
            task["task_id"],
            actor,
            warehouse_id,
            {
                "assigned_to": username,
                "estimated_minutes": estimate,
                "assignment_reason": reason,
            },
            device_id=device_id,
        )
        projected[username] += estimate
        assigned_counts[username] += 1
        if task["batch_id"] is not None:
            counterpart_workers[task["batch_id"]].add(username)
        assignments.append({
            "task_id": task["task_id"],
            "assigned_to": username,
            "estimated_minutes": estimate,
            "assignment_reason": reason,
        })
    return assignments


def build_dispatch_overview(db, warehouse_id):
    rates = load_dispatch_rates(db, warehouse_id)
    workers = _eligible_workers(db, warehouse_id)
    tasks = _open_tasks(db, warehouse_id)
    completed_rows = db.execute(
        text(
            """
            SELECT claimed_by AS username, COUNT(*) AS completed_tasks,
                   COALESCE(SUM(active_seconds), 0) AS active_seconds
              FROM work_tasks
             WHERE warehouse_id = :wid
               AND status = 'COMPLETED'
               AND completed_at >= CURRENT_DATE
               AND claimed_by IS NOT NULL
             GROUP BY claimed_by
            """
        ),
        {"wid": warehouse_id},
    ).mappings().all()
    completed = {row["username"]: row for row in completed_rows}

    schedules = defaultdict(list)
    unassigned = []
    for task in tasks:
        estimate = _number(task["estimated_minutes"], 0.0)
        if estimate <= 0:
            estimate = estimate_task_minutes(task, rates)
        item = {
            "task_id": task["task_id"],
            "batch_id": task["batch_id"],
            "task_type": task["task_type"],
            "status": task["status"],
            "priority": task["priority"],
            "reference": task["pack_note_ref"] or task["source_ref"],
            "order_count": task["order_count"],
            "sku_count": task["sku_count"],
            "unit_count": task["unit_count"],
            "complexity_level": task["complexity_level"],
            "estimated_minutes": estimate,
            "remaining_minutes": _remaining_minutes(task, estimate),
            "assignment_reason": task["assignment_reason"],
        }
        owner = task["claimed_by"] or task["assigned_to"]
        if owner:
            schedules[owner].append(item)
        else:
            unassigned.append(item)

    worker_payload = []
    for worker in workers:
        username = worker["username"]
        schedule = schedules[username]
        schedule.sort(key=lambda item: (
            0 if item["status"] in ACTIVE_STATUSES else 1,
            -item["priority"],
            item["task_id"],
        ))
        scheduled_minutes = round(sum(item["remaining_minutes"] for item in schedule), 1)
        capacity = int(worker["daily_capacity_minutes"])
        done = completed.get(username, {})
        worker_payload.append({
            "user_id": worker["user_id"],
            "username": username,
            "full_name": worker["full_name"],
            "availability_status": worker["availability_status"],
            "daily_capacity_minutes": capacity,
            "status_note": worker["status_note"],
            "status_updated_at": (
                worker["status_updated_at"].isoformat()
                if worker["status_updated_at"] else None
            ),
            "allowed_task_types": worker["allowed_task_types"],
            "scheduled_minutes": scheduled_minutes,
            "capacity_percent": round(scheduled_minutes * 100 / capacity, 1),
            "scheduled_task_count": len(schedule),
            "current_task": schedule[0] if schedule and schedule[0]["status"] in ACTIVE_STATUSES else None,
            "next_tasks": [item for item in schedule if item["status"] not in ACTIVE_STATUSES][:4],
            "completed_tasks_today": int(done.get("completed_tasks") or 0),
            "active_minutes_today": round(int(done.get("active_seconds") or 0) / 60.0, 1),
        })

    available_workers = [
        worker for worker in worker_payload
        if worker["availability_status"] == "AVAILABLE"
    ]
    total_scheduled = round(sum(worker["scheduled_minutes"] for worker in worker_payload), 1)
    total_capacity = sum(worker["daily_capacity_minutes"] for worker in available_workers)
    unassigned_minutes = round(sum(item["estimated_minutes"] for item in unassigned), 1)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workers": worker_payload,
        "unassigned_tasks": unassigned[:50],
        "summary": {
            "available_workers": len(available_workers),
            "total_workers": len(worker_payload),
            "scheduled_minutes": total_scheduled,
            "available_capacity_minutes": total_capacity,
            "capacity_percent": round(total_scheduled * 100 / total_capacity, 1)
            if total_capacity else None,
            "unassigned_tasks": len(unassigned),
            "unassigned_minutes": unassigned_minutes,
            "estimated_clear_minutes": max(
                [worker["scheduled_minutes"] for worker in available_workers] or [0]
            ),
        },
        "policy": {
            "strategy": "lowest_projected_minutes",
            "cross_check": True,
            "one_active_task_per_employee": True,
            "picking_minutes_per_50": rates["PICKING"],
            "packing_minutes_per_50": rates["PACKING"],
        },
    }


def set_worker_availability(
    db,
    warehouse_id,
    user_id,
    status,
    capacity_minutes,
    actor,
    *,
    status_note=None,
):
    worker = db.execute(
        text(
            """
            SELECT user_id, username, role, is_active, warehouse_id, warehouse_ids
              FROM users
             WHERE user_id = :uid
            """
        ),
        {"uid": user_id},
    ).mappings().first()
    if worker is None:
        raise LookupError("Employee not found")
    has_warehouse = (
        worker["warehouse_id"] == warehouse_id
        or warehouse_id in (worker["warehouse_ids"] or [])
    )
    if worker["role"] != "USER" or not worker["is_active"] or not has_warehouse:
        raise ValueError("Employee is not active in this warehouse")

    db.execute(
        text(
            """
            INSERT INTO work_worker_status
                (warehouse_id, user_id, work_date, availability_status,
                 daily_capacity_minutes, status_note, updated_by)
            VALUES (:wid, :uid, CURRENT_DATE, :status, :capacity, :note, :actor)
            ON CONFLICT (warehouse_id, user_id, work_date)
            DO UPDATE SET availability_status = EXCLUDED.availability_status,
                          daily_capacity_minutes = EXCLUDED.daily_capacity_minutes,
                          status_note = EXCLUDED.status_note,
                          updated_by = EXCLUDED.updated_by,
                          updated_at = NOW()
            """
        ),
        {
            "wid": warehouse_id,
            "uid": user_id,
            "status": status,
            "capacity": capacity_minutes,
            "note": status_note,
            "actor": actor,
        },
    )

    released = []
    if status != "AVAILABLE":
        rows = db.execute(
            text(
                """
                SELECT task_id FROM work_tasks
                 WHERE warehouse_id = :wid
                   AND assigned_to = :username
                   AND claimed_by IS NULL
                   AND status = 'ASSIGNED'
                 FOR UPDATE
                """
            ),
            {"wid": warehouse_id, "username": worker["username"]},
        ).fetchall()
        for row in rows:
            db.execute(
                text(
                    """
                    UPDATE work_tasks
                       SET status = 'QUEUED', assigned_to = NULL,
                           assignment_reason = :reason, assigned_at = NULL,
                           assigned_by = :actor, updated_at = NOW()
                     WHERE task_id = :tid
                    """
                ),
                {
                    "tid": row.task_id,
                    "reason": f"Released because {worker['username']} is {status.lower()}",
                    "actor": actor,
                },
            )
            record_task_event(
                db,
                row.task_id,
                "REOPENED",
                actor,
                reason_code="WORKER_UNAVAILABLE",
                metadata={"previous_assignee": worker["username"], "status": status},
            )
            released.append(row.task_id)

    write_audit_log(
        db,
        "WORKER_AVAILABILITY_CHANGED",
        "USER",
        user_id,
        actor,
        warehouse_id,
        {
            "status": status,
            "daily_capacity_minutes": capacity_minutes,
            "status_note": status_note,
            "released_task_ids": released,
        },
    )
    assignments = auto_dispatch(db, warehouse_id, actor)
    return {"released_task_ids": released, "assignments": assignments}


def assign_task(db, task_id, assigned_to, actor, *, reason=None):
    task = db.execute(
        text("SELECT * FROM work_tasks WHERE task_id = :tid FOR UPDATE"),
        {"tid": task_id},
    ).mappings().first()
    if task is None:
        raise LookupError("Task not found")
    if task["status"] not in ("QUEUED", "ASSIGNED") or task["claimed_by"]:
        raise ValueError("Only waiting tasks can be reassigned")

    previous = task["assigned_to"]
    if assigned_to:
        workers = {
            worker["username"]: worker
            for worker in _eligible_workers(db, task["warehouse_id"])
        }
        worker = workers.get(assigned_to)
        if worker is None or task["task_type"] not in worker["allowed_task_types"]:
            raise ValueError("Employee is not eligible for this work type")
        if worker["availability_status"] != "AVAILABLE":
            raise ValueError("Employee is not currently available for new work")
        if task["batch_id"] is not None and task["task_type"] in ("PICKING", "PACKING"):
            conflict = db.execute(
                text(
                    """
                    SELECT 1 FROM work_tasks
                     WHERE batch_id = :bid
                       AND task_type IN ('PICKING', 'PACKING')
                       AND task_type <> :task_type
                       AND COALESCE(claimed_by, assigned_to) = :username
                     LIMIT 1
                    """
                ),
                {
                    "bid": task["batch_id"],
                    "task_type": task["task_type"],
                    "username": assigned_to,
                },
            ).first()
            if conflict:
                raise ValueError("Picker and packer for one Pack Note must be different employees")

    rates = load_dispatch_rates(db, task["warehouse_id"])
    estimate = _number(task["estimated_minutes"], 0.0) or estimate_task_minutes(task, rates)
    assignment_reason = reason or (
        f"Manual assignment by {actor}" if assigned_to else f"Returned to auto queue by {actor}"
    )
    db.execute(
        text(
            """
            UPDATE work_tasks
               SET status = CASE WHEN :assigned_to IS NULL THEN 'QUEUED' ELSE 'ASSIGNED' END,
                   assigned_to = :assigned_to, estimated_minutes = :estimate,
                   assignment_reason = :reason,
                   assigned_at = CASE WHEN :assigned_to IS NULL THEN NULL ELSE NOW() END,
                   assigned_by = :actor, updated_at = NOW()
             WHERE task_id = :tid
            """
        ),
        {
            "assigned_to": assigned_to,
            "estimate": estimate,
            "reason": assignment_reason,
            "actor": actor,
            "tid": task_id,
        },
    )
    record_task_event(
        db,
        task_id,
        "ASSIGNED" if assigned_to else "REOPENED",
        actor,
        reason_code="MANUAL_DISPATCH",
        notes=assignment_reason,
        metadata={"previous_assignee": previous, "assigned_to": assigned_to},
    )
    write_audit_log(
        db,
        "WORK_TASK_MANUALLY_ASSIGNED",
        "WORK_TASK",
        task_id,
        actor,
        task["warehouse_id"],
        {
            "previous_assignee": previous,
            "assigned_to": assigned_to,
            "assignment_reason": assignment_reason,
        },
    )
    return task["warehouse_id"]
