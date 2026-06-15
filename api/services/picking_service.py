"""
Core picking business logic - batch creation, wave picking, pick confirmation,
short picks, batch completion.
"""

from datetime import datetime, timezone

from flask import g, has_request_context
from sqlalchemy import text

from services.audit_service import write_audit_log
from services.connector_stub import enrich_order
from services.events_service import emit_event, get_user_external_id
from services.inventory_service import add_inventory

from constants import (
    BATCH_OPEN, BATCH_IN_PROGRESS, BATCH_COMPLETED, BATCH_CANCELLED,
    SO_OPEN, SO_PICKED,
    TASK_PENDING, TASK_PICKED, TASK_SHORT, TASK_SKIPPED, TASK_RELEASED,
    ACTION_PICK, ACTION_SO_PICK_RELEASED, ACTION_SO_ALLOCATION_RELEASED,
    BIN_PICKABLE, BIN_PICKABLE_STAGING,
)


class InsufficientCoverageError(Exception):
    """Raised at batch creation when one or more SOs cannot be fully covered
    by pickable inventory in the target warehouse. Carries a structured list
    of unpickable SOs so the picker UI can surface a per-SKU shortfall modal
    and offer to drop those SOs from the batch.

    The historical bug this guards against: the inner allocation loop in
    create_pick_batch / wave_create would exit silently with remaining > 0
    when inventory was exhausted, creating fewer pick_tasks than the SO
    needed. complete_batch's pending-task count would then read zero, the
    SO would flip through PICKED -> PACKED -> SHIPPED, and the customer
    would receive a short shipment with no in-system trace.
    """
    def __init__(self, unpickable):
        self.unpickable = unpickable
        super().__init__(
            f"{len(unpickable)} sales order(s) cannot be fully picked from current inventory"
        )


def _plan_coverage(db, so_lines_by_item, warehouse_id):
    """Given a map of item_id -> list of {so_id, so_number, so_line_id, needed},
    lock the candidate inventory rows FOR UPDATE and walk contributors in
    FIFO order (by so_id then so_line_id) to decide which SOs can be fully
    covered. Returns (unpickable_by_so, inv_rows_by_item) where:

      unpickable_by_so: dict[so_id, {so_number, lines: [...]}]
      inv_rows_by_item: dict[item_id, fetched inventory rows] - returned so
                       callers reuse the same locked snapshot for the real
                       allocation pass without re-querying (which would
                       re-lock the same rows in the same transaction; cheap
                       but redundant).

    FIFO-by-so_id was chosen so two callers running back-to-back from the
    same input list make the same drop decisions. Any deterministic order
    works; so_id is stable and obvious in the audit trail.
    """
    unpickable_by_so = {}
    inv_rows_by_item = {}

    for item_id, contributions in so_lines_by_item.items():
        inv_rows = db.execute(
            text(
                """
                SELECT inv.inventory_id, inv.bin_id, inv.quantity_on_hand, inv.quantity_allocated,
                       (inv.quantity_on_hand - inv.quantity_allocated) AS available,
                       b.pick_sequence, b.bin_type, inv.lot_number, inv.updated_at
                FROM inventory inv
                JOIN bins b ON b.bin_id = inv.bin_id
                WHERE inv.item_id = :item_id
                  AND inv.warehouse_id = :wh
                  AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                  AND b.bin_type IN (:bin_pickable, :bin_pickable_staging)
                ORDER BY
                  b.pick_sequence ASC,
                  inv.updated_at ASC
                FOR UPDATE OF inv
                """
            ),
            {"item_id": item_id, "wh": warehouse_id, "bin_pickable": BIN_PICKABLE, "bin_pickable_staging": BIN_PICKABLE_STAGING},
        ).fetchall()
        inv_rows_by_item[item_id] = inv_rows

        total_available = sum(r.available for r in inv_rows)

        item_info = db.execute(
            text("SELECT sku, item_name FROM items WHERE item_id = :iid"),
            {"iid": item_id},
        ).fetchone()

        sorted_contribs = sorted(contributions, key=lambda c: (c["so_id"], c["so_line_id"]))
        cumulative = 0
        for c in sorted_contribs:
            if cumulative + c["needed"] <= total_available:
                cumulative += c["needed"]
                continue
            so_id = c["so_id"]
            entry = unpickable_by_so.setdefault(
                so_id, {"so_id": so_id, "so_number": c["so_number"], "lines": []}
            )
            entry["lines"].append({
                "sku": item_info.sku if item_info else None,
                "item_name": item_info.item_name if item_info else None,
                "ordered": c["needed"],
                "available": max(0, total_available - cumulative),
            })

    return unpickable_by_so, inv_rows_by_item


def cancel_prior_user_batches(db, username, warehouse_id):
    """No-Resume rule: a new pick session implicitly invalidates any
    in-progress session for the same operator. Walks the user's
    OPEN / IN_PROGRESS batches in
    this warehouse and runs full_revert_batch on each so inventory
    and line state are restored before the new batch is created.
    Returns the list of cancelled batch_ids for logging / callers
    that want to surface "you abandoned N prior batches" in the UI.

    Scope choice: warehouse_id filter keeps the operator-walks-to-a-
    different-warehouse case clean (do not nuke a batch the user is
    actively running in warehouse A just because they scanned in
    warehouse B). Scanners are warehouse-pinned in practice, so this
    is conservative correctness."""
    prior = db.execute(
        text(
            "SELECT batch_id FROM pick_batches "
            " WHERE assigned_to = :user "
            "   AND warehouse_id = :wh "
            "   AND status IN (:s_open, :s_inprog) "
            "ORDER BY batch_id"
        ),
        {"user": username, "wh": warehouse_id,
         "s_open": BATCH_OPEN, "s_inprog": BATCH_IN_PROGRESS},
    ).fetchall()
    cancelled = []
    for row in prior:
        full_revert_batch(db, batch_id=row.batch_id, username=username)
        cancelled.append(row.batch_id)
    return cancelled


