"""Sales-order shared service.

v1.9.0 introduces one shared cancel handler. Two callers converge here:

- Admin operator path: POST /api/admin/sales-orders/<id>/cancel.
- Inbound path: an ERP-pushed update on an existing SO whose
  canonical status field has flipped to CANCELLED.

The cancel transition is the only state-changing SO operation that
historically did NOT write audit_log; routing both paths through this
service closes that hole and gives the inventory unwind one source of
truth. Cancellation does not emit an outbox event (Sentry follows the
ERP for cancel; downstream consumers learn through their own ERP
integration).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import g, has_request_context
from sqlalchemy import text

from constants import (
    ACTION_CANCEL,
    ACTION_PICK,
    ACTION_SO_PICK_RELEASED,
    ACTION_SO_STATUS_REVERTED,
    ACTION_SO_UNPACKED,
    ACTION_SO_UNSHIPPED,
    BATCH_COMPLETED,
    SO_CANCELLED,
    SO_OPEN,
    SO_PACKED,
    SO_PICKED,
    SO_SHIPPED,
    TASK_PENDING,
    TASK_PICKED,
    TASK_RELEASED,
)
from services.audit_service import write_audit_log
from services.events_service import emit_event, get_user_external_id
from services.inventory_service import add_inventory


# Allowed source values for the audit_log.details.source field. Both
# admin and inbound flows must pass one of these so an audit reader can
# distinguish operator-initiated cancels from ERP-initiated cancels.
ALLOWED_SOURCES = ("admin", "inbound")


class CancelNotAllowed(Exception):
    """Raised when the SO cannot be cancelled (typically because it is
    already SHIPPED). The caller surfaces this as a 4xx response with
    an error_kind that maps to the current_status."""

    def __init__(self, message: str, current_status: str):
        super().__init__(message)
        self.current_status = current_status


def _get_default_receiving_bin(db) -> int:
    """Read the default_receiving_bin app_setting. Raises RuntimeError
    if the setting is missing; the seed always provisions it. A
    misconfigured deployment surfaces as a 500 by design rather than
    silently dropping inventory restoration."""
    row = db.execute(
        text(
            "SELECT value FROM app_settings WHERE key = 'default_receiving_bin'"
        )
    ).fetchone()
    if not row or not row.value:
        raise RuntimeError(
            "default_receiving_bin app_setting is missing; cannot unwind "
            "PICKED/PACKED cancellation"
        )
    return int(row.value)


def cancel_sales_order(
    db,
    *,
    so_id: int,
    source: str,
    username: str,
) -> Dict[str, Any]:
    """Cancel a sales order. Idempotent on already-cancelled.

    Locks the sales_orders row with FOR UPDATE so a concurrent ship /
    pick cannot transition past us mid-cancel. Per-status unwind:

    - OPEN: if the SO has been allocated to an active pick batch
      (sales_order_lines.quantity_allocated > 0), release the
      inventory.quantity_allocated, delete pending pick_tasks +
      pick_batch_orders; otherwise status flip only. PICKING was
      retired in mig 060, so allocation state replaces it as the
      "is this inside a batch?" signal.
    - PICKED / PACKED: increment inventory.quantity_on_hand at the
      default receiving bin by each line's quantity_picked, reset
      sales_order_lines.quantity_picked / quantity_packed = 0 and
      status = 'PENDING'. Pre-existing PICKED pick_tasks rows stay in
      place as the audit trail of what happened. Operators move items
      physically; the inventory record reflects the ERP-mandated state.
    - SHIPPED: raises CancelNotAllowed; caller returns 4xx. The dockd
      void-ship route is the path for SHIPPED reversal.

    Args:
        so_id: sales_orders.so_id.
        source: "admin" or "inbound" (lands in audit_log.details.source).
        username: actor for audit_log.user_id.

    Returns dict with pre_status, so_number, audit_log_id (None on the
    idempotent already-cancelled path).

    Raises:
        CancelNotAllowed when the SO is SHIPPED or not found.
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"source must be one of {ALLOWED_SOURCES}; got {source!r}"
        )

    so = db.execute(
        text(
            "SELECT so_id, so_number, status, warehouse_id "
            "  FROM sales_orders "
            " WHERE so_id = :sid "
            " FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise CancelNotAllowed(
            "sales order not found", current_status="UNKNOWN"
        )
    if so.status == SO_SHIPPED:
        raise CancelNotAllowed(
            "cannot cancel a SHIPPED order; void the ship via "
            "/api/v1/dockd/orders/<so>/void-ship first",
            current_status=so.status,
        )
    if so.status == SO_CANCELLED:
        # Idempotent no-op. Audit was already written at original cancel.
        return {
            "pre_status": SO_CANCELLED,
            "so_number": so.so_number,
            "audit_log_id": None,
        }

    pre_status = so.status
    warehouse_id = so.warehouse_id

    if pre_status == SO_OPEN:
        # OPEN with no allocation: _unwind_allocated's SELECT returns
        # nothing and the DELETEs are no-ops. OPEN inside an active
        # batch (pre-mig 060 this would have been PICKING): release
        # allocation, drop pending tasks and pick_batch_orders.
        _unwind_allocated(db, so_id)
    elif pre_status in (SO_PICKED, SO_PACKED):
        _unwind_picked_or_packed(db, so_id, warehouse_id)

    db.execute(
        text(
            "UPDATE sales_orders SET status = :status WHERE so_id = :sid"
        ),
        {"status": SO_CANCELLED, "sid": so_id},
    )

    audit_log_id = write_audit_log(
        db,
        action_type=ACTION_CANCEL,
        entity_type="SO",
        entity_id=so_id,
        user_id=username,
        warehouse_id=warehouse_id,
        details={
            "so_number": so.so_number,
            "pre_status": pre_status,
            "source": source,
        },
    )

    return {
        "pre_status": pre_status,
        "so_number": so.so_number,
        "audit_log_id": audit_log_id,
    }


def _unwind_allocated(db, so_id: int) -> None:
    """Pre-pick state unwind: release inventory.quantity_allocated for
    each line's pending pick_tasks, zero out
    sales_order_lines.quantity_allocated, then delete pending
    pick_tasks + pick_batch_orders. Safe to call on an OPEN SO with
    no allocation: the SELECT returns nothing and the DELETEs are
    no-ops."""
    lines = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_allocated "
            "  FROM sales_order_lines "
            " WHERE so_id = :sid AND quantity_allocated > 0"
        ),
        {"sid": so_id},
    ).fetchall()

    for line in lines:
        tasks = db.execute(
            text(
                "SELECT bin_id, quantity_to_pick FROM pick_tasks "
                " WHERE so_line_id = :sol_id AND status = :task_status"
            ),
            {"sol_id": line.so_line_id, "task_status": TASK_PENDING},
        ).fetchall()
        for task in tasks:
            db.execute(
                text(
                    "UPDATE inventory "
                    "   SET quantity_allocated = quantity_allocated - :qty "
                    " WHERE item_id = :iid AND bin_id = :bid"
                ),
                {
                    "qty": task.quantity_to_pick,
                    "iid": line.item_id,
                    "bid": task.bin_id,
                },
            )
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_allocated = 0 "
                " WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id},
        )

    db.execute(
        text("DELETE FROM pick_tasks WHERE so_id = :sid"),
        {"sid": so_id},
    )
    db.execute(
        text("DELETE FROM pick_batch_orders WHERE so_id = :sid"),
        {"sid": so_id},
    )


