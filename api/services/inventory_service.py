"""
Shared inventory manipulation functions used by receiving, putaway, transfers,
and other workflows that touch inventory quantities.

V-030: all inventory mutations below are race-safe. ``add_inventory`` uses
``INSERT ... ON CONFLICT DO UPDATE`` so concurrent callers cannot create
duplicate rows or overwrite each other. ``move_inventory`` locks the source
row with ``SELECT ... FOR UPDATE`` before checking available quantity, so
two concurrent moves from the same bin cannot both pass the
sufficient-stock check.
"""

from datetime import datetime, timezone

from sqlalchemy import text

from constants import (
    SO_OPEN, SO_WAITING_STOCK,
    BIN_PICKABLE, BIN_PICKABLE_STAGING,
)
from services.events_service import emit_event

# How the stock that satisfied a backorder arrived. Rides on
# backorder.fulfillable so a consumer can tell a truck receipt from a
# cycle-count correction without joining back to the audit log.
RELEASE_SOURCE_RECEIPT = "receipt"
RELEASE_SOURCE_ADJUSTMENT = "adjustment"
RELEASE_SOURCE_CYCLE_COUNT = "cycle_count"
RELEASE_SOURCE_TRANSFER = "transfer"
RELEASE_SOURCE_SYNC = "sync"


def set_inventory_quantity(db, inventory_id, quantity):
    """Set quantity_on_hand on an existing inventory row.

    Zero is a first-class state: the row is retained at quantity_on_hand = 0
    when a bin empties, never deleted, so the inventory snapshot stays
    complete and downstream consumers can distinguish "went to zero" from
    "never tracked". Rows are only removed when their item or bin is
    itself deleted.
    """
    db.execute(
        text(
            "UPDATE inventory SET quantity_on_hand = :qty, updated_at = NOW() WHERE inventory_id = :inv_id"
        ),
        {"qty": quantity, "inv_id": inventory_id},
    )


