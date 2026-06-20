"""Recompute-service contract for Pipe C (services.channel_availability_service).

The service reconciles per-channel sellable availability against live inventory.
These tests pin the invariants that keep it from lying to a marketplace:

- available = SUM(on_hand - allocated) over Pickable / PickableStaging bins only;
  Staging stock never counts.
- Allocation (reserve-at-creation, cancel, the manual stepper) moves the number,
  not just on-hand changes.
- A version is bumped ONLY when the sellable number actually changes, so a quiet
  warehouse enqueues no publish.
- sku_scope filters which items publish (sku / category) and which warehouses
  count toward the number.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json  # noqa: E402

from sqlalchemy import text as sa_text  # noqa: E402

from db_test_context import get_raw_connection  # noqa: E402
from services.channel_availability_service import (  # noqa: E402
    recompute_active_channels,
    recompute_channel,
)


# ----------------------------------------------------------------------
# Seeding helpers (raw connection, shares the test transaction)
# ----------------------------------------------------------------------

def _mk_item(sku=None, category=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = sku or f"PC-{uuid.uuid4().hex[:10]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, category, is_active, external_id) "
        "VALUES (%s, %s, %s, TRUE, %s) RETURNING item_id",
        (sku, sku, category, str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id, sku


def _bin_of_type(bin_type, *, warehouse_id=None):
    """Return (bin_id, warehouse_id) for a seed bin of the given type."""
    conn = get_raw_connection()
    cur = conn.cursor()
    if warehouse_id is None:
        cur.execute(
            "SELECT bin_id, warehouse_id FROM bins WHERE bin_type = %s "
            "ORDER BY bin_id LIMIT 1",
            (bin_type,),
        )
    else:
        cur.execute(
            "SELECT bin_id, warehouse_id FROM bins "
            "WHERE bin_type = %s AND warehouse_id = %s ORDER BY bin_id LIMIT 1",
            (bin_type, warehouse_id),
        )
    row = cur.fetchone()
    cur.close()
    return (row[0], row[1]) if row else (None, None)


def _set_inv(item_id, bin_id, warehouse_id, *, on_hand, allocated=0):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, "
        "quantity_on_hand, quantity_allocated) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand, "
        "    quantity_allocated = EXCLUDED.quantity_allocated",
        (item_id, bin_id, warehouse_id, on_hand, allocated),
    )
    cur.close()


def _reserve(item_id, bin_id, allocated):
    """Bump quantity_allocated on the existing inventory row, as
    reserve-at-creation does when an order commits stock. A direct UPDATE (not a
    second _set_inv) because ON CONFLICT cannot dedupe a NULL lot_number row
    (V-030), which would otherwise insert a duplicate inventory row."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE inventory SET quantity_allocated = %s "
        "WHERE item_id = %s AND bin_id = %s",
        (allocated, item_id, bin_id),
    )
    cur.close()


def _mk_channel(channel_id, sku_scope=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO channels (channel_id, display_name, delivery_url, "
        "created_by, external_id, sku_scope) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (channel_id, channel_id, "https://sink.example.com/availability",
         "tester", str(uuid.uuid4()), json.dumps(sku_scope or {})),
    )
    cur.close()