def create_pick_batch(db, so_identifiers, warehouse_id, username, exclude_so_ids=None):
    exclude_set = set(exclude_so_ids or [])

    # No-Resume rule: scrub any in-progress batches this user
    # abandoned before starting fresh. Runs first so the per-SO
    # active-batch guard below sees a clean slate -- otherwise a
    # user whose prior batch contained one of the re-scanned SOs
    # would hit "already in active pick batch" on what is now
    # effectively a fresh session.
    cancel_prior_user_batches(db, username=username, warehouse_id=warehouse_id)

    # 1. Resolve SOs (excluded identifiers are silently dropped here; the
    # picker UI already collected them in a prior 409 response, so we don't
    # re-error on them).
    sales_orders = []
    for ident in so_identifiers:
        so = db.execute(
            text(
                """
                SELECT so_id, so_number, so_barcode, status, warehouse_id
                FROM sales_orders
                WHERE (so_number = :ident OR so_barcode = :ident)
                  AND warehouse_id = :wh
                LIMIT 1
                """
            ),
            {"ident": ident, "wh": warehouse_id},
        ).fetchone()

        if not so:
            raise ValueError(f"Sales order '{ident}' not found")
        if so.so_id in exclude_set:
            continue
        if so.status != SO_OPEN:
            raise ValueError(f"Sales order '{ident}' status is {so.status}, must be OPEN")

        # PICKING was retired as a distinct status; an OPEN SO can still be
        # sitting inside an active batch, so guard the second batch attempt
        # against the pick_batches aggregate rather than the SO row.
        active = db.execute(
            text(
                """
                SELECT pb.batch_id FROM pick_batch_orders pbo
                JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
                WHERE pbo.so_id = :so_id
                  AND pb.status IN (:batch_open, :batch_in_progress)
                LIMIT 1
                """
            ),
            {"so_id": so.so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
        ).fetchone()
        if active:
            raise ValueError(
                f"Sales order '{ident}' is already in active pick batch {active.batch_id}"
            )

        sales_orders.append(so)

    if not sales_orders:
        raise ValueError("No sales orders to pick (all excluded or empty input)")

    # 1.5 Coverage pre-flight. Build the item -> contributors map FIRST so
    # we can decide which SOs are unpickable BEFORE we insert any
    # pick_batches / pick_batch_orders / inventory allocation rows. The
    # FOR UPDATE locks acquired by _plan_coverage persist through the
    # rest of this transaction so the real allocation loop below sees
    # the same inventory snapshot.
    coverage_map = {}
    for so in sales_orders:
        lines = db.execute(
            text(
                """
                SELECT so_line_id, item_id, quantity_ordered, quantity_allocated
                FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_ordered > quantity_allocated
                """
            ),
            {"so_id": so.so_id},
        ).fetchall()
        for line in lines:
            needed = line.quantity_ordered - line.quantity_allocated
            if needed <= 0:
                continue
            coverage_map.setdefault(line.item_id, []).append({
                "so_id": so.so_id,
                "so_number": so.so_number,
                "so_line_id": line.so_line_id,
                "needed": needed,
            })

    unpickable, _ = _plan_coverage(db, coverage_map, warehouse_id)
    if unpickable:
        # Drop any locks + partial state. The route layer translates the
        # raised exception into a 409 with the structured unpickable list.
        db.rollback()
        raise InsufficientCoverageError(list(unpickable.values()))

    # 2. Generate batch number
    now = datetime.now(timezone.utc)
    # Microsecond resolution guards against two batches in the same
    # second colliding on pick_batches.batch_number UNIQUE -- the
    # tighter suffix collapses the second-grain race that surfaced
    # when the test suite creates multiple batches in quick succession.
    batch_number = f"BATCH-{now.strftime('%Y%m%d-%H%M%S-%f')}"

    # 3. Create pick_batches record
    result = db.execute(
        text(
            """
            INSERT INTO pick_batches (batch_number, warehouse_id, assigned_to, status)
            VALUES (:batch_number, :warehouse_id, :assigned_to, :status)
            RETURNING batch_id
            """
        ),
        {"batch_number": batch_number, "warehouse_id": warehouse_id, "assigned_to": username, "status": BATCH_OPEN},
    )
    batch_id = result.fetchone()[0]

    # 4. Create pick_batch_orders and assign totes
    orders_info = []
    for idx, so in enumerate(sales_orders, 1):
        tote_number = f"TOTE-{idx}"
        db.execute(
            text(
                """
                INSERT INTO pick_batch_orders (batch_id, so_id, tote_number)
                VALUES (:batch_id, :so_id, :tote_number)
                """
            ),
            {"batch_id": batch_id, "so_id": so.so_id, "tote_number": tote_number},
        )
        orders_info.append({"so_id": so.so_id, "so_number": so.so_number, "tote_number": tote_number})

    # 5. For each SO, for each line, allocate inventory and create pick tasks
    total_items = 0
    for order in orders_info:
        so_id = order["so_id"]
        tote_number = order["tote_number"]

        lines = db.execute(
            text(
                """
                SELECT so_line_id, item_id, quantity_ordered, quantity_allocated
                FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_ordered > quantity_allocated
                """
            ),
            {"so_id": so_id},
        ).fetchall()

        for line in lines:
            needed = line.quantity_ordered - line.quantity_allocated
            if needed <= 0:
                continue

            # Find available inventory sorted by bin type preference, then FIFO
            # V-030: lock every candidate inventory row before reading
            # quantity_allocated so two concurrent batch-creates cannot
            # both allocate the same stock. FOR UPDATE OF inv restricts
            # the lock to the inventory table (bins is read-only here).
            inv_rows = db.execute(
                text(
                    """
                    SELECT inv.inventory_id, inv.bin_id, inv.quantity_on_hand, inv.quantity_allocated,
                           (inv.quantity_on_hand - inv.quantity_allocated) AS available,
                           b.pick_sequence, b.bin_type, inv.lot_number
                    FROM inventory inv
                    JOIN bins b ON b.bin_id = inv.bin_id
                    WHERE inv.item_id = :item_id
                      AND inv.warehouse_id = :wh
                      AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                      AND b.bin_type IN (:bin_pickable, :bin_pickable_staging)
                    ORDER BY
                      b.pick_sequence ASC,
                      inv.updated_at ASC
                    FOR UPDATE OF inv
                    """
                ),
                {"item_id": line.item_id, "wh": warehouse_id, "bin_pickable": BIN_PICKABLE, "bin_pickable_staging": BIN_PICKABLE_STAGING},
            ).fetchall()

            remaining = needed
            for inv in inv_rows:
                if remaining <= 0:
                    break

                take = min(remaining, inv.available)

                # Increment inventory.quantity_allocated
                db.execute(
                    text(
                        "UPDATE inventory SET quantity_allocated = quantity_allocated + :qty WHERE inventory_id = :inv_id"
                    ),
                    {"qty": take, "inv_id": inv.inventory_id},
                )

                # Increment sales_order_lines.quantity_allocated
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_allocated = quantity_allocated + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": take, "sol_id": line.so_line_id},
                )

                # Create pick_tasks record
                db.execute(
                    text(
                        """
                        INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, bin_id,
                                                quantity_to_pick, pick_sequence, tote_number, status)
                        VALUES (:batch_id, :so_id, :so_line_id, :item_id, :bin_id,
                                :qty, :pick_seq, :tote, :task_status)
                        """
                    ),
                    {
                        "batch_id": batch_id,
                        "so_id": so_id,
                        "so_line_id": line.so_line_id,
                        "item_id": line.item_id,
                        "bin_id": inv.bin_id,
                        "qty": take,
                        "pick_seq": inv.pick_sequence,
                        "tote": tote_number,
                        "task_status": TASK_PENDING,
                    },
                )

                total_items += take
                remaining -= take

    # SOs remain in OPEN status while the batch is active. "Currently
    # being picked" is derived from pick_batch_orders + pick_batches.status
    # rather than an SO-level status, which avoids two-source drift between
    # the SO row and the batch aggregate.

    # 6. Update batch totals
    db.execute(
        text(
            "UPDATE pick_batches SET total_orders = :orders, total_items = :items WHERE batch_id = :bid"
        ),
        {"orders": len(sales_orders), "items": total_items, "bid": batch_id},
    )

    # 8. Get the full task list
    tasks = _get_tasks_for_batch(db, batch_id)

    db.commit()

    return {
        "batch_id": batch_id,
        "batch_number": batch_number,
        "status": BATCH_OPEN,
        "total_orders": len(sales_orders),
        "total_items": total_items,
        "orders": [{"so_number": o["so_number"], "tote_number": o["tote_number"]} for o in orders_info],
        "tasks": tasks,
    }


