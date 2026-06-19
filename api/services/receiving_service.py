"""Receiving shared service: admin unreceive path.

Mirrors the structure of sales_order_service.py: route handlers are
thin, this module owns the multi-table reversal so the inventory walk,
audit row, event emission, and PO status recompute share one
transaction. The session-wide cancel path in api/routes/receiving.py
(used by the mobile receive screen) is intentionally NOT consolidated
here -- it predates the outbound event contract and operates on a list
of receipt_ids in one tap, whereas record_unreceive targets a single
receipt for the admin double-scan-undo flow.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import g, has_request_context
from sqlalchemy import text

from constants import (
    ACTION_RECEIVE_CANCEL,
    PO_OPEN,
    PO_PARTIAL,
    PO_RECEIVED,
    POL_PARTIAL,
    POL_PENDING,
    POL_RECEIVED,
)
from services.audit_service import write_audit_log
from services.events_service import (
    emit_event,
    get_user_external_id,
)


class UnreceiveError(Exception):
    """The unreceive request cannot be applied.

    kind discriminates the four shapes the route layer maps to HTTP
    status codes:

      * 'not_found'             404 -- receipt does not exist or scope
                                       filtered it
      * 'not_a_po_receipt'      422 -- receipt is an RMA goods-in
                                       (so_id NOT NULL), not a PO receipt
      * 'insufficient_available' 409 -- warehouse pool lacks the qty
                                        to back out (allocated/picked
                                        elsewhere; operator must adjust
                                        first)
    """

    def __init__(self, message: str, kind: str, **context):
        super().__init__(message)
        self.kind = kind
        self.context = context


def recompute_po_status(db, po_id: int) -> str:
    """Re-derive every line's status and the PO header status from the
    lines' current quantity_received vs quantity_ordered, persist them,
    and return the new header status.

    Single source of truth for PO status derivation. A line is PENDING
    when nothing is received, RECEIVED when receipts meet or exceed the
    order, PARTIAL in between; the header is RECEIVED when every line is
    fully received, PARTIAL when any line carries receipts, OPEN
    otherwise. Callers invoke this after changing quantity_ordered (admin
    line edit) or quantity_received (unreceive) so a RECEIVED line
    reopens to PARTIAL once the order grows past what was received -- and
    the header follows in lockstep, which is what makes an over-receipt
    receivable again.
    """
    db.execute(
        text(
            """
            UPDATE purchase_order_lines
               SET status = CASE
                       WHEN quantity_received = 0 THEN :pol_pending
                       WHEN quantity_received >= quantity_ordered THEN :pol_received
                       ELSE :pol_partial
                   END
             WHERE po_id = :pid
            """
        ),
        {
            "pid": po_id,
            "pol_pending": POL_PENDING,
            "pol_received": POL_RECEIVED,
            "pol_partial": POL_PARTIAL,
        },
    )
    lines = db.execute(
        text(
            "SELECT quantity_received, quantity_ordered "
            "  FROM purchase_order_lines WHERE po_id = :pid"
        ),
        {"pid": po_id},
    ).fetchall()
    if lines and all(ln.quantity_received >= ln.quantity_ordered for ln in lines):
        new_status = PO_RECEIVED
    elif any(ln.quantity_received > 0 for ln in lines):
        new_status = PO_PARTIAL
    else:
        new_status = PO_OPEN
    db.execute(
        text("UPDATE purchase_orders SET status = :s WHERE po_id = :pid"),
        {"s": new_status, "pid": po_id},
    )
    return new_status


def record_unreceive(
    db,
    *,
    receipt_id: int,
    username: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Reverse a single PO receipt.

    Inventory model: prefer the receipt's original bin, then walk the
    receipt's warehouse for any other bin holding this item with
    available stock (on_hand - allocated). A strict-bin rule does not
    fit the typical warehouse flow because items get put away from the
    receive bin to a pick bin before the operator notices a
    double-scan; the warehouse-pool model lets the unreceive land
    even after putaway has moved the goods.

    Refuses with kind='insufficient_available' when the warehouse
    total available is below the receipt qty -- typically because the
    item has been picked or shipped against an SO and the inventory
    is gone. The operator handles that gap via an inventory adjustment,
    not this path.

    Caller owns the transaction boundary; this function does not
    commit. The event emission is skipped when no Flask request
    context exists (matches complete_batch / record_admin_pick).

    Returns dict with receipt_id, po_id, po_status (post-recompute),
    quantity_reversed, and per-bin decrement list for the audit.
    """
    receipt = db.execute(
        text(
            "SELECT receipt_id, external_id, po_id, po_line_id, "
            "       item_id, bin_id, warehouse_id, quantity_received, "
            "       lot_number, serial_number "
            "  FROM item_receipts WHERE receipt_id = :rid FOR UPDATE"
        ),
        {"rid": receipt_id},
    ).fetchone()
    if receipt is None:
        raise UnreceiveError(
            f"receipt {receipt_id} not found", kind="not_found",
        )
    if receipt.po_id is None:
        # RMA goods-in receipts go through their own reversal flow
        # (cancel-RMA / restock) and carry so_id instead of po_id.
        # Refusing here avoids muddling the two reversal contracts.
        raise UnreceiveError(
            "this receipt is an RMA goods-in row, not a PO receipt",
            kind="not_a_po_receipt",
            receipt_id=receipt_id,
        )

    qty_to_reverse = receipt.quantity_received

    # Warehouse-pool availability check. If the item has been picked
    # or shipped since the receipt, the warehouse total is below the
    # receipt qty and we refuse -- the operator must adjust inventory
    # first, then unreceive.
    warehouse_available = db.execute(
        text(
            "SELECT COALESCE("
            "  SUM(GREATEST(0, quantity_on_hand - quantity_allocated)), 0"
            ") AS avail "
            "  FROM inventory "
            " WHERE item_id = :iid AND warehouse_id = :wid"
        ),
        {"iid": receipt.item_id, "wid": receipt.warehouse_id},
    ).scalar()
    if warehouse_available < qty_to_reverse:
        raise UnreceiveError(
            (
                f"warehouse {receipt.warehouse_id} has only "
                f"{warehouse_available} available of this item but the "
                f"receipt is for {qty_to_reverse}. The goods may have "
                f"been picked or shipped; adjust inventory before "
                f"unreceiving."
            ),
            kind="insufficient_available",
            warehouse_available=int(warehouse_available),
            receipt_quantity=qty_to_reverse,
        )

    # Walk: try the receipt's original bin first, then other bins
    # ordered by available DESC so we drain the bin most likely to be
    # the putaway target. Per-bin decrement floored at the bin's
    # available stock so we never write a negative on_hand.
    bin_decrements: list[dict] = []
    remaining = qty_to_reverse

    orig = db.execute(
        text(
            "SELECT inventory_id, "
            "       GREATEST(0, quantity_on_hand - quantity_allocated) AS avail "
            "  FROM inventory "
            " WHERE item_id = :iid AND bin_id = :bid AND warehouse_id = :wid "
            " FOR UPDATE"
        ),
        {
            "iid": receipt.item_id,
            "bid": receipt.bin_id,
            "wid": receipt.warehouse_id,
        },
    ).fetchone()
    if orig is not None and orig.avail > 0:
        take = min(remaining, int(orig.avail))
        db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_on_hand = quantity_on_hand - :d, "
                "       updated_at = NOW() "
                " WHERE inventory_id = :id"
            ),
            {"d": take, "id": orig.inventory_id},
        )
        bin_decrements.append({"bin_id": receipt.bin_id, "quantity": take})
        remaining -= take

    if remaining > 0:
        other_invs = db.execute(
            text(
                "SELECT inventory_id, bin_id, "
                "       GREATEST(0, quantity_on_hand - quantity_allocated) AS avail "
                "  FROM inventory "
                " WHERE item_id = :iid AND warehouse_id = :wid "
                "   AND bin_id <> :origbid "
                "   AND quantity_on_hand - quantity_allocated > 0 "
                " ORDER BY (quantity_on_hand - quantity_allocated) DESC, bin_id ASC "
                " FOR UPDATE"
            ),
            {
                "iid": receipt.item_id,
                "wid": receipt.warehouse_id,
                "origbid": receipt.bin_id,
            },
        ).fetchall()
        for inv in other_invs:
            if remaining <= 0:
                break
            take = min(remaining, int(inv.avail))
            if take <= 0:
                continue
            db.execute(
                text(
                    "UPDATE inventory "
                    "   SET quantity_on_hand = quantity_on_hand - :d, "
                    "       updated_at = NOW() "
                    " WHERE inventory_id = :id"
                ),
                {"d": take, "id": inv.inventory_id},
            )
            bin_decrements.append({"bin_id": inv.bin_id, "quantity": take})
            remaining -= take

    if remaining > 0:
        # Defence in depth: the warehouse_available check above already
        # rules this out, but a FOR UPDATE race could in theory leave
        # gap. Raise loudly so the caller rolls back rather than
        # quietly under-reversing.
        raise UnreceiveError(
            "warehouse pool drained mid-walk; rolling back",
            kind="insufficient_available",
            warehouse_available=qty_to_reverse - remaining,
            receipt_quantity=qty_to_reverse,
        )

    # Decrement the PO line's received qty. GREATEST(0, ...) is
    # redundant once the warehouse-available check passes but keeps
    # the same shape as cancel_receiving for reviewer familiarity. The
    # line + header status flips are derived afterward by
    # recompute_po_status so the rule lives in one place.
    db.execute(
        text(
            """
            UPDATE purchase_order_lines
               SET quantity_received = GREATEST(0, quantity_received - :qty)
             WHERE po_line_id = :plid
            """
        ),
        {"qty": qty_to_reverse, "plid": receipt.po_line_id},
    )

    # Hard-delete the receipt row -- the audit row + event carry the
    # forensic record. Soft-delete (status column) would change the
    # item_receipts contract that mobile cancel_receiving and the
    # event-emission path both rely on.
    db.execute(
        text("DELETE FROM item_receipts WHERE receipt_id = :rid"),
        {"rid": receipt_id},
    )

    # PO line + header status recompute. Re-opens any line that dropped
    # below quantity_ordered and the header in lockstep; matches
    # receive_items's status flip on the other side.
    new_po_status = recompute_po_status(db, receipt.po_id)

    write_audit_log(
        db,
        action_type=ACTION_RECEIVE_CANCEL,
        entity_type="PO",
        entity_id=receipt.po_id,
        user_id=username,
        warehouse_id=receipt.warehouse_id,
        details={
            "source": "admin_unreceive",
            "receipt_id": receipt_id,
            "receipt_external_id": str(receipt.external_id),
            "po_line_id": receipt.po_line_id,
            "item_id": receipt.item_id,
            "quantity_reversed": qty_to_reverse,
            "bin_decrements": bin_decrements,
            "reason": reason,
        },
    )

    if has_request_context():
        po = db.execute(
            text("SELECT external_id FROM purchase_orders WHERE po_id = :pid"),
            {"pid": receipt.po_id},
        ).fetchone()
        item = db.execute(
            text("SELECT external_id FROM items WHERE item_id = :iid"),
            {"iid": receipt.item_id},
        ).fetchone()
        po_external_id_str = str(po.external_id)
        emit_event(
            db,
            event_type="receipt.cancelled",
            event_version=1,
            aggregate_type="item_receipt",
            aggregate_id=receipt_id,
            aggregate_external_id=receipt.external_id,
            warehouse_id=receipt.warehouse_id,
            source_txn_id=g.source_txn_id,
            payload={
                "receipt_external_id": str(receipt.external_id),
                "po_external_id": po_external_id_str,
                "lines": [
                    {
                        "item_external_id": str(item.external_id),
                        "quantity_reversed": qty_to_reverse,
                        "lot_number": receipt.lot_number,
                        "serial_number": receipt.serial_number,
                    }
                ],
                "cancelled_by_user_external_id": get_user_external_id(db, username),
                "cancelled_at": (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                ),
                "reason": reason,
            },
        )

    return {
        "receipt_id": receipt_id,
        "po_id": receipt.po_id,
        "po_status": new_po_status,
        "quantity_reversed": qty_to_reverse,
        "bin_decrements": bin_decrements,
    }