def _unwind_picked_or_packed(db, so_id: int, warehouse_id: int) -> None:
    """Post-pick state unwind. Items have already left their source
    bins (decremented at pick-confirm time). Restore them to the
    default receiving bin so an operator can physically move them back
    or redirect the inventory however the ERP-mandated cancel
    workflow requires.

    PICKED pick_tasks rows stay in place: they are the audit trail of
    what physically happened. Only the SO-line state resets so a future
    re-pick attempt (rare; cancellation is terminal in v1.9) would
    re-allocate cleanly. pick_batch_orders is dropped so the SO does
    not show in batch listings.
    """
    receiving_bin_id = _get_default_receiving_bin(db)

    lines = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_picked "
            "  FROM sales_order_lines "
            " WHERE so_id = :sid AND quantity_picked > 0"
        ),
        {"sid": so_id},
    ).fetchall()

    for line in lines:
        # add_inventory handles both new-row and existing-row cases via
        # the V-030 advisory-lock + SELECT-then-INSERT-or-UPDATE pattern.
        # lot_number stays NULL; per-lot tracking is not part of the
        # cancel-restore semantic.
        add_inventory(
            db,
            item_id=line.item_id,
            bin_id=receiving_bin_id,
            warehouse_id=warehouse_id,
            quantity=line.quantity_picked,
            lot_number=None,
        )
        db.execute(
            text(
                "UPDATE sales_order_lines "
                "   SET quantity_picked = 0, "
                "       quantity_packed = 0, "
                "       status          = 'PENDING' "
                " WHERE so_line_id = :sol_id"
            ),
            {"sol_id": line.so_line_id},
        )

    db.execute(
        text("DELETE FROM pick_batch_orders WHERE so_id = :sid"),
        {"sid": so_id},
    )