def get_batch_tasks(db, batch_id):
    batch = db.execute(
        text(
            """
            SELECT batch_id, batch_number, status, assigned_to, total_orders, total_items,
                   created_at, started_at, completed_at, warehouse_id
            FROM pick_batches WHERE batch_id = :bid
            """
        ),
        {"bid": batch_id},
    ).fetchone()

    if not batch:
        return None

    orders = db.execute(
        text(
            """
            SELECT so.so_number, pbo.tote_number
            FROM pick_batch_orders pbo
            JOIN sales_orders so ON so.so_id = pbo.so_id
            WHERE pbo.batch_id = :bid
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    tasks = _get_tasks_for_batch(db, batch_id)

    # v1.8.0 (#295): TO batch detection. pick_batch_orders is empty for
    # a TO batch (no SO joins); the TO header surfaces via pick_tasks
    # discriminator -> transfer_orders.
    to_row = db.execute(
        text(
            """
            SELECT DISTINCT pt.to_id, o.to_number
              FROM pick_tasks pt
              JOIN transfer_orders o ON o.to_id = pt.to_id
             WHERE pt.batch_id = :bid
             LIMIT 1
            """
        ),
        {"bid": batch_id},
    ).fetchone()
    kind = "TO" if to_row else "SO"

    return {
        "batch_id": batch.batch_id,
        "batch_number": batch.batch_number,
        "status": batch.status,
        "total_orders": batch.total_orders,
        "total_items": batch.total_items,
        "orders": [{"so_number": o.so_number, "tote_number": o.tote_number} for o in orders],
        "tasks": tasks,
        "kind": kind,
        "to_id": to_row.to_id if to_row else None,
        "to_number": to_row.to_number if to_row else None,
    }


def get_next_task(db, batch_id):
    # v1.8.0 (#295): LEFT JOIN sales_orders + transfer_orders so the
    # row resolves regardless of the pick_tasks discriminator. so_number
    # is NULL for TO tasks; to_number is NULL for SO tasks.
    row = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.pick_sequence, pt.quantity_to_pick, pt.quantity_picked,
                   pt.tote_number, pt.status,
                   b.bin_code, b.bin_barcode, b.aisle, b.row_num, b.level_num,
                   i.sku, i.item_name, i.upc,
                   so.so_number,
                   tro.to_number,
                   z.zone_name
            FROM pick_tasks pt
            JOIN bins b ON b.bin_id = pt.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            JOIN items i ON i.item_id = pt.item_id
            LEFT JOIN sales_orders so ON so.so_id = pt.so_id
            LEFT JOIN transfer_orders tro ON tro.to_id = pt.to_id
            WHERE pt.batch_id = :bid AND pt.status = :task_pending
            ORDER BY pt.pick_sequence ASC
            LIMIT 1
            """
        ),
        {"bid": batch_id, "task_pending": TASK_PENDING},
    ).fetchone()

    if not row:
        return None

    result = _task_row_to_dict(row)

    # Add contributing_orders for wave picks
    result["contributing_orders"] = _get_contributing_orders(db, row.pick_task_id)

    # Add pick_number / total_picks
    pick_number = db.execute(
        text(
            """
            SELECT COUNT(*) FROM pick_tasks
            WHERE batch_id = :bid AND status != :task_pending
            """
        ),
        {"bid": batch_id, "task_pending": TASK_PENDING},
    ).scalar()
    total_picks = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).scalar()
    result["pick_number"] = pick_number + 1
    result["total_picks"] = total_picks

    return result


