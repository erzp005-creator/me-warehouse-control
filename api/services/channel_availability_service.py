"""
Pipe C recompute service: reconcile per-channel sellable availability against
live inventory.

Design: this is a RECONCILIATION sweep, not a delta applier. Each call recomputes
``available = SUM(GREATEST(on_hand - allocated, 0))`` over a channel's in-scope
warehouses (Pickable / PickableStaging bins only) straight from the current
``inventory`` rows, and writes the result into ``channel_availability``. It does
not trust an event payload's numbers; it reads the truth.

Why a sweep and not pure event-driven: ``available`` depends on
``quantity_allocated``, which moves at order ingestion (reserve-at-creation,
v1.21) and on cancel / release / the manual stepper -- none of which emit an
integration_event. A consumer that only replayed the event stream would miss the
single most common availability change (a new order reserving stock) and would
silently lie to the marketplace about sellable counts. Reconciling against live
inventory is self-healing: it converges to the truth no matter which code path
moved the numbers, including paths that emit nothing and paths that do not exist
yet. The integration_events stream is used by the daemon (see
``services/connector_publisher``) only to WAKE this sweep sooner than its periodic
tick -- never as the source of the numbers.

Version discipline: ``current_version`` is bumped (from
``channel_availability_version_seq``) only for rows whose ``available_qty``
actually changes. A sweep that finds nothing changed writes nothing and enqueues
no publish, so a quiet warehouse produces zero outbound traffic. A bumped row is
"dirty" (``current_version > last_version``) until the publisher delivers it.
"""

import json

from sqlalchemy import text

from constants import BIN_PICKABLE, BIN_PICKABLE_STAGING


def _coerce_scope(sku_scope):
    """Normalize a channel's sku_scope JSONB into (skus, categories,
    warehouse_ids), each a non-empty list or None.

    JSONB columns decode to dicts via psycopg2, but a str is tolerated (e.g. a
    hand-built test row) and parsed. An absent or empty dimension imposes no
    restriction on that dimension.
    """
    if not sku_scope:
        return None, None, None
    if isinstance(sku_scope, str):
        sku_scope = json.loads(sku_scope)

    def _clean(key):
        vals = sku_scope.get(key)
        return list(vals) if vals else None

    return _clean("skus"), _clean("categories"), _clean("warehouse_ids")


def recompute_channel(db, channel_id, sku_scope, *, item_ids=None):
    """Reconcile one channel's availability against live inventory.

    ``item_ids`` None runs a full sweep over every in-scope item (also the seed
    path for a brand-new channel: every item is "new", so every in-scope row is
    inserted dirty and the publisher sends the initial snapshot). Passing a
    subset restricts the sweep to those items -- the event fast-path the daemon
    can use to recompute just what an inventory event touched.

    Returns the number of rows whose version was bumped (rows whose sellable
    number actually changed). A return of 0 means the channel is already in sync.
    """
    skus, categories, warehouse_ids = _coerce_scope(sku_scope)

    params = {
        "cid": channel_id,
        "bp": BIN_PICKABLE,
        "bps": BIN_PICKABLE_STAGING,
    }

    # The warehouse filter scopes WHICH warehouses' stock counts toward the
    # number. It lives in the pickable-inventory aggregate, not the outer item
    # filter.
    wh_clause = ""
    if warehouse_ids:
        wh_clause = "AND inv.warehouse_id = ANY(:wh_ids)"
        params["wh_ids"] = warehouse_ids

    # The item filters define WHICH items publish on this channel. skus OR
    # categories: an item in scope if it matches either dimension that is set.
    item_clauses = []
    if skus:
        item_clauses.append("i.sku = ANY(:skus)")
        params["skus"] = skus
    if categories:
        item_clauses.append("i.category = ANY(:categories)")
        params["categories"] = categories
    scope_clause = ""
    if item_clauses:
        scope_clause = "AND (" + " OR ".join(item_clauses) + ")"

    # Targeted subset (event fast-path / tests).
    target_clause = ""
    if item_ids is not None:
        target_clause = "AND i.item_id = ANY(:item_ids)"
        params["item_ids"] = list(item_ids)

    # pickable_inv aggregates only Pickable / PickableStaging bins in the
    # in-scope warehouses, so non-sellable Staging stock never inflates the
    # number. computed then LEFT JOINs every in-scope active item to that
    # aggregate, defaulting to 0 -- an item with no sellable stock publishes a
    # truthful 0 rather than dropping out. changed keeps only rows whose number
    # moved, so the upsert bumps a version (and enqueues a publish) for exactly
    # those.
    sql = f"""
        WITH pickable_inv AS (
            SELECT inv.item_id,
                   SUM(GREATEST(inv.quantity_on_hand - inv.quantity_allocated, 0)) AS qty
              FROM inventory inv
              JOIN bins b ON b.bin_id = inv.bin_id
             WHERE b.bin_type IN (:bp, :bps)
               {wh_clause}
             GROUP BY inv.item_id
        ),
        computed AS (
            SELECT i.item_id,
                   COALESCE(pi.qty, 0)::int AS qty
              FROM items i
              LEFT JOIN pickable_inv pi ON pi.item_id = i.item_id
             WHERE i.is_active = TRUE
               {scope_clause}
               {target_clause}
        ),
        changed AS (
            SELECT c.item_id, c.qty
              FROM computed c
              LEFT JOIN channel_availability ca
                     ON ca.channel_id = :cid AND ca.item_id = c.item_id
             WHERE ca.item_id IS NULL
                OR ca.available_qty IS DISTINCT FROM c.qty
        )
        INSERT INTO channel_availability (
            channel_id, item_id, available_qty,
            current_version, last_version, updated_at
        )
        SELECT :cid, ch.item_id, ch.qty,
               nextval('channel_availability_version_seq'), 0, NOW()
          FROM changed ch
        ON CONFLICT (channel_id, item_id) DO UPDATE
           SET available_qty   = EXCLUDED.available_qty,
               current_version = nextval('channel_availability_version_seq'),
               attempt_count   = 0,
               next_attempt_at = NULL,
               dlq             = FALSE,
               last_error      = NULL,
               updated_at      = NOW()
    """
    return db.execute(text(sql), params).rowcount


def recompute_active_channels(db, *, item_ids=None):
    """Sweep every active channel. Returns {channel_id: rows_bumped}.

    Paused / revoked channels are skipped: their config is frozen and the
    publisher is not draining them, so there is no point materializing churn
    against a channel nobody is shipping.
    """
    channels = db.execute(
        text("SELECT channel_id, sku_scope FROM channels WHERE status = 'active'")
    ).fetchall()
    return {
        ch.channel_id: recompute_channel(
            db, ch.channel_id, ch.sku_scope, item_ids=item_ids
        )
        for ch in channels
    }