def _mk_warehouse_pickable_bin():
    """Create a fresh warehouse + zone + Pickable bin; return (warehouse_id,
    bin_id). The apartment-lab seed puts every pickable bin in warehouse 1, so
    the warehouse-scope test builds its own second warehouse to prove the filter.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    sfx = uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO warehouses (warehouse_code, warehouse_name) "
        "VALUES (%s, %s) RETURNING warehouse_id",
        (f"W-{sfx}", f"WH {sfx}"),
    )
    wh = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) "
        "VALUES (%s, %s, %s, 'PICKING') RETURNING zone_id",
        (wh, f"Z-{sfx}", f"Zone {sfx}"),
    )
    zone = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, "
        "bin_type, external_id) VALUES (%s, %s, %s, %s, 'Pickable', %s) "
        "RETURNING bin_id",
        (zone, wh, f"B-{sfx}", f"BC-{sfx}", str(uuid.uuid4())),
    )
    bin_id = cur.fetchone()[0]
    cur.close()
    return wh, bin_id


def _avail(db, channel_id, item_id):
    row = db.execute(
        sa_text(
            "SELECT available_qty, current_version, last_version, dlq "
            "FROM channel_availability WHERE channel_id = :c AND item_id = :i"
        ),
        {"c": channel_id, "i": item_id},
    ).fetchone()
    return row  # None if the item is not in scope / not materialized


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

class TestSellableComputation:
    def test_staging_stock_is_excluded(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pick_bin, wh = _bin_of_type("Pickable")
        stag_bin, _ = _bin_of_type("Staging", warehouse_id=wh)
        _set_inv(item_id, pick_bin, wh, on_hand=10)
        _set_inv(item_id, stag_bin, wh, on_hand=5)

        bumped = recompute_channel(db, "amz", {"skus": [sku]})
        assert bumped == 1
        row = _avail(db, "amz", item_id)
        # 10 in the pickable bin counts; 5 in Staging does not.
        assert row.available_qty == 10
        # Fresh row is dirty (awaiting first publish).
        assert row.current_version > row.last_version

    def test_allocation_reduces_available(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pick_bin, wh = _bin_of_type("Pickable")
        _set_inv(item_id, pick_bin, wh, on_hand=10, allocated=3)

        recompute_channel(db, "amz", {"skus": [sku]})
        assert _avail(db, "amz", item_id).available_qty == 7  # 10 - 3

        # Reserve the rest (what reserve-at-creation does on a new order).
        _reserve(item_id, pick_bin, allocated=10)
        v_before = _avail(db, "amz", item_id).current_version
        recompute_channel(db, "amz", {"skus": [sku]})
        row = _avail(db, "amz", item_id)
        assert row.available_qty == 0
        assert row.current_version > v_before  # change bumped the version


class TestVersionDiscipline:
    def test_unchanged_recompute_does_not_bump(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pick_bin, wh = _bin_of_type("Pickable")
        _set_inv(item_id, pick_bin, wh, on_hand=8)

        first = recompute_channel(db, "amz", {"skus": [sku]})
        assert first == 1
        v1 = _avail(db, "amz", item_id).current_version

        # Nothing moved; a second sweep must write nothing and bump no version.
        second = recompute_channel(db, "amz", {"skus": [sku]})
        assert second == 0
        assert _avail(db, "amz", item_id).current_version == v1


class TestScopeFiltering:
    def test_sku_scope_excludes_other_items(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        in_id, in_sku = _mk_item()
        out_id, _ = _mk_item()
        pick_bin, wh = _bin_of_type("Pickable")
        _set_inv(in_id, pick_bin, wh, on_hand=4)
        _set_inv(out_id, pick_bin, wh, on_hand=4)

        recompute_channel(db, "amz", {"skus": [in_sku]})
        assert _avail(db, "amz", in_id) is not None
        assert _avail(db, "amz", out_id) is None  # out of scope, never materialized

    def test_category_scope(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        in_id, _ = _mk_item(category="reels")
        out_id, _ = _mk_item(category="waders")
        pick_bin, wh = _bin_of_type("Pickable")
        _set_inv(in_id, pick_bin, wh, on_hand=2)
        _set_inv(out_id, pick_bin, wh, on_hand=2)

        recompute_channel(db, "amz", {"categories": ["reels"]})
        assert _avail(db, "amz", in_id) is not None
        assert _avail(db, "amz", out_id) is None

    def test_warehouse_scope_limits_counted_stock(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pick1, wh1 = _bin_of_type("Pickable")        # seed warehouse 1
        wh2, pick2 = _mk_warehouse_pickable_bin()    # a fresh second warehouse
        _set_inv(item_id, pick1, wh1, on_hand=6)
        _set_inv(item_id, pick2, wh2, on_hand=9)

        recompute_channel(db, "amz", {"skus": [sku], "warehouse_ids": [wh1]})
        # Only wh1 stock counts toward the channel's number; wh2's 9 is excluded.
        assert _avail(db, "amz", item_id).available_qty == 6


class TestActiveChannelOrchestration:
    def test_recompute_active_channels_reads_db_scope(self, _db_transaction):
        db = _db_transaction
        item_id, sku = _mk_item()
        pick_bin, wh = _bin_of_type("Pickable")
        _set_inv(item_id, pick_bin, wh, on_hand=5)
        _mk_channel("livech", sku_scope={"skus": [sku]})

        results = recompute_active_channels(db)
        assert results.get("livech", 0) >= 1
        assert _avail(db, "livech", item_id).available_qty == 5