def confirm_pick(db, pick_task_id, scanned_barcode, quantity_picked, username):
    # 1. Load pick task
    task = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.batch_id, pt.so_id, pt.so_line_id,
                   pt.to_id, pt.to_line_id,
                   pt.item_id, pt.bin_id, pt.quantity_to_pick, pt.status,
                   pt.tote_number
            FROM pick_tasks pt
            WHERE pt.pick_task_id = :tid
            """
        ),
        {"tid": pick_task_id},
    ).fetchone()

    if not task:
        raise ValueError("Pick task not found")
    if task.status != TASK_PENDING:
        raise ValueError(f"Pick task is already {task.status}")

    # Cap quantity to task requirement to prevent over-pick
    if quantity_picked > task.quantity_to_pick:
        raise ValueError(
            f"Cannot pick {quantity_picked} - task only requires {task.quantity_to_pick}"
        )

    # 2. Validate barcode
    item = db.execute(
        text("SELECT item_id, sku, upc, barcode_aliases FROM items WHERE item_id = :iid"),
        {"iid": task.item_id},
    ).fetchone()

    if not _barcode_matches(scanned_barcode, item.upc, item.barcode_aliases):
        raise BarcodeError(f"Wrong item scanned. Expected SKU: {item.sku}")

    # 3. Update pick task
    db.execute(
        text(
            """
            UPDATE pick_tasks
            SET status = :task_status, quantity_picked = :qty, picked_by = :user,
                picked_at = NOW(), scan_confirmed = TRUE
            WHERE pick_task_id = :tid
            """
        ),
        {"qty": quantity_picked, "user": username, "tid": pick_task_id, "task_status": TASK_PICKED},
    )

    # 4. Branch on the pick_tasks discriminator (mig 049 #281). SO
    # picks update sales_order_lines as before; TO picks update
    # transfer_order_lines via the picked-state-machine helper. The
    # XOR CHECK on pick_tasks guarantees exactly one of so_id / to_id
    # is non-NULL so the branches are mutually exclusive.
    if task.to_id is not None:
        # v1.8.0 (#292): TO line picked-qty + status update via the
        # WHERE-clause guard helper. Raises OverPickAttempt when a
        # concurrent picker has already filled the line; the route
        # surfaces 409.
        from services.transfer_order_service import (
            maybe_promote_header_to_partially_picked,
            update_transfer_order_line_picked,
        )
        update_transfer_order_line_picked(
            db, task.to_line_id, quantity_picked,
        )
        maybe_promote_header_to_partially_picked(db, task.to_id)
    else:
        breakdown = db.execute(
            text("SELECT id, so_id, so_line_id, quantity FROM wave_pick_breakdown WHERE pick_task_id = :tid ORDER BY so_id"),
            {"tid": pick_task_id},
        ).fetchall()

        if breakdown:
            # Wave pick - update each contributing SO line
            for bd in breakdown:
                db.execute(
                    text(
                        "UPDATE wave_pick_breakdown SET quantity_picked = quantity WHERE id = :bid"
                    ),
                    {"bid": bd.id},
                )
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": bd.quantity, "sol_id": bd.so_line_id},
                )
        else:
            # Standard pick - single SO line
            db.execute(
                text(
                    "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
                ),
                {"qty": quantity_picked, "sol_id": task.so_line_id},
            )

    # 5. Update inventory (floor at zero for safety).
    #
    # SO picks decrement source on_hand + allocated immediately: the
    # picked stock is committed to the SO and not returnable through
    # the SO flow.
    #
    # TO picks v1.8.0 (#293): inventory does NOT change at pick time.
    # The TO reservation (quantity_allocated, set at import) persists
    # through pick + submit; inventory moves source -> destination
    # only when an admin approves the picker's submission. A
    # rejection therefore leaves source stock intact for re-pick;
    # short-close on the line is the operator-side closeout.
    if task.to_id is None:
        db.execute(
            text(
                """
                UPDATE inventory
                SET quantity_on_hand = GREATEST(0, quantity_on_hand - :picked),
                    quantity_allocated = GREATEST(0, quantity_allocated - :allocated),
                    updated_at = NOW()
                WHERE item_id = :iid AND bin_id = :bid
                """
            ),
            {"picked": quantity_picked, "allocated": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
        )

    # 6. Get remaining count
    remaining = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid AND status = :task_status"),
        {"bid": task.batch_id, "task_status": TASK_PENDING},
    ).scalar()

    # 7. Audit log
    batch = db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": task.batch_id},
    ).fetchone()

    if task.to_id is not None:
        # v1.8.0 (#292): TO picks log against TO_LINE entity so
        # investigators trace back through the TO lifecycle audit
        # chain rather than mixing with SO picks.
        from constants import ACTION_TO_LINE_PICKED
        write_audit_log(
            db,
            action_type=ACTION_TO_LINE_PICKED,
            entity_type="TO_LINE",
            entity_id=task.to_line_id,
            user_id=username,
            warehouse_id=batch.warehouse_id,
            details={
                "sku": item.sku,
                "quantity_to_pick": task.quantity_to_pick,
                "quantity_picked": quantity_picked,
                "pick_task_id": pick_task_id,
                "to_id": task.to_id,
                "item_id": task.item_id,
                "bin_id": task.bin_id,
                "batch_id": task.batch_id,
            },
        )
    else:
        write_audit_log(
            db,
            action_type=ACTION_PICK,
            entity_type="SO",
            entity_id=task.so_id,
            user_id=username,
            warehouse_id=batch.warehouse_id,
            details={
                "sku": item.sku,
                "quantity_to_pick": task.quantity_to_pick,
                "quantity_picked": quantity_picked,
                "pick_task_id": pick_task_id,
                "item_id": task.item_id,
                "bin_id": task.bin_id,
                "batch_id": task.batch_id,
            },
        )

    db.commit()

    # 8. Get bin info for response
    bin_row = db.execute(
        text("SELECT bin_code FROM bins WHERE bin_id = :bid"),
        {"bid": task.bin_id},
    ).fetchone()

    return {
        "task": {
            "pick_task_id": pick_task_id,
            "status": TASK_PICKED,
            "sku": item.sku,
            "quantity_picked": quantity_picked,
            "bin_code": bin_row.bin_code,
            "tote_number": task.tote_number,
        },
        "remaining_tasks": remaining,
    }


def short_pick(db, pick_task_id, quantity_available, username):
    # 1. Load pick task
    task = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.batch_id, pt.so_id, pt.so_line_id, pt.item_id,
                   pt.bin_id, pt.quantity_to_pick, pt.status, pt.tote_number
            FROM pick_tasks pt
            WHERE pt.pick_task_id = :tid
            """
        ),
        {"tid": pick_task_id},
    ).fetchone()

    if not task:
        raise ValueError("Pick task not found")
    if task.status != TASK_PENDING:
        raise ValueError(f"Pick task is already {task.status}")

    if quantity_available > task.quantity_to_pick:
        raise ValueError(
            f"Available quantity {quantity_available} exceeds task requirement "
            f"{task.quantity_to_pick} - use confirm instead"
        )

    shortage = task.quantity_to_pick - quantity_available

    # 2. Update pick task
    db.execute(
        text(
            """
            UPDATE pick_tasks
            SET status = :task_status, quantity_picked = :qty, picked_by = :user, picked_at = NOW()
            WHERE pick_task_id = :tid
            """
        ),
        {"qty": quantity_available, "user": username, "tid": pick_task_id, "task_status": TASK_SHORT},
    )

    # 3. Update inventory (floor at zero for safety)
    db.execute(
        text(
            """
            UPDATE inventory
            SET quantity_on_hand = GREATEST(0, quantity_on_hand - :picked),
                quantity_allocated = GREATEST(0, quantity_allocated - :allocated),
                updated_at = NOW()
            WHERE item_id = :iid AND bin_id = :bid
            """
        ),
        {"picked": quantity_available, "allocated": task.quantity_to_pick, "iid": task.item_id, "bid": task.bin_id},
    )

    # 4. Update sales_order_lines.quantity_picked (wave or standard)
    breakdown = db.execute(
        text("SELECT id, so_id, so_line_id, quantity FROM wave_pick_breakdown WHERE pick_task_id = :tid ORDER BY so_id"),
        {"tid": pick_task_id},
    ).fetchall()

    if breakdown:
        # Wave pick - distribute available quantity FIFO by SO ID
        remaining_to_give = quantity_available
        for bd in breakdown:
            give = min(remaining_to_give, bd.quantity)
            short_qty = bd.quantity - give
            db.execute(
                text(
                    "UPDATE wave_pick_breakdown SET quantity_picked = :picked, short_quantity = :short WHERE id = :bid"
                ),
                {"picked": give, "short": short_qty, "bid": bd.id},
            )
            if give > 0:
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": give, "sol_id": bd.so_line_id},
                )
            remaining_to_give -= give
    else:
        # Standard pick - single SO line
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_picked = quantity_picked + :qty WHERE so_line_id = :sol_id"
            ),
            {"qty": quantity_available, "sol_id": task.so_line_id},
        )

    # 5. Audit log
    item = db.execute(
        text("SELECT sku FROM items WHERE item_id = :iid"),
        {"iid": task.item_id},
    ).fetchone()

    batch = db.execute(
        text("SELECT warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": task.batch_id},
    ).fetchone()

    write_audit_log(
        db,
        action_type=ACTION_PICK,
        entity_type="SO",
        entity_id=task.so_id,
        user_id=username,
        warehouse_id=batch.warehouse_id,
        details={
            "pick_task_id": pick_task_id,
            "item_id": task.item_id,
            "sku": item.sku,
            "quantity_to_pick": task.quantity_to_pick,
            "quantity_picked": quantity_available,
            "shortage": shortage,
            "bin_id": task.bin_id,
            "batch_id": task.batch_id,
            "type": "SHORT_PICK",
        },
    )

    db.commit()

    bin_row = db.execute(
        text("SELECT bin_code FROM bins WHERE bin_id = :bid"),
        {"bid": task.bin_id},
    ).fetchone()

    return {
        "task": {
            "pick_task_id": pick_task_id,
            "status": TASK_SHORT,
            "sku": item.sku,
            "quantity_to_pick": task.quantity_to_pick,
            "quantity_picked": quantity_available,
            "shortage": shortage,
            "bin_code": bin_row.bin_code,
        },
    }