# so-refinement: forward-flow ordering used by the revert-status path.
# PICKING / PACKING / ALLOCATED were retired in v1.13.0 (mig 058); the
# live flow is OPEN -> PICKED -> PACKED -> SHIPPED. Any target status
# with a strictly-lower index than current is a "backward" transition
# and must go through revert_sales_order_status() so the operator
# decides what happens to picked / packed / shipped state.
_STATUS_ORDER = {
    SO_OPEN: 0,
    SO_PICKED: 1,
    SO_PACKED: 2,
    SO_SHIPPED: 3,
}


class RevertNotAllowed(Exception):
    """The revert request is invalid for a structural reason (target
    status not lower than current, partial release would leave the SO
    in an inconsistent state, etc.). Caller maps to a 4xx with the
    `kind` discriminator so the frontend can show the right error."""

    def __init__(self, message: str, kind: str, **context):
        super().__init__(message)
        self.kind = kind
        self.context = context


def revert_sales_order_status(
    db,
    *,
    so_id: int,
    new_status: str,
    release_pick_task_ids: list,
    username: str,
) -> Dict[str, Any]:
    """Demote an SO from PICKED/PACKED/SHIPPED back to an earlier
    status, unwinding the side effects the operator selected.

    Effects, computed from (current, target):
      * unship: SHIPPED to anything. Clears tracking_number, carrier,
        shipped_at on the header. No physical inventory move (the
        goods left the building); operators reconcile externally.
      * unpack: current >= PACKED and target < PACKED. Zeros
        quantity_packed on every line. No inventory move (pack does
        not touch inventory; only labels units as packed).
      * release: each pick_task_id in release_pick_task_ids restores
        its quantity_picked to the bin it came from, decrements the
        line's quantity_picked by the same amount, and marks the
        pick_task as RELEASED so a subsequent revert prompt does not
        re-offer it.

    Guards:
      * Target must be strictly lower than current (forward only by
        the normal status flow).
      * SO must exist and not be CANCELLED.
      * Every release_pick_task_id must belong to this SO and still
        be in TASK_PICKED state.
      * If target < PICKED and any line ends with quantity_picked > 0
        after releases, raises with kind='picked_qty_remaining' so the
        operator can either release more or pick a higher target.

    Audit: one ACTION_SO_STATUS_REVERTED row per request (from/to),
    plus one ACTION_SO_PICK_RELEASED per released pick_task, plus a
    single ACTION_SO_UNPACKED row when unpack runs and a single
    ACTION_SO_UNSHIPPED row when unship runs.
    """
    if new_status not in _STATUS_ORDER:
        raise RevertNotAllowed(
            f"unknown target status: {new_status!r}",
            kind="invalid_status",
        )

    so = db.execute(
        text(
            "SELECT so_id, so_number, status, warehouse_id, "
            "       tracking_number, carrier, shipped_at "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise RevertNotAllowed(
            "sales order not found", kind="not_found",
        )
    if so.status == SO_CANCELLED:
        raise RevertNotAllowed(
            "cannot revert a cancelled order; reopen via the cancel-undo workflow",
            kind="cancelled",
            current_status=so.status,
        )

    cur_idx = _STATUS_ORDER.get(so.status)
    new_idx = _STATUS_ORDER[new_status]
    if cur_idx is None or new_idx > cur_idx:
        raise RevertNotAllowed(
            "target status must not be higher than current status",
            kind="not_backward",
            current_status=so.status,
            target_status=new_status,
        )

    # so-refinement: new_status == current_status is the "release-only"
    # path. The operator wants to release picks without flipping status,
    # which is valid when releasing a subset that does not zero out
    # quantity_picked. need_unship/need_unpack guard against firing on
    # a same-status request so a release-only call on a SHIPPED order
    # does not also clear its shipment fields.
    need_unship = so.status == SO_SHIPPED and new_status != SO_SHIPPED
    need_unpack = cur_idx >= _STATUS_ORDER[SO_PACKED] and new_idx < _STATUS_ORDER[SO_PACKED]
    target_below_picked = new_idx < _STATUS_ORDER[SO_PICKED]

    pick_ids = list({int(x) for x in release_pick_task_ids or []})
    released_details = []

    if pick_ids:
        tasks = db.execute(
            text(
                "SELECT pick_task_id, so_line_id, item_id, bin_id, "
                "       quantity_to_pick, quantity_picked, status "
                "  FROM pick_tasks "
                " WHERE pick_task_id = ANY(:ids) AND so_id = :sid "
                " FOR UPDATE"
            ),
            {"ids": pick_ids, "sid": so_id},
        ).fetchall()
        found_ids = {t.pick_task_id for t in tasks}
        missing = [pid for pid in pick_ids if pid not in found_ids]
        if missing:
            raise RevertNotAllowed(
                f"pick_task(s) not found on this SO: {missing}",
                kind="pick_task_missing",
                missing_ids=missing,
            )
        wrong_state = [t.pick_task_id for t in tasks if t.status != TASK_PICKED]
        if wrong_state:
            raise RevertNotAllowed(
                f"pick_task(s) not in PICKED state: {wrong_state}",
                kind="pick_task_wrong_state",
                wrong_state_ids=wrong_state,
            )
        for t in tasks:
            qty_picked = int(t.quantity_picked or 0)
            # quantity_to_pick is what create_pick_batch / wave_create
            # bumped sol.quantity_allocated by; releasing the task must
            # undo that allocation so a re-scan sees the line as still
            # needing coverage. quantity_picked is what physically moved
            # out of the bin and goes back via add_inventory; for a
            # normal PICKED task the two values are equal, but the split
            # keeps the partial-pick path (qty_picked < qty_to_pick)
            # correct rather than under-restoring the allocation.
            qty_allocated = int(t.quantity_to_pick or 0)
            if qty_picked > 0:
                add_inventory(
                    db,
                    item_id=t.item_id,
                    bin_id=t.bin_id,
                    warehouse_id=so.warehouse_id,
                    quantity=qty_picked,
                    lot_number=None,
                )
            db.execute(
                text(
                    "UPDATE sales_order_lines "
                    "   SET quantity_picked = GREATEST(quantity_picked - :picked_qty, 0), "
                    "       quantity_allocated = GREATEST(quantity_allocated - :alloc_qty, 0) "
                    " WHERE so_line_id = :sol_id"
                ),
                {"picked_qty": qty_picked, "alloc_qty": qty_allocated, "sol_id": t.so_line_id},
            )
            db.execute(
                text(
                    "UPDATE pick_tasks SET status = :released "
                    " WHERE pick_task_id = :ptid"
                ),
                {"released": TASK_RELEASED, "ptid": t.pick_task_id},
            )
            # Audit row only when units actually moved. A qty_picked=0
            # release still flips the task to RELEASED and undoes the
            # allocation, but there is no physical inventory event to
            # narrate so SO_PICK_RELEASED is skipped.
            if qty_picked > 0:
                released_details.append({
                    "pick_task_id": t.pick_task_id,
                    "so_line_id": t.so_line_id,
                    "item_id": t.item_id,
                    "bin_id": t.bin_id,
                    "quantity": qty_picked,
                })

    if target_below_picked:
        # Reject mid-flight: if releases were partial, the SO would
        # have picked qty on lines but a status that claims no picks.
        # Operator must either release more pick_tasks or pick a
        # target that still permits picks (PICKED or higher).
        remaining = db.execute(
            text(
                "SELECT COALESCE(SUM(quantity_picked), 0) AS total "
                "  FROM sales_order_lines WHERE so_id = :sid"
            ),
            {"sid": so_id},
        ).scalar()
        if remaining and int(remaining) > 0:
            raise RevertNotAllowed(
                "cannot demote below PICKED while quantity_picked remains; "
                "release the remaining pick_tasks or pick a higher target status",
                kind="picked_qty_remaining",
                remaining_picked=int(remaining),
                target_status=new_status,
            )

    if need_unpack:
        db.execute(
            text(
                "UPDATE sales_order_lines SET quantity_packed = 0 "
                " WHERE so_id = :sid AND quantity_packed > 0"
            ),
            {"sid": so_id},
        )
        db.execute(
            text("UPDATE sales_orders SET packed_at = NULL WHERE so_id = :sid"),
            {"sid": so_id},
        )
        write_audit_log(
            db,
            action_type=ACTION_SO_UNPACKED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={"from_status": so.status, "to_status": new_status},
        )

    if need_unship:
        db.execute(
            text(
                "UPDATE sales_orders "
                "   SET tracking_number = NULL, carrier = NULL, shipped_at = NULL "
                " WHERE so_id = :sid"
            ),
            {"sid": so_id},
        )
        write_audit_log(
            db,
            action_type=ACTION_SO_UNSHIPPED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "prev_tracking_number": so.tracking_number,
                "prev_carrier": so.carrier,
                "prev_shipped_at": so.shipped_at.isoformat() if so.shipped_at else None,
            },
        )

    for d in released_details:
        write_audit_log(
            db,
            action_type=ACTION_SO_PICK_RELEASED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details=d,
        )

    status_changed = new_status != so.status
    if status_changed:
        db.execute(
            text("UPDATE sales_orders SET status = :status WHERE so_id = :sid"),
            {"status": new_status, "sid": so_id},
        )
        write_audit_log(
            db,
            action_type=ACTION_SO_STATUS_REVERTED,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "from_status": so.status,
                "to_status": new_status,
                "released_pick_tasks": [d["pick_task_id"] for d in released_details],
                "unpacked": bool(need_unpack),
                "unshipped": bool(need_unship),
            },
        )

    return {
        "so_id": so_id,
        "so_number": so.so_number,
        "from_status": so.status,
        "to_status": new_status,
        "released_pick_tasks": released_details,
        "unpacked": bool(need_unpack),
        "unshipped": bool(need_unship),
    }


