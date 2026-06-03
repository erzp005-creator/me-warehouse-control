"""Admin Picking Batches: list active pick batches and release stuck ones.

Operator escape hatch for the cross-pick lock. A sales order is "locked"
into picking purely by having a pick_batch_orders row that joins to a
pick_batches row whose status is OPEN or IN_PROGRESS (there is no flag on
the SO itself). When a pick round fails the batch can be left OPEN
forever -- nothing ages it out -- and neither the original picker nor
anyone else can pick those orders. This page lists the active batches and
lets an admin release one, which reuses the same full_revert_batch unwind
the mobile cancel-batch flow already runs: inventory allocations freed,
physically-picked units restored to their source bins, line counters
reset, audit rows written, batch marked CANCELLED. The SOs then drop back
to a clean pickable state.

Delete is SO-only. Transfer-order batches are listed read-only: TO picking
has no cross-pick lock (orders are never trapped) and a correct TO unwind
needs a state-machine change, so TO release stays on the Transfer Order
screen. The delete endpoint refuses TO batches with 409 as defense in
depth even though the UI disables the action.
"""

from flask import g, jsonify, request
from sqlalchemy import text

from constants import BATCH_OPEN, BATCH_IN_PROGRESS, TASK_PICKED, TASK_SHORT
from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from routes.admin import admin_bp
from services.picking_service import full_revert_batch


def _iso(dt):
    return dt.isoformat() if dt else None


@admin_bp.route("/pick-batches", methods=["GET"])
@require_auth
@require_admin_or_page_permission("picking-batches")
@with_db
def list_pick_batches():
    """List the active (OPEN / IN_PROGRESS) pick batches for a warehouse.

    These are exactly the batches that hold the cross-pick lock, so this
    is the queue an admin works to free stuck orders. COMPLETED /
    CANCELLED batches are terminal and never lock anything, so they are
    not listed.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    if warehouse_id is None:
        return jsonify({"error": "warehouse_id is required"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied

    batches = g.db.execute(
        text(
            """
            SELECT pb.batch_id, pb.batch_number, pb.status, pb.assigned_to,
                   pb.created_at, pb.started_at, pb.warehouse_id,
                   EXISTS (
                       SELECT 1 FROM pick_tasks pt
                        WHERE pt.batch_id = pb.batch_id
                          AND pt.to_id IS NOT NULL
                   ) AS is_to,
                   (SELECT COUNT(*) FROM pick_tasks pt
                     WHERE pt.batch_id = pb.batch_id) AS total_tasks,
                   (SELECT COUNT(*) FROM pick_tasks pt
                     WHERE pt.batch_id = pb.batch_id
                       AND pt.status IN (:t_picked, :t_short)) AS completed_tasks
              FROM pick_batches pb
             WHERE pb.warehouse_id = :wh
               AND pb.status IN (:b_open, :b_inprog)
             ORDER BY pb.created_at ASC
            """
        ),
        {
            "wh": warehouse_id,
            "b_open": BATCH_OPEN,
            "b_inprog": BATCH_IN_PROGRESS,
            "t_picked": TASK_PICKED,
            "t_short": TASK_SHORT,
        },
    ).fetchall()

    batch_ids = [b.batch_id for b in batches]
    orders_by_batch = {}
    to_number_by_batch = {}
    if batch_ids:
        # SO identifiers: one row per order in each SO batch.
        for row in g.db.execute(
            text(
                """
                SELECT pbo.batch_id, so.so_number
                  FROM pick_batch_orders pbo
                  JOIN sales_orders so ON so.so_id = pbo.so_id
                 WHERE pbo.batch_id = ANY(:ids)
                 ORDER BY pbo.batch_id, so.so_number
                """
            ),
            {"ids": batch_ids},
        ).fetchall():
            orders_by_batch.setdefault(row.batch_id, []).append(row.so_number)

        # TO identifier: a TO batch carries a single to_number across its
        # tasks (start-picking anchors one TO per batch).
        for row in g.db.execute(
            text(
                """
                SELECT DISTINCT pt.batch_id, o.to_number
                  FROM pick_tasks pt
                  JOIN transfer_orders o ON o.to_id = pt.to_id
                 WHERE pt.batch_id = ANY(:ids)
                """
            ),
            {"ids": batch_ids},
        ).fetchall():
            to_number_by_batch[row.batch_id] = row.to_number

    result = []
    for b in batches:
        total = int(b.total_tasks or 0)
        completed = int(b.completed_tasks or 0)
        result.append({
            "batch_id": b.batch_id,
            "batch_number": b.batch_number,
            "status": b.status,
            "kind": "TO" if b.is_to else "SO",
            "assigned_to": b.assigned_to,
            "created_at": _iso(b.created_at),
            "started_at": _iso(b.started_at),
            "warehouse_id": b.warehouse_id,
            "total_tasks": total,
            "completed_tasks": completed,
            "pending_tasks": total - completed,
            "orders": orders_by_batch.get(b.batch_id, []),
            "to_number": to_number_by_batch.get(b.batch_id),
        })

    return jsonify({"pick_batches": result})


@admin_bp.route("/pick-batches/<int:batch_id>/delete", methods=["POST"])
@require_auth
@require_admin_or_page_permission("picking-batches")
@with_db
def delete_pick_batch(batch_id):
    """Release a stuck SO pick batch: cancel it and unwind every effect so
    its orders are indistinguishable from "never started picking."

    Reuses full_revert_batch (the same primitive behind mobile
    cancel-batch): PENDING tasks free their inventory + line allocation;
    PICKED tasks restore units to their source bin and reset line
    counters; the batch is marked CANCELLED. Idempotent on terminal
    batches. Anything the picker physically pulled is restored in the WMS;
    the operator is responsible for the matching physical return.
    """
    batch = g.db.execute(
        text(
            "SELECT batch_id, status, warehouse_id FROM pick_batches "
            " WHERE batch_id = :bid"
        ),
        {"bid": batch_id},
    ).fetchone()
    if not batch:
        return jsonify({"error": "Batch not found"}), 404

    ok, denied = check_warehouse_access(batch.warehouse_id)
    if not ok:
        return denied

    # SO-only. A TO batch must not go through the SO unwind: TO picks book
    # no inventory at pick time and reversing a fully-picked TO line needs
    # a state-machine change full_revert_batch does not do. Refuse here as
    # defense in depth -- the UI already disables Delete for TO rows.
    is_to = g.db.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pick_tasks "
            " WHERE batch_id = :bid AND to_id IS NOT NULL)"
        ),
        {"bid": batch_id},
    ).scalar()
    if is_to:
        return jsonify({
            "error": "to_batch_not_deletable",
            "detail": (
                "This is a transfer-order pick batch. Release it from the "
                "Transfer Orders screen, not here."
            ),
        }), 409

    # Capture the orders being freed before the unwind for the response.
    freed = g.db.execute(
        text(
            """
            SELECT so.so_number
              FROM pick_batch_orders pbo
              JOIN sales_orders so ON so.so_id = pbo.so_id
             WHERE pbo.batch_id = :bid
             ORDER BY so.so_number
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    result = full_revert_batch(
        g.db, batch_id=batch_id, username=g.current_user["username"],
    )
    g.db.commit()

    return jsonify({
        "message": "Batch released",
        "batch_id": batch_id,
        "released_tasks": result["released_tasks"],
        "released_orders": [r.so_number for r in freed],
    })