def add_inventory(db, item_id, bin_id, warehouse_id, quantity, lot_number=None):
    """Increment existing inventory or create a new record.

    V-030 race safety: Postgres's default UNIQUE(item_id, bin_id, lot_number)
    treats NULL lot_numbers as distinct, so ON CONFLICT cannot serialize
    concurrent inserts when lot is NULL (the common case). We use a
    transaction-scoped advisory lock keyed on (item_id, bin_id) to
    serialize callers, then the existing SELECT-then-INSERT-or-UPDATE
    flow runs safely. The lock releases automatically at COMMIT or
    ROLLBACK.

    Returns the new quantity_on_hand at (item_id, bin_id, lot_number).
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(:iid, :bid)"),
        {"iid": item_id, "bid": bin_id},
    )

    existing = db.execute(
        text(
            """
            SELECT inventory_id, quantity_on_hand
            FROM inventory
            WHERE item_id = :item_id AND bin_id = :bin_id
              AND lot_number IS NOT DISTINCT FROM :lot_number
            FOR UPDATE
            """
        ),
        {"item_id": item_id, "bin_id": bin_id, "lot_number": lot_number},
    ).fetchone()

    if existing:
        new_qty = existing.quantity_on_hand + quantity
        db.execute(
            text(
                """
                UPDATE inventory
                SET quantity_on_hand = quantity_on_hand + :qty, updated_at = NOW()
                WHERE inventory_id = :inv_id
                """
            ),
            {"qty": quantity, "inv_id": existing.inventory_id},
        )
        return new_qty
    else:
        db.execute(
            text(
                """
                INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, lot_number)
                VALUES (:item_id, :bin_id, :warehouse_id, :qty, :lot_number)
                """
            ),
            {
                "item_id": item_id,
                "bin_id": bin_id,
                "warehouse_id": warehouse_id,
                "qty": quantity,
                "lot_number": lot_number,
            },
        )
        return quantity


def move_inventory(db, item_id, from_bin_id, to_bin_id, warehouse_id, quantity, lot_number=None):
    """Atomic bin-to-bin inventory transfer.

    Locks the source row with SELECT ... FOR UPDATE so a concurrent
    move from the same bin cannot also pass the sufficient-stock check.
    Decrements source (retaining the row at 0 when the bin empties),
    upserts destination via add_inventory's ON CONFLICT path.

    Returns (new_source_qty, new_dest_qty).
    Raises ValueError if insufficient inventory in source bin.
    """
    # V-030: lock the source row until commit so concurrent callers
    # serialize through this critical section.
    source_inv = db.execute(
        text(
            """
            SELECT inventory_id, quantity_on_hand
            FROM inventory
            WHERE item_id = :item_id AND bin_id = :bin_id
              AND lot_number IS NOT DISTINCT FROM :lot_number
            FOR UPDATE
            """
        ),
        {"item_id": item_id, "bin_id": from_bin_id, "lot_number": lot_number},
    ).fetchone()

    if not source_inv or source_inv.quantity_on_hand < quantity:
        available = source_inv.quantity_on_hand if source_inv else 0
        raise ValueError(f"Insufficient inventory in source bin. Available: {available}")

    # Decrement source
    new_source_qty = source_inv.quantity_on_hand - quantity
    set_inventory_quantity(db, source_inv.inventory_id, new_source_qty)

    # Upsert destination (atomic via ON CONFLICT).
    new_dest_qty = add_inventory(db, item_id, to_bin_id, warehouse_id, quantity, lot_number)

    return new_source_qty, new_dest_qty


def release_satisfiable_backorders(
    db, *, warehouse_id, item_id, source_txn_id, deferred_notifications,
    source=RELEASE_SOURCE_RECEIPT,
):
    """After stock lands for (warehouse_id, item_id), look at WAITING_STOCK
    backorders in that warehouse that carry a line on this item. For each,
    check whether ALL its lines are now satisfiable against current pickable
    on-hand. Flip the satisfiable ones to OPEN, stamp
    backorder_fulfillable_at, and emit backorder.fulfillable. One
    (event_type, payload, warehouse_id) tuple is appended to
    ``deferred_notifications`` per flipped BO; the caller drains the list
    AFTER its commit to fire the corresponding Teams cards.

    Partial-satisfaction stays in WAITING_STOCK with no event (matches the
    spec's "notify only when fully available" decision so a Teams ping
    doesn't fire until the warehouse can actually pull the BO).

    Inventory is NOT consumed here; the BO becomes pickable for the
    next batch builder.

    ``source`` records HOW the stock arrived, so a downstream consumer can
    tell a truck receipt from a cycle-count correction. It changes nothing
    about whether a backorder releases.

    This used to live in receiving.py and ran only on POST /receive, so
    a backorder sat in WAITING_STOCK while the item was on the shelf in a
    pickable bin in that exact warehouse. It is here now because stock lands
    by several routes and every one of them should be able to call it. It is
    deliberately NOT hooked inside add_inventory(): the cycle-count approval
    queue writes inventory with raw SQL and never calls it, so a hook there
    would miss the path that matters most.
    """
    candidates = db.execute(
        text(
            """
            SELECT bo.so_id, bo.so_number, bo.external_id, bo.warehouse_id,
                   bo.backorder_opened_at, bo.parent_so_id, bo.customer_name
              FROM sales_orders bo
             WHERE bo.status = :waiting
               AND bo.warehouse_id = :wid
               AND EXISTS (
                 SELECT 1 FROM sales_order_lines bol
                  WHERE bol.so_id = bo.so_id
                    AND bol.item_id = :iid
               )
             ORDER BY bo.backorder_opened_at ASC
            """
        ),
        {"waiting": SO_WAITING_STOCK, "wid": warehouse_id, "iid": item_id},
    ).fetchall()

    for bo in candidates:
        bo_lines = db.execute(
            text(
                """
                SELECT sol.so_line_id, sol.item_id, sol.quantity_ordered,
                       i.sku, i.item_name, i.external_id AS item_external_id
                  FROM sales_order_lines sol
                  JOIN items i ON i.item_id = sol.item_id
                 WHERE sol.so_id = :bid
                """
            ),
            {"bid": bo.so_id},
        ).fetchall()
        if not bo_lines:
            continue

        all_satisfiable = True
        for line in bo_lines:
            available = db.execute(
                text(
                    """
                    SELECT COALESCE(
                             SUM(GREATEST(inv.quantity_on_hand - inv.quantity_allocated, 0)),
                             0
                           )
                      FROM inventory inv
                      JOIN bins b ON b.bin_id = inv.bin_id
                     WHERE inv.item_id = :iid
                       AND inv.warehouse_id = :wid
                       AND b.bin_type IN (:bp, :bps)
                    """
                ),
                {
                    "iid": line.item_id,
                    "wid": warehouse_id,
                    "bp": BIN_PICKABLE,
                    "bps": BIN_PICKABLE_STAGING,
                },
            ).scalar() or 0
            if available < line.quantity_ordered:
                all_satisfiable = False
                break
        if not all_satisfiable:
            continue

        db.execute(
            text(
                "UPDATE sales_orders "
                "   SET status = :open, backorder_fulfillable_at = NOW() "
                " WHERE so_id = :bid"
            ),
            {"open": SO_OPEN, "bid": bo.so_id},
        )

        days_waiting = 0
        if bo.backorder_opened_at:
            row = db.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - :opened))::bigint AS s"
                ),
                {"opened": bo.backorder_opened_at},
            ).fetchone()
            if row and row.s:
                days_waiting = int(row.s) // 86400

        parent_so_number = ""
        if bo.parent_so_id is not None:
            parent_so_number = db.execute(
                text("SELECT so_number FROM sales_orders WHERE so_id = :sid"),
                {"sid": bo.parent_so_id},
            ).scalar() or ""

        fulfillable_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        fulfillable_payload = {
            "backorder_so_external_id": str(bo.external_id),
            "backorder_so_number": bo.so_number,
            "parent_so_number": parent_so_number,
            "warehouse_id": warehouse_id,
            "customer_name": bo.customer_name,
            "items": [
                {
                    "item_external_id": str(line.item_external_id),
                    "sku": line.sku,
                    "item_name": line.item_name,
                    "qty": line.quantity_ordered,
                }
                for line in bo_lines
            ],
            "days_waiting": days_waiting,
            "fulfillable_at": fulfillable_at,
            "source": source,
        }
        emit_event(
            db,
            event_type="backorder.fulfillable",
            event_version=1,
            aggregate_type="sales_order",
            aggregate_id=bo.so_id,
            aggregate_external_id=bo.external_id,
            warehouse_id=warehouse_id,
            source_txn_id=source_txn_id,
            payload=fulfillable_payload,
        )
        deferred_notifications.append(
            ("backorder.fulfillable", fulfillable_payload, warehouse_id)
        )