# ---------------------------------------------------------------------------
# Admin virtual pick
# ---------------------------------------------------------------------------
#
# Operator-driven shortcut: an admin marks a sales order picked through the
# admin UI without the handheld going out on the floor. Used when the floor
# work already happened but the digital pick never landed -- e.g. legacy
# ShipRush bridge, a stuck batch, or an SO that arrived already-fulfilled.
#
# The end state must be indistinguishable from a real pick:
#   * sales_order_lines.quantity_picked carries the picked qty per line
#   * inventory.quantity_on_hand decrements at the chosen bin per pick
#   * audit log carries one ACTION_PICK row per pick (entity_type='SO')
#   * pick.confirmed integration event fires when the SO flips to PICKED
#
# Symmetry with handheld picks comes from synthesising a one-shot
# pick_batch (status COMPLETED at insert) and one pick_task per pick
# entry. The existing _revert_so_status path takes pick_task_ids as input
# and walks the same release SQL regardless of how the task was created,
# so a virtual pick can be undone through the same "Release Picked
# Quantities" UI as a real pick. No second undo path needed.


class AdminPickError(Exception):
    """Operator submitted an admin-pick request that cannot be applied.

    kind distinguishes the four shapes the route layer maps to HTTP
    status codes:

      * 'so_not_open'           422 -- SO must be OPEN to virtual-pick
      * 'line_not_on_so'        422 -- so_line_id does not belong to so_id
      * 'bin_wrong_warehouse'   422 -- bin is in a different warehouse
      * 'over_pick'             422 -- summed pick qty > unpicked remaining
      * 'insufficient_available' 409 -- bin has less available than asked
    """

    def __init__(self, message: str, kind: str, **context):
        super().__init__(message)
        self.kind = kind
        self.context = context