def complete_batch(db, batch_id, username):
    # 1. Load batch
    batch = db.execute(
        text("SELECT batch_id, batch_number, status, warehouse_id FROM pick_batches WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).fetchone()

    if not batch:
        raise ValueError("Batch not found")

    # Check all tasks are in terminal state
    pending_count = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid AND status = :task_status"),
        {"bid": batch_id, "task_status": TASK_PENDING},
    ).scalar()

    if pending_count > 0:
        raise ValueError(f"Cannot complete batch - {pending_count} tasks still pending")

    # Layer 2A: line-level fulfillment guard. The pending-task count above
    # only checks that pick_tasks reached a terminal state; it does NOT
    # check that every SO line was actually covered. Under-allocation at
    # batch creation (now blocked by InsufficientCoverageError pre-flight,
    # but historically silent) leaves lines with no pick_task at all -
    # zero PENDING tasks for them, zero PICKED tasks, and quantity_picked
    # never moves off 0. Without this guard, those lines silently advance
    # through PICKED -> PACKED -> SHIPPED.
    #
    # A line is considered legitimately closed when EITHER
    #   - quantity_picked >= quantity_ordered (fully picked), OR
    #   - an explicit operator-confirmed shortfall marker exists:
    #       * pick_tasks.status = SHORT on a task for this so_line_id, OR
    #       * wave_pick_breakdown.short_quantity > 0 for this so_line_id
    #         (wave picks distribute one pick_task across many SO lines).
    silently_short = db.execute(
        text(
            """
            SELECT sol.so_id, sol.so_line_id, i.sku,
                   sol.quantity_ordered, sol.quantity_picked
              FROM sales_order_lines sol
              JOIN pick_batch_orders pbo ON pbo.so_id = sol.so_id
              JOIN items i ON i.item_id = sol.item_id
             WHERE pbo.batch_id = :bid
               AND sol.quantity_picked < sol.quantity_ordered
               AND NOT EXISTS (
                   SELECT 1 FROM pick_tasks pt
                    WHERE pt.so_line_id = sol.so_line_id
                      AND pt.status = :short
               )
               AND NOT EXISTS (
                   SELECT 1 FROM wave_pick_breakdown wb
                    WHERE wb.so_line_id = sol.so_line_id
                      AND wb.short_quantity > 0
               )
             ORDER BY sol.so_id, sol.so_line_id
            """
        ),
        {"bid": batch_id, "short": TASK_SHORT},
    ).fetchall()

    if silently_short:
        offenders = [
            f"so_id={r.so_id} sku={r.sku} ordered={r.quantity_ordered} picked={r.quantity_picked}"
            for r in silently_short[:5]
        ]
        more = "" if len(silently_short) <= 5 else f" (+{len(silently_short) - 5} more)"
        raise ValueError(
            "Cannot complete batch - lines under-picked with no short-close marker: "
            + "; ".join(offenders) + more
        )

    # 2. Update batch
    db.execute(
        text(
            "UPDATE pick_batches SET status = :batch_status, completed_at = NOW() WHERE batch_id = :bid"
        ),
        {"bid": batch_id, "batch_status": BATCH_COMPLETED},
    )

    # 3. Flip each SO to PICKED.
    # v1.5.0 #119: FOR UPDATE OF so locks each sales_orders row for the
    # rest of this transaction so two concurrent complete_batch calls
    # that share an SO serialise on the SO aggregate. The lock scope
    # is sales_orders only; pick_batch_orders rows are not locked.
    so_rows = db.execute(
        text(
            """
            SELECT pbo.so_id, so.so_number, so.external_id, so.warehouse_id
            FROM pick_batch_orders pbo
            JOIN sales_orders so ON so.so_id = pbo.so_id
            WHERE pbo.batch_id = :bid
            FOR UPDATE OF so
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    # v1.5.0 #116: one pick.confirmed emit per SO that flips to PICKED.
    # All events share g.source_txn_id (the same request), but each has
    # a distinct aggregate_id (so_id) so the integration_events
    # idempotency constraint treats them as separate rows. Outside a
    # Flask request context (unit tests that call complete_batch
    # directly) emission is skipped; the HTTP-driven path is the only
    # supported v1.5.0 emit trigger.
    in_request = has_request_context()
    source_txn_id = g.source_txn_id if in_request else None
    user_ext_id = get_user_external_id(db, username) if in_request else None
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for so in so_rows:
        db.execute(
            text("UPDATE sales_orders SET status = :status, picked_at = NOW() WHERE so_id = :so_id"),
            {"so_id": so.so_id, "status": SO_PICKED},
        )

        if not in_request:
            continue

        # Fetch the SO's lines with item external_ids for the envelope.
        line_rows = db.execute(
            text(
                """
                SELECT i.external_id AS item_external_id, sol.quantity_picked
                  FROM sales_order_lines sol
                  JOIN items i ON i.item_id = sol.item_id
                 WHERE sol.so_id = :sid
                 ORDER BY sol.line_number
                """
            ),
            {"sid": so.so_id},
        ).fetchall()
        emit_event(
            db,
            event_type="pick.confirmed",
            event_version=1,
            aggregate_type="sales_order",
            aggregate_id=so.so_id,
            aggregate_external_id=so.external_id,
            warehouse_id=so.warehouse_id,
            source_txn_id=source_txn_id,
            payload={
                "sales_order_external_id": str(so.external_id),
                "lines": [
                    {
                        "item_external_id": str(line.item_external_id),
                        "quantity_picked": line.quantity_picked,
                        # sales_order_lines has no lot/serial columns in
                        # v1.5.0; the wire contract keeps the fields
                        # nullable for a future schema bump.
                        "lot_number": None,
                        "serial_number": None,
                    }
                    for line in line_rows
                ],
                "completed_by_user_external_id": user_ext_id,
                "completed_at": completed_at,
            },
        )

    # 4. Audit log
    write_audit_log(
        db,
        action_type=ACTION_PICK,
        entity_type="BATCH",
        entity_id=batch_id,
        user_id=username,
        warehouse_id=batch.warehouse_id,
        details={"batch_number": batch.batch_number, "so_count": len(so_rows)},
    )

    # 5. Summary
    task_stats = db.execute(
        text(
            """
            SELECT COALESCE(SUM(quantity_picked), 0) AS total_picked,
                   COUNT(*) FILTER (WHERE status = :task_short) AS total_shorts
            FROM pick_tasks WHERE batch_id = :bid
            """
        ),
        {"bid": batch_id, "task_short": TASK_SHORT},
    ).fetchone()

    db.commit()

    return {
        "batch_id": batch_id,
        "batch_number": batch.batch_number,
        "summary": {
            "total_orders": len(so_rows),
            "total_items_picked": task_stats.total_picked,
            "total_shorts": task_stats.total_shorts,
            "orders": [{"so_number": so.so_number, "status": SO_PICKED} for so in so_rows],
        },
    }


# --- Wave Picking ---


def wave_validate(db, barcode, warehouse_id, username=None):
    """Validate a scanned barcode for the pick screen. Lightweight check,
    no allocation. Recognizes both sales-order numbers/barcodes and
    transfer-order numbers; the response carries a `kind` discriminator
    ('SO' or 'TO') so the caller can route appropriately.

    `username` is required to resolve the TO assignment check; SO paths
    ignore it. Returns `{valid, ...}` on success and
    `{valid: False, error, status_code}` on failure (the route honors
    `status_code` for the HTTP response).
    """
    so = db.execute(
        text(
            """
            SELECT so_id, so_number, status, warehouse_id
            FROM sales_orders
            WHERE (so_number = :barcode OR so_barcode = :barcode)
              AND warehouse_id = :wh
            LIMIT 1
            """
        ),
        {"barcode": barcode, "wh": warehouse_id},
    ).fetchone()

    if so:
        return _validate_so(db, so)

    to_result = _validate_to(db, barcode, warehouse_id, username)
    if to_result is not None:
        return to_result

    # --- FUTURE: ERP Connector Hook ---
    # If a connector is configured, attempt to pull the SO:
    #   result = enrich_order(barcode, warehouse_id)
    #   if result: re-query DB and continue
    # For now, return "Order not found" immediately.
    # --- END FUTURE HOOK ---
    return {"valid": False, "error": "Order not found", "status_code": 404}


def _validate_so(db, so):
    if so.status != SO_OPEN:
        active_batch = db.execute(
            text(
                """
                SELECT pb.batch_id
                FROM pick_batch_orders pbo
                JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
                WHERE pbo.so_id = :so_id AND pb.status IN (:batch_open, :batch_in_progress)
                LIMIT 1
                """
            ),
            {"so_id": so.so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
        ).fetchone()
        if active_batch:
            return {
                "valid": False,
                "error": "Order already in active pick batch",
                "batch_id": active_batch.batch_id,
                "status_code": 409,
            }
        return {
            "valid": False,
            "error": f"Order status is {so.status}, must be OPEN",
            "status_code": 400,
        }

    active_batch = db.execute(
        text(
            """
            SELECT pb.batch_id
            FROM pick_batch_orders pbo
            JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
            WHERE pbo.so_id = :so_id AND pb.status IN (:batch_open, :batch_in_progress)
            LIMIT 1
            """
        ),
        {"so_id": so.so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
    ).fetchone()
    if active_batch:
        return {
            "valid": False,
            "error": "Order already in active pick batch",
            "batch_id": active_batch.batch_id,
            "status_code": 409,
        }

    line_stats = db.execute(
        text(
            """
            SELECT COUNT(*) AS line_count, COALESCE(SUM(quantity_ordered), 0) AS total_units
            FROM sales_order_lines WHERE so_id = :so_id
            """
        ),
        {"so_id": so.so_id},
    ).fetchone()

    if line_stats.line_count == 0:
        return {"valid": False, "error": "Order has no items", "status_code": 400}

    return {
        "valid": True,
        "kind": "SO",
        "so_id": so.so_id,
        "so_number": so.so_number,
        "line_count": line_stats.line_count,
        "total_units": line_stats.total_units,
    }


def _validate_to(db, barcode, warehouse_id, username):
    """Resolve a scanned barcode against transfer_orders. Returns None
    when the barcode is not a known TO at this warehouse so the caller
    can fall through to "not found". Returns a result dict otherwise.

    A TO is scannable from the pick screen only when:
      - status is OPEN or PARTIALLY_PICKED (other statuses are explicit)
      - a pick_batch has been provisioned via start-picking
      - the batch is assigned to the caller (username), so two pickers
        cannot stomp on the same batch
    """
    to = db.execute(
        text(
            """
            SELECT to_id, to_number, status, source_warehouse_id
              FROM transfer_orders
             WHERE to_number = :barcode
               AND source_warehouse_id = :wh
             LIMIT 1
            """
        ),
        {"barcode": barcode, "wh": warehouse_id},
    ).fetchone()
    if not to:
        return None

    if to.status not in ("OPEN", "PARTIALLY_PICKED"):
        return {
            "valid": False,
            "error": f"Transfer order status is {to.status}",
            "to_number": to.to_number,
            "to_status": to.status,
            "status_code": 409,
        }

    batch = db.execute(
        text(
            """
            SELECT DISTINCT pb.batch_id, pb.batch_number, pb.assigned_to,
                            pb.status AS batch_status
              FROM pick_tasks pt
              JOIN pick_batches pb ON pb.batch_id = pt.batch_id
             WHERE pt.to_id = :tid
               AND pb.status IN (:b_open, :b_inprog)
             ORDER BY pb.batch_id DESC
             LIMIT 1
            """
        ),
        {"tid": to.to_id, "b_open": BATCH_OPEN, "b_inprog": BATCH_IN_PROGRESS},
    ).fetchone()
    if not batch:
        return {
            "valid": False,
            "error": "Transfer order not started -- ask admin to start picking",
            "to_number": to.to_number,
            "status_code": 409,
        }

    if username is not None and batch.assigned_to != username:
        return {
            "valid": False,
            "error": f"Transfer order is assigned to {batch.assigned_to}",
            "to_number": to.to_number,
            "batch_id": batch.batch_id,
            "assigned_to": batch.assigned_to,
            "status_code": 409,
        }

    line_stats = db.execute(
        text(
            """
            SELECT COUNT(*) AS line_count,
                   COALESCE(SUM(committed_qty), 0) AS total_units
              FROM transfer_order_lines
             WHERE to_id = :tid
               AND status IN ('PENDING', 'PARTIALLY_PICKED')
            """
        ),
        {"tid": to.to_id},
    ).fetchone()

    return {
        "valid": True,
        "kind": "TO",
        "to_id": to.to_id,
        "to_number": to.to_number,
        "batch_id": batch.batch_id,
        "batch_number": batch.batch_number,
        "line_count": line_stats.line_count,
        "total_units": line_stats.total_units,
    }


def wave_create(db, so_ids, warehouse_id, username, exclude_so_ids=None):
    """Create a wave pick batch from multiple SOs with combined item picks.

    `exclude_so_ids` is populated by the picker UI on the second call after
    a 409 InsufficientCoverageError - SOs the picker chose to drop are
    filtered out before validation + coverage checks. SOs in the input list
    that appear in exclude_so_ids are silently skipped (no error).
    """
    if len(so_ids) != len(set(so_ids)):
        raise ValueError("Duplicate SO IDs in request")

    exclude_set = set(exclude_so_ids or [])
    so_ids = [s for s in so_ids if s not in exclude_set]
    if not so_ids:
        raise ValueError("No sales orders to pick (all excluded or empty input)")

    # No-Resume rule: scrub any in-progress batches this user
    # abandoned before starting fresh. Same rationale as
    # create_pick_batch -- wave picks share the picker session, so
    # the same auto-cancel discipline applies.
    cancel_prior_user_batches(db, username=username, warehouse_id=warehouse_id)

    # 1. Validate all SOs
    sales_orders = []
    for so_id in so_ids:
        so = db.execute(
            text(
                "SELECT so_id, so_number, status, warehouse_id FROM sales_orders WHERE so_id = :so_id"
            ),
            {"so_id": so_id},
        ).fetchone()

        if not so:
            raise ValueError(f"SO not found: {so_id}")
        if so.warehouse_id != warehouse_id:
            raise ValueError(f"SO {so.so_number} is in a different warehouse")
        if so.status != SO_OPEN:
            raise ValueError(f"SO {so.so_number} status is {so.status}, must be OPEN")

        # Check not already in active batch
        active = db.execute(
            text(
                """
                SELECT pb.batch_id FROM pick_batch_orders pbo
                JOIN pick_batches pb ON pb.batch_id = pbo.batch_id
                WHERE pbo.so_id = :so_id AND pb.status IN (:batch_open, :batch_in_progress)
                LIMIT 1
                """
            ),
            {"so_id": so_id, "batch_open": BATCH_OPEN, "batch_in_progress": BATCH_IN_PROGRESS},
        ).fetchone()
        if active:
            raise AlreadyInBatchError(so.so_number, active.batch_id)

        # Check has lines
        line_count = db.execute(
            text("SELECT COUNT(*) FROM sales_order_lines WHERE so_id = :so_id"),
            {"so_id": so_id},
        ).scalar()
        if line_count == 0:
            raise ValueError(f"SO {so.so_number} has no items")

        sales_orders.append(so)

    # 1.5 Coverage pre-flight. Build line_map BEFORE creating the batch row
    # so an InsufficientCoverageError leaves no half-written batch behind.
    # See create_pick_batch for the same pattern + rationale.
    line_map = {}
    for so in sales_orders:
        lines = db.execute(
            text(
                """
                SELECT so_line_id, item_id, quantity_ordered, quantity_allocated
                FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_ordered > quantity_allocated
                """
            ),
            {"so_id": so.so_id},
        ).fetchall()
        for line in lines:
            needed = line.quantity_ordered - line.quantity_allocated
            if needed <= 0:
                continue
            line_map.setdefault(line.item_id, []).append({
                "so_id": so.so_id,
                "so_number": so.so_number,
                "so_line_id": line.so_line_id,
                "needed": needed,
            })

    unpickable, _ = _plan_coverage(db, line_map, warehouse_id)
    if unpickable:
        db.rollback()
        raise InsufficientCoverageError(list(unpickable.values()))

    # 2. Generate batch
    now = datetime.now(timezone.utc)
    batch_number = f"WAVE-{now.strftime('%Y%m%d-%H%M%S-%f')}"

    result = db.execute(
        text(
            """
            INSERT INTO pick_batches (batch_number, warehouse_id, assigned_to, status)
            VALUES (:bn, :wh, :user, :status)
            RETURNING batch_id
            """
        ),
        {"bn": batch_number, "wh": warehouse_id, "user": username, "status": BATCH_OPEN},
    )
    batch_id = result.fetchone()[0]

    # 3. Create wave_pick_orders and pick_batch_orders
    for idx, so in enumerate(sales_orders, 1):
        tote = f"TOTE-{idx}"
        db.execute(
            text("INSERT INTO pick_batch_orders (batch_id, so_id, tote_number) VALUES (:bid, :sid, :tote)"),
            {"bid": batch_id, "sid": so.so_id, "tote": tote},
        )
        db.execute(
            text("INSERT INTO wave_pick_orders (batch_id, so_id) VALUES (:bid, :sid)"),
            {"bid": batch_id, "sid": so.so_id},
        )

    # 4. Gather all lines across all SOs, group by item_id
    # line_map: item_id -> [ {so_id, so_line_id, needed} ]
    line_map = {}
    for so in sales_orders:
        lines = db.execute(
            text(
                """
                SELECT so_line_id, item_id, quantity_ordered, quantity_allocated
                FROM sales_order_lines
                WHERE so_id = :so_id AND quantity_ordered > quantity_allocated
                """
            ),
            {"so_id": so.so_id},
        ).fetchall()
        for line in lines:
            needed = line.quantity_ordered - line.quantity_allocated
            if needed <= 0:
                continue
            if line.item_id not in line_map:
                line_map[line.item_id] = []
            line_map[line.item_id].append({
                "so_id": so.so_id,
                "so_line_id": line.so_line_id,
                "needed": needed,
            })

    # 5. For each item, allocate inventory and create combined pick tasks
    total_units = 0
    warnings = []

    for item_id, contributions in line_map.items():
        combined_needed = sum(c["needed"] for c in contributions)

        # V-030: lock every candidate inventory row before reading
        # quantity_allocated so two concurrent wave-creates cannot
        # both allocate the same stock.
        inv_rows = db.execute(
            text(
                """
                SELECT inv.inventory_id, inv.bin_id, inv.quantity_on_hand, inv.quantity_allocated,
                       (inv.quantity_on_hand - inv.quantity_allocated) AS available,
                       b.pick_sequence, b.bin_type
                FROM inventory inv
                JOIN bins b ON b.bin_id = inv.bin_id
                WHERE inv.item_id = :item_id
                  AND inv.warehouse_id = :wh
                  AND (inv.quantity_on_hand - inv.quantity_allocated) > 0
                  AND b.bin_type IN (:bin_pickable, :bin_pickable_staging)
                ORDER BY
                  b.pick_sequence ASC,
                  inv.updated_at ASC
                FOR UPDATE OF inv
                """
            ),
            {"item_id": item_id, "wh": warehouse_id, "bin_pickable": BIN_PICKABLE, "bin_pickable_staging": BIN_PICKABLE_STAGING},
        ).fetchall()

        total_available = sum(r.available for r in inv_rows)
        if total_available < combined_needed:
            item_info = db.execute(
                text("SELECT sku FROM items WHERE item_id = :iid"),
                {"iid": item_id},
            ).fetchone()
            warnings.append({
                "sku": item_info.sku,
                "needed": combined_needed,
                "available": total_available,
            })

        # Allocate from bins in order, creating one pick task per bin
        remaining = combined_needed
        for inv in inv_rows:
            if remaining <= 0:
                break

            take = min(remaining, inv.available)

            # Allocate inventory
            db.execute(
                text("UPDATE inventory SET quantity_allocated = quantity_allocated + :qty WHERE inventory_id = :inv_id"),
                {"qty": take, "inv_id": inv.inventory_id},
            )

            # Create combined pick task - use first contributing SO as reference
            first_contrib = contributions[0]
            task_result = db.execute(
                text(
                    """
                    INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, bin_id,
                                            quantity_to_pick, pick_sequence, tote_number, status)
                    VALUES (:bid, :so_id, :so_line_id, :item_id, :bin_id,
                            :qty, :pick_seq, 'WAVE', :task_status)
                    RETURNING pick_task_id
                    """
                ),
                {
                    "bid": batch_id,
                    "so_id": first_contrib["so_id"],
                    "so_line_id": first_contrib["so_line_id"],
                    "item_id": item_id,
                    "bin_id": inv.bin_id,
                    "qty": take,
                    "pick_seq": inv.pick_sequence,
                    "task_status": TASK_PENDING,
                },
            )
            pick_task_id = task_result.fetchone()[0]

            # Create wave_pick_breakdown records - distribute this pick across SOs
            pick_remaining = take
            for contrib in contributions:
                if pick_remaining <= 0:
                    break
                alloc = min(pick_remaining, contrib["needed"])
                if alloc <= 0:
                    continue

                db.execute(
                    text(
                        """
                        INSERT INTO wave_pick_breakdown (pick_task_id, so_id, so_line_id, quantity)
                        VALUES (:tid, :so_id, :sol_id, :qty)
                        """
                    ),
                    {"tid": pick_task_id, "so_id": contrib["so_id"], "sol_id": contrib["so_line_id"], "qty": alloc},
                )

                # Update SO line allocation
                db.execute(
                    text(
                        "UPDATE sales_order_lines SET quantity_allocated = quantity_allocated + :qty WHERE so_line_id = :sol_id"
                    ),
                    {"qty": alloc, "sol_id": contrib["so_line_id"]},
                )

                contrib["needed"] -= alloc
                pick_remaining -= alloc

            total_units += take
            remaining -= take

    # SOs stay OPEN while the wave is active; activity is tracked via the
    # batch aggregate (see create_pick_batch for the same rationale).

    # 6. Update batch totals
    total_picks = db.execute(
        text("SELECT COUNT(*) FROM pick_tasks WHERE batch_id = :bid"),
        {"bid": batch_id},
    ).scalar()

    db.execute(
        text("UPDATE pick_batches SET total_orders = :orders, total_items = :items WHERE batch_id = :bid"),
        {"orders": len(sales_orders), "items": total_units, "bid": batch_id},
    )

    # 8. Audit log
    write_audit_log(
        db,
        action_type=ACTION_PICK,
        entity_type="BATCH",
        entity_id=batch_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "batch_number": batch_number,
            "type": "WAVE_CREATE",
            "so_count": len(sales_orders),
            "total_picks": total_picks,
            "total_units": total_units,
        },
    )

    # 9. Get first pick for response
    first_pick = get_next_task(db, batch_id)

    db.commit()

    response = {
        "batch_id": batch_id,
        "batch_number": batch_number,
        "total_orders": len(sales_orders),
        "total_picks": total_picks,
        "total_units": total_units,
        "orders": [
            {"so_id": so.so_id, "so_number": so.so_number, "line_count": sum(1 for c in line_map.values() for item in c if item["so_id"] == so.so_id)}
            for so in sales_orders
        ],
    }

    if first_pick:
        response["first_pick"] = first_pick

    if warnings:
        response["warnings"] = warnings

    return response


class AlreadyInBatchError(Exception):
    """Raised when an SO is already in an active pick batch."""
    def __init__(self, so_number, batch_id):
        self.so_number = so_number
        self.batch_id = batch_id
        super().__init__(f"SO {so_number} already in active pick batch {batch_id}")


# --- Helpers ---

def _get_contributing_orders(db, pick_task_id):
    """Get contributing orders for a wave pick task."""
    rows = db.execute(
        text(
            """
            SELECT wpb.so_id, so.so_number, wpb.quantity
            FROM wave_pick_breakdown wpb
            JOIN sales_orders so ON so.so_id = wpb.so_id
            WHERE wpb.pick_task_id = :tid
            ORDER BY wpb.so_id
            """
        ),
        {"tid": pick_task_id},
    ).fetchall()
    return [{"so_number": r.so_number, "quantity": r.quantity} for r in rows]


def full_revert_batch(db, batch_id, username):
    """Cancel a batch and unwind every effect so the SOs in it are
    indistinguishable from "never started picking."

    Operator semantic (hotfix/picking-revert-allocation): cancel = wipe
    all progress, no Resume affordance. Whatever was picked goes back
    on the shelf (operator is responsible for physically returning),
    whatever was reserved is freed, the line counters reset, and the
    batch is marked CANCELLED.

    Per-task effects, branched on terminal pick_task.status:
      PENDING -> the task never fired. Decrement
        inventory.quantity_allocated by quantity_to_pick (release the
        bin reservation booked at create_pick_batch), decrement
        sales_order_lines.quantity_allocated by the same amount, flip
        the task to SKIPPED.
      PICKED -> units physically left the bin. add_inventory restores
        them to the source bin, decrement
        sales_order_lines.quantity_picked by quantity_picked AND
        quantity_allocated by quantity_to_pick, flip the task to
        RELEASED (matches the revert flow's terminal state).
        inventory.quantity_allocated was already cleared at
        confirm_pick time, so no inventory.allocated change here.
      SHORT -> the operator picked quantity_picked and shorted the
        rest. add_inventory restores those quantity_picked units to
        the bin if > 0, decrement sol.quantity_picked /
        quantity_allocated. inventory.quantity_allocated was already
        cleared at short_pick. Task flips to SKIPPED (the original
        SHORT signal is preserved in audit; for cancel we want the
        line back to clean).
      SKIPPED / RELEASED -> already terminal, no-op (idempotent
        re-entry).

    Writes one SO_PICK_RELEASED audit row per task whose
    quantity_picked > 0 (the physical inventory event), plus one
    SO_ALLOCATION_RELEASED row per task whose allocation was undone
    (the line-state event). TO-batch lanes are out of scope: this
    helper only revert SO picks; TO unwind goes through the existing
    transfer_order_service helpers."""
    batch = db.execute(
        text(
            "SELECT batch_id, status, warehouse_id "
            "  FROM pick_batches WHERE batch_id = :bid"
        ),
        {"bid": batch_id},
    ).fetchone()
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")
    if batch.status in (BATCH_COMPLETED, BATCH_CANCELLED):
        # Already terminal -- nothing to unwind. Caller should not
        # rely on this returning a result; the explicit shape lets
        # callers distinguish "noop" from "did work."
        return {"batch_id": batch_id, "released_tasks": 0, "noop": True}

    tasks = db.execute(
        text(
            "SELECT pick_task_id, so_id, so_line_id, item_id, bin_id, "
            "       quantity_to_pick, quantity_picked, status "
            "  FROM pick_tasks "
            " WHERE batch_id = :bid "
            " FOR UPDATE"
        ),
        {"bid": batch_id},
    ).fetchall()

    released_count = 0
    for t in tasks:
        # Skip TO lanes (so_id is NULL on TO tasks): the SO-side unwind
        # below would no-op anyway, but the explicit guard is clearer
        # than relying on the WHERE so_line_id = NULL match producing
        # zero rows.
        if t.so_id is None:
            continue
        qty_to_pick = int(t.quantity_to_pick or 0)
        qty_picked = int(t.quantity_picked or 0)

        if t.status == TASK_PENDING:
            # Reservation only -- nothing physically moved.
            if qty_to_pick > 0:
                db.execute(
                    text(
                        "UPDATE inventory "
                        "   SET quantity_allocated = GREATEST(0, quantity_allocated - :qty), "
                        "       updated_at = NOW() "
                        " WHERE item_id = :iid AND bin_id = :bid"
                    ),
                    {"qty": qty_to_pick, "iid": t.item_id, "bid": t.bin_id},
                )
            new_status = TASK_SKIPPED
        elif t.status in (TASK_PICKED, TASK_SHORT):
            # Physically moved qty_picked units; restore them.
            if qty_picked > 0:
                add_inventory(
                    db,
                    item_id=t.item_id,
                    bin_id=t.bin_id,
                    warehouse_id=batch.warehouse_id,
                    quantity=qty_picked,
                    lot_number=None,
                )
                write_audit_log(
                    db,
                    action_type=ACTION_SO_PICK_RELEASED,
                    entity_type="SO",
                    entity_id=t.so_id,
                    user_id=username,
                    warehouse_id=batch.warehouse_id,
                    details={
                        "pick_task_id": t.pick_task_id,
                        "so_line_id": t.so_line_id,
                        "item_id": t.item_id,
                        "bin_id": t.bin_id,
                        "quantity": qty_picked,
                        "reason": "batch_cancel",
                    },
                )
            new_status = TASK_RELEASED if t.status == TASK_PICKED else TASK_SKIPPED
        else:
            # SKIPPED / RELEASED already terminal; allocation either
            # never booked or already undone via revert flow.
            continue

        # Whether the task was PENDING, PICKED, or SHORT, the create
        # step booked qty_to_pick onto sol.quantity_allocated and the
        # picked step may have bumped sol.quantity_picked. Undo both
        # so the line returns to the pre-batch numbers.
        if t.so_line_id is not None and (qty_to_pick > 0 or qty_picked > 0):
            db.execute(
                text(
                    "UPDATE sales_order_lines "
                    "   SET quantity_picked = GREATEST(0, quantity_picked - :picked_qty), "
                    "       quantity_allocated = GREATEST(0, quantity_allocated - :alloc_qty) "
                    " WHERE so_line_id = :sol_id"
                ),
                {"picked_qty": qty_picked, "alloc_qty": qty_to_pick,
                 "sol_id": t.so_line_id},
            )
            if qty_to_pick > 0:
                write_audit_log(
                    db,
                    action_type=ACTION_SO_ALLOCATION_RELEASED,
                    entity_type="SO",
                    entity_id=t.so_id,
                    user_id=username,
                    warehouse_id=batch.warehouse_id,
                    details={
                        "pick_task_id": t.pick_task_id,
                        "so_line_id": t.so_line_id,
                        "released_quantity": qty_to_pick,
                        "reason": "batch_cancel",
                    },
                )

        db.execute(
            text(
                "UPDATE pick_tasks SET status = :new_status "
                " WHERE pick_task_id = :ptid"
            ),
            {"new_status": new_status, "ptid": t.pick_task_id},
        )
        released_count += 1

    db.execute(
        text(
            "UPDATE pick_batches SET status = :batch_status "
            " WHERE batch_id = :bid"
        ),
        {"bid": batch_id, "batch_status": BATCH_CANCELLED},
    )

    return {"batch_id": batch_id, "released_tasks": released_count, "noop": False}


def _get_tasks_for_batch(db, batch_id):
    # v1.8.0 (#295): LEFT JOIN sales_orders + transfer_orders so TO
    # tasks resolve too; so_number is NULL for TO rows and to_number
    # is NULL for SO rows.
    rows = db.execute(
        text(
            """
            SELECT pt.pick_task_id, pt.pick_sequence, pt.quantity_to_pick, pt.quantity_picked,
                   pt.tote_number, pt.status,
                   b.bin_code, b.bin_barcode, b.aisle, b.row_num, b.level_num,
                   i.sku, i.item_name, i.upc,
                   so.so_number,
                   tro.to_number,
                   z.zone_name
            FROM pick_tasks pt
            JOIN bins b ON b.bin_id = pt.bin_id
            LEFT JOIN zones z ON z.zone_id = b.zone_id
            JOIN items i ON i.item_id = pt.item_id
            LEFT JOIN sales_orders so ON so.so_id = pt.so_id
            LEFT JOIN transfer_orders tro ON tro.to_id = pt.to_id
            WHERE pt.batch_id = :bid
            ORDER BY pt.pick_sequence ASC, b.bin_code ASC
            """
        ),
        {"bid": batch_id},
    ).fetchall()

    return [_task_row_to_dict(r) for r in rows]


def _task_row_to_dict(row):
    return {
        "pick_task_id": row.pick_task_id,
        "pick_sequence": row.pick_sequence,
        "bin_code": row.bin_code,
        "bin_barcode": row.bin_barcode,
        "zone": row.zone_name or None,
        "aisle": row.aisle or None,
        "row_num": row.row_num,
        "level_num": row.level_num,
        "sku": row.sku,
        "item_name": row.item_name,
        "upc": row.upc,
        "quantity_to_pick": row.quantity_to_pick,
        "tote_number": row.tote_number,
        "so_number": row.so_number,
        # v1.8.0 (#295): TO discriminator. NULL when this row is a
        # SO pick (so_number set instead). Mobile renders the
        # appropriate header based on whichever is non-null.
        "to_number": getattr(row, "to_number", None),
        "status": row.status,
    }


def _barcode_matches(scanned, upc, barcode_aliases):
    if scanned == upc:
        return True
    if barcode_aliases and isinstance(barcode_aliases, list):
        return scanned in barcode_aliases
    return False


class BarcodeError(Exception):
    """Raised when a scanned barcode doesn't match the expected item."""
    pass