def record_admin_pick(db, *, so_id: int, picks, username: str) -> dict:
    """Apply a batch of admin virtual picks atomically.

    picks: iterable of {so_line_id, bin_id, quantity} entries. Multiple
    entries with the same so_line_id are allowed (split-bin: line N
    drawn from bin A and bin B in one submit) and sum into the line's
    quantity_picked.

    Caller owns the transaction boundary. This function performs no
    commit; the route handler commits after also running
    maybe_promote_so_to_picked. Raises AdminPickError on validation
    failure; the caller does not need to roll back because no writes
    happen ahead of the validation pass.

    Implementation notes:
      * One synthetic pick_batch row per call (status COMPLETED, marked
        ADMIN-PICK-<so_id>-<microsecond timestamp>) so subsequent
        release flows see one batch per admin action.
      * One pick_task row per pick entry (status TASK_PICKED,
        scan_confirmed=true, picked_by=username) so the existing
        release-pick-tasks endpoint can undo individual entries.
      * Inventory side: decrement quantity_on_hand at the bin by the
        picked qty. quantity_allocated is NOT changed because the
        bin was never pre-allocated against this SO -- the available
        check (on_hand - allocated >= qty) is the safety against
        stealing inventory promised to a different SO.
      * Line side: bump quantity_picked. Bump quantity_allocated to
        max(current, quantity_picked) to preserve the picked-floor
        invariant that the handheld path's _normalize_so_reservations
        enforces.
    """
    so = db.execute(
        text(
            "SELECT so_id, so_number, external_id, status, warehouse_id "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        raise AdminPickError(
            f"sales order {so_id} not found",
            kind="so_not_found",
        )
    if so.status != SO_OPEN:
        raise AdminPickError(
            f"sales order must be OPEN to admin-pick (current: {so.status})",
            kind="so_not_open",
            current_status=so.status,
        )

    pick_entries = list(picks)
    if not pick_entries:
        raise AdminPickError("no picks supplied", kind="empty")

    # Group by so_line_id so we can validate the over-pick guard against
    # the line's remaining capacity in a single sum check per line.
    by_line: dict[int, int] = {}
    for p in pick_entries:
        by_line[p["so_line_id"]] = by_line.get(p["so_line_id"], 0) + p["quantity"]

    # Lock + load every involved line. ANY(:ids) keeps it one round trip
    # whether the operator picks 1 or 50 lines.
    line_rows = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_ordered, quantity_picked, "
            "       quantity_allocated "
            "  FROM sales_order_lines "
            " WHERE so_line_id = ANY(:ids) AND so_id = :sid "
            " FOR UPDATE"
        ),
        {"ids": list(by_line.keys()), "sid": so_id},
    ).fetchall()
    line_by_id = {ln.so_line_id: ln for ln in line_rows}
    missing = [lid for lid in by_line if lid not in line_by_id]
    if missing:
        raise AdminPickError(
            f"so_line_id(s) not on SO {so_id}: {missing}",
            kind="line_not_on_so",
            missing_line_ids=missing,
        )
    for lid, total in by_line.items():
        ln = line_by_id[lid]
        remaining = ln.quantity_ordered - ln.quantity_picked
        if total > remaining:
            raise AdminPickError(
                f"line {lid} over-pick: requested {total}, remaining {remaining}",
                kind="over_pick",
                so_line_id=lid,
                requested=total,
                remaining=remaining,
            )

    # Bin-warehouse sanity. Loads every distinct bin in one query.
    bin_ids = sorted({p["bin_id"] for p in pick_entries})
    bin_rows = db.execute(
        text("SELECT bin_id, warehouse_id FROM bins WHERE bin_id = ANY(:ids)"),
        {"ids": bin_ids},
    ).fetchall()
    bin_warehouse = {b.bin_id: b.warehouse_id for b in bin_rows}
    for bid in bin_ids:
        if bid not in bin_warehouse:
            raise AdminPickError(
                f"bin {bid} not found", kind="bin_not_found", bin_id=bid,
            )
        if bin_warehouse[bid] != so.warehouse_id:
            raise AdminPickError(
                f"bin {bid} is in warehouse {bin_warehouse[bid]}, "
                f"SO {so_id} is in warehouse {so.warehouse_id}",
                kind="bin_wrong_warehouse",
                bin_id=bid,
                bin_warehouse=bin_warehouse[bid],
                so_warehouse=so.warehouse_id,
            )

    # Synthesise the wrapper batch. Microsecond timestamp keeps the
    # batch_number UNIQUE clean even when an operator double-submits.
    batch_number = (
        f"ADMIN-PICK-{so_id}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    )
    batch_id = db.execute(
        text(
            "INSERT INTO pick_batches "
            "  (batch_number, warehouse_id, assigned_to, status, "
            "   started_at, completed_at) "
            "VALUES (:bn, :wh, :user, :st, NOW(), NOW()) "
            "RETURNING batch_id"
        ),
        {
            "bn": batch_number,
            "wh": so.warehouse_id,
            "user": username,
            "st": BATCH_COMPLETED,
        },
    ).scalar()
    db.execute(
        text(
            "INSERT INTO pick_batch_orders (batch_id, so_id) "
            "VALUES (:bid, :sid)"
        ),
        {"bid": batch_id, "sid": so_id},
    )

    created_task_ids: list[int] = []
    for seq, pick in enumerate(pick_entries, start=1):
        line = line_by_id[pick["so_line_id"]]

        # Atomic available check + on_hand decrement. The WHERE clause
        # is the safety: if available drops below the requested qty
        # between the validation read and the write, the UPDATE
        # matches zero rows and RETURNING is empty. Treat that as
        # insufficient_available and raise -- the caller's transaction
        # rolls back any prior writes (batch row, prior picks).
        updated = db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_on_hand = quantity_on_hand - :qty, "
                "       updated_at = NOW() "
                " WHERE item_id = :iid AND bin_id = :bid "
                "   AND quantity_on_hand - quantity_allocated >= :qty "
                "RETURNING inventory_id"
            ),
            {
                "qty": pick["quantity"],
                "iid": line.item_id,
                "bid": pick["bin_id"],
            },
        ).fetchone()
        if updated is None:
            raise AdminPickError(
                f"bin {pick['bin_id']} has insufficient available stock "
                f"for line {pick['so_line_id']} (requested {pick['quantity']})",
                kind="insufficient_available",
                so_line_id=pick["so_line_id"],
                bin_id=pick["bin_id"],
                requested=pick["quantity"],
            )

        # Bump line counters. GREATEST keeps quantity_allocated >= the
        # new picked floor without overriding a higher standing
        # reservation (matching the handheld path's invariant).
        db.execute(
            text(
                "UPDATE sales_order_lines "
                "   SET quantity_picked = quantity_picked + :qty, "
                "       quantity_allocated = GREATEST( "
                "           quantity_allocated, quantity_picked + :qty "
                "       ) "
                " WHERE so_line_id = :sol_id"
            ),
            {"qty": pick["quantity"], "sol_id": pick["so_line_id"]},
        )

        # Synthetic pick_task. status=PICKED, scan_confirmed=true,
        # tote_number NULL (admin picks have no tote). pick_sequence
        # 1..N within the batch keeps the (batch_id, pick_sequence)
        # index dense.
        task_id = db.execute(
            text(
                "INSERT INTO pick_tasks "
                "  (batch_id, so_id, so_line_id, item_id, bin_id, "
                "   quantity_to_pick, quantity_picked, pick_sequence, "
                "   status, picked_by, picked_at, scan_confirmed) "
                "VALUES (:bid, :sid, :sol, :iid, :bin, "
                "        :qty, :qty, :seq, "
                "        :st, :user, NOW(), TRUE) "
                "RETURNING pick_task_id"
            ),
            {
                "bid": batch_id,
                "sid": so_id,
                "sol": pick["so_line_id"],
                "iid": line.item_id,
                "bin": pick["bin_id"],
                "qty": pick["quantity"],
                "seq": seq,
                "st": TASK_PICKED,
                "user": username,
            },
        ).scalar()
        created_task_ids.append(task_id)

        # Audit row per pick. details.source='admin_virtual' is the
        # discriminator that lets dashboards and post-incident readers
        # separate operator shortcuts from handheld floor work.
        item_sku = db.execute(
            text("SELECT sku FROM items WHERE item_id = :iid"),
            {"iid": line.item_id},
        ).scalar()
        write_audit_log(
            db,
            action_type=ACTION_PICK,
            entity_type="SO",
            entity_id=so_id,
            user_id=username,
            warehouse_id=so.warehouse_id,
            details={
                "source": "admin_virtual",
                "sku": item_sku,
                "quantity_to_pick": pick["quantity"],
                "quantity_picked": pick["quantity"],
                "pick_task_id": task_id,
                "item_id": line.item_id,
                "bin_id": pick["bin_id"],
                "batch_id": batch_id,
                "so_line_id": pick["so_line_id"],
            },
        )

    return {
        "batch_id": batch_id,
        "batch_number": batch_number,
        "pick_task_ids": created_task_ids,
        "picks_applied": len(created_task_ids),
    }


def maybe_promote_so_to_picked(db, *, so_id: int, username: str) -> bool:
    """If every line on the SO is fully picked, flip status to PICKED.

    Mirrors complete_batch's status flip + pick.confirmed emit, but
    only fires when the SO is the one being completed. Returns True
    when the flip happened, False when at least one line is still
    under-picked.

    Caller owns the transaction boundary; this function performs no
    commit. Skipped emission outside a Flask request context matches
    complete_batch's pattern -- unit tests can call this directly and
    get the status flip without the event side effect.
    """
    short = db.execute(
        text(
            "SELECT 1 FROM sales_order_lines "
            " WHERE so_id = :sid AND quantity_picked < quantity_ordered "
            " LIMIT 1"
        ),
        {"sid": so_id},
    ).fetchone()
    if short is not None:
        return False

    so = db.execute(
        text(
            "SELECT so_id, so_number, external_id, status, warehouse_id "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if so is None:
        return False
    if so.status != SO_OPEN:
        # Some other request already promoted it; nothing to do.
        return False

    db.execute(
        text(
            "UPDATE sales_orders SET status = :st, picked_at = NOW() "
            " WHERE so_id = :sid"
        ),
        {"sid": so_id, "st": SO_PICKED},
    )

    if not has_request_context():
        return True

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
        {"sid": so_id},
    ).fetchall()
    completed_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    emit_event(
        db,
        event_type="pick.confirmed",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=so.so_id,
        aggregate_external_id=so.external_id,
        warehouse_id=so.warehouse_id,
        source_txn_id=g.source_txn_id,
        payload={
            "sales_order_external_id": str(so.external_id),
            "lines": [
                {
                    "item_external_id": str(line.item_external_id),
                    "quantity_picked": line.quantity_picked,
                    "lot_number": None,
                    "serial_number": None,
                }
                for line in line_rows
            ],
            "completed_by_user_external_id": get_user_external_id(db, username),
            "completed_at": completed_at,
        },
    )
    return True
