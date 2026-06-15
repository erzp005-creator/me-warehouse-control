"""so-refinement: service-level tests for revert_sales_order_status.

The service is the single transition point for admin-driven backward
SO status flips (PICKED / PACKED / SHIPPED -> earlier). Tests below
cover:

- Per-effect happy paths: release (PICKED -> earlier), unpack
  (PACKED -> below PACKED), unship (SHIPPED -> earlier), and the
  composed paths.
- The release-only mode (new_status == current_status): picks
  release, status stays put, no SO_STATUS_REVERTED audit row, no
  unship / unpack side effects even when current is SHIPPED / PACKED.
- The picked_qty_remaining guard: target below PICKED with any
  unreleased pick rejects loudly with kind='picked_qty_remaining'.
- All RevertNotAllowed kinds: not_backward, invalid_status,
  not_found, cancelled, pick_task_missing, pick_task_wrong_state.
- Audit shape per effect (one SO_STATUS_REVERTED per request when
  status flips, one SO_PICK_RELEASED per released task, single
  SO_UNPACKED / SO_UNSHIPPED rows when those effects fire).
- Edge cases: multiple pick_tasks on a single line, pick_task with
  quantity_picked=0 (short pick), idempotent re-release rejects via
  wrong_state.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import text as sa_text

from db_test_context import get_raw_connection


# ----------------------------------------------------------------------
# Fixture helpers. Match the style of test_cancel_sales_order_service.py:
# raw-cursor inserts so the per-test transaction owns every byte.
# ----------------------------------------------------------------------


def _ensure_user(username="op"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row:
        cur.close()
        return row[0]
    cur.execute(
        "INSERT INTO users (username, password_hash, full_name, role, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING user_id",
        (username,
         "$2b$12$placeholderHashForTests000000000000000000000000000000",
         username.title(), "USER", str(uuid.uuid4())),
    )
    user_id = cur.fetchone()[0]
    cur.close()
    return user_id


def _insert_so(status="OPEN", warehouse_id=1, **extra):
    """Insert a sales_orders row. **extra applies as a follow-up UPDATE
    so the base INSERT keeps a static column list -- the
    test_external_id_inserts scanner reads SQL as literal text and
    cannot see runtime-constructed column lists."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, external_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING so_id",
        (f"SO-REVERT-{uuid.uuid4().hex[:8]}", "Cust", status,
         warehouse_id, str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    for col, val in extra.items():
        cur.execute(
            f"UPDATE sales_orders SET {col} = %s WHERE so_id = %s",
            (val, so_id),
        )
    cur.close()
    return so_id


def _insert_item():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (f"SKU-{uuid.uuid4().hex[:8]}", "Widget", "0123456789012", str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so_line(so_id, item_id, *, qty_ordered=2, qty_allocated=0,
                     qty_picked=0, qty_packed=0, status="PENDING"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_allocated, "
        " quantity_picked, quantity_packed, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_allocated, qty_picked,
         qty_packed, 1, status),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _set_inv(item_id, bin_id, *, qty_on_hand, qty_allocated=0, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, "
        "quantity_on_hand, quantity_allocated) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand, "
        "    quantity_allocated = EXCLUDED.quantity_allocated",
        (item_id, bin_id, warehouse_id, qty_on_hand, qty_allocated),
    )
    cur.close()


def _get_inv_on_hand(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand FROM inventory "
        "WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _insert_pick_task(so_id, sol_id, item_id, bin_id, *, qty_to_pick=2,
                       qty_picked=None, status="PICKED"):
    """Insert a pick_task with optional explicit quantity_picked.

    Defaults to quantity_picked == quantity_to_pick when status==PICKED
    so a normal pick lands consistent. Pass qty_picked=0 to simulate a
    short pick that ended with zero units actually moved.
    """
    if qty_picked is None:
        qty_picked = qty_to_pick if status in ("PICKED", "RELEASED") else 0
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pick_batches (batch_number, warehouse_id, status) "
        "VALUES (%s, 1, 'OPEN') RETURNING batch_id",
        (f"BATCH-{uuid.uuid4().hex[:8]}",),
    )
    batch_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pick_tasks (batch_id, so_id, so_line_id, item_id, "
        "bin_id, quantity_to_pick, quantity_picked, status, pick_sequence) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING pick_task_id",
        (batch_id, so_id, sol_id, item_id, bin_id, qty_to_pick, qty_picked,
         status, 1),
    )
    task_id = cur.fetchone()[0]
    cur.close()
    return task_id


def _audit_rows(db, so_id, action_type=None):
    if action_type:
        return db.execute(
            sa_text(
                "SELECT action_type, details FROM audit_log "
                " WHERE entity_type = 'SO' AND entity_id = :sid "
                "   AND action_type = :act "
                " ORDER BY log_id"
            ),
            {"sid": so_id, "act": action_type},
        ).fetchall()
    return db.execute(
        sa_text(
            "SELECT action_type, details FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = :sid "
            " ORDER BY log_id"
        ),
        {"sid": so_id},
    ).fetchall()


# ----------------------------------------------------------------------
# Happy-path: release picks (PICKED -> earlier statuses)
# ----------------------------------------------------------------------


class TestReleasePicksPickedToEarlier:
    def test_picked_to_open_full_release(self, _db_transaction):
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        # Source bin starts at 8 because picking moved 2 units out;
        # release should restore back to 10.
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        result = revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        # Status flipped.
        status = db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().status
        assert status == "OPEN"
        # Inventory restored to source bin (not default receiving).
        assert _get_inv_on_hand(item_id, bin_id=3) == 10
        # SOL.quantity_picked decremented.
        qty_picked = db.execute(
            sa_text("SELECT quantity_picked FROM sales_order_lines "
                    "WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone().quantity_picked
        assert qty_picked == 0
        # pick_task marked RELEASED (audit retained, line link
        # intact -- the FK SET NULL only fires on subsequent SOL delete).
        pt_row = db.execute(
            sa_text("SELECT status, so_line_id, quantity_picked, picked_at "
                    "FROM pick_tasks WHERE pick_task_id = :p"),
            {"p": pt_id},
        ).fetchone()
        assert pt_row.status == "RELEASED"
        assert pt_row.so_line_id == sol_id
        assert pt_row.quantity_picked == 2

        # Return-value contract.
        assert result["from_status"] == "PICKED"
        assert result["to_status"] == "OPEN"
        assert result["unpacked"] is False
        assert result["unshipped"] is False
        assert len(result["released_pick_tasks"]) == 1
        detail = result["released_pick_tasks"][0]
        assert detail["pick_task_id"] == pt_id
        assert detail["so_line_id"] == sol_id
        assert detail["item_id"] == item_id
        assert detail["bin_id"] == 3
        assert detail["quantity"] == 2


# ----------------------------------------------------------------------
# Happy-path: unpack (PACKED -> below PACKED)
# ----------------------------------------------------------------------


class TestUnpack:
    def test_packed_to_picked_unpacks_only(self, _db_transaction):
        """PACKED -> PICKED zeros quantity_packed and clears packed_at.
        No inventory move; quantity_picked stays put since target is PICKED."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PACKED")
        # Stamp packed_at so we can verify it clears.
        db.execute(
            sa_text("UPDATE sales_orders SET packed_at = NOW() "
                    "WHERE so_id = :s"),
            {"s": so_id},
        )
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="PACKED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          qty_to_pick=2, status="PICKED")

        result = revert_sales_order_status(
            db, so_id=so_id, new_status="PICKED",
            release_pick_task_ids=[], username="op",
        )

        # quantity_packed zeroed.
        row = db.execute(
            sa_text("SELECT quantity_packed, quantity_picked "
                    "FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert row.quantity_packed == 0
        # quantity_picked preserved (target PICKED still requires picks).
        assert row.quantity_picked == 2
        # packed_at cleared on the header.
        packed_at = db.execute(
            sa_text("SELECT packed_at FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().packed_at
        assert packed_at is None
        # No inventory move.
        assert _get_inv_on_hand(item_id, bin_id=3) == 8
        # Status flipped.
        status = db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().status
        assert status == "PICKED"

        assert result["unpacked"] is True
        assert result["unshipped"] is False
        assert result["released_pick_tasks"] == []

    def test_packed_to_open_unpacks_and_releases(self, _db_transaction):
        """PACKED -> OPEN must unpack AND release all picks (otherwise
        the picked_qty_remaining guard fires)."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PACKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="PACKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        result = revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        row = db.execute(
            sa_text("SELECT quantity_packed, quantity_picked "
                    "FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert row.quantity_packed == 0
        assert row.quantity_picked == 0
        assert _get_inv_on_hand(item_id, bin_id=3) == 10
        assert result["unpacked"] is True
        assert result["unshipped"] is False
        assert len(result["released_pick_tasks"]) == 1


# ----------------------------------------------------------------------
# Happy-path: unship (SHIPPED -> earlier)
# ----------------------------------------------------------------------


class TestUnship:
    def test_shipped_to_packed_clears_shipment_only(self, _db_transaction):
        """SHIPPED -> PACKED clears tracking_number/carrier/shipped_at
        on the header. quantity_packed stays; no inventory move."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(
            status="SHIPPED",
            tracking_number="1Z999AA10123456784",
            carrier="UPS",
        )
        db.execute(
            sa_text("UPDATE sales_orders SET shipped_at = NOW() "
                    "WHERE so_id = :s"),
            {"s": so_id},
        )
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="SHIPPED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          qty_to_pick=2, status="PICKED")

        result = revert_sales_order_status(
            db, so_id=so_id, new_status="PACKED",
            release_pick_task_ids=[], username="op",
        )

        hdr = db.execute(
            sa_text("SELECT status, tracking_number, carrier, shipped_at "
                    "FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone()
        assert hdr.status == "PACKED"
        assert hdr.tracking_number is None
        assert hdr.carrier is None
        assert hdr.shipped_at is None
        # quantity_packed preserved (target == PACKED).
        qpacked = db.execute(
            sa_text("SELECT quantity_packed FROM sales_order_lines "
                    "WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone().quantity_packed
        assert qpacked == 2
        # No inventory move.
        assert _get_inv_on_hand(item_id, bin_id=3) == 8

        assert result["unshipped"] is True
        assert result["unpacked"] is False

    def test_shipped_to_open_full_unwind(self, _db_transaction):
        """SHIPPED -> OPEN clears shipment fields + unpacks + releases
        every pick. All three effects compose."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(
            status="SHIPPED",
            tracking_number="1Z999AA10123456784",
            carrier="UPS",
        )
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="SHIPPED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        result = revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        hdr = db.execute(
            sa_text("SELECT status, tracking_number FROM sales_orders "
                    "WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone()
        assert hdr.status == "OPEN"
        assert hdr.tracking_number is None
        row = db.execute(
            sa_text("SELECT quantity_packed, quantity_picked "
                    "FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert row.quantity_packed == 0
        assert row.quantity_picked == 0
        assert _get_inv_on_hand(item_id, bin_id=3) == 10
        assert result["unshipped"] is True
        assert result["unpacked"] is True
        assert len(result["released_pick_tasks"]) == 1


# ----------------------------------------------------------------------
# Release-only mode (new_status == current_status)
# ----------------------------------------------------------------------


class TestReleaseOnly:
    def test_release_only_picked_releases_without_status_flip(self, _db_transaction):
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        result = revert_sales_order_status(
            db, so_id=so_id, new_status="PICKED",
            release_pick_task_ids=[pt_id], username="op",
        )

        # Picks released, inventory restored.
        assert _get_inv_on_hand(item_id, bin_id=3) == 10
        # Status unchanged.
        status = db.execute(
            sa_text("SELECT status FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().status
        assert status == "PICKED"
        # No SO_STATUS_REVERTED audit row since status didn't change.
        rows = _audit_rows(db, so_id, action_type="SO_STATUS_REVERTED")
        assert rows == []
        # SO_PICK_RELEASED row still written (the release is what
        # actually happened).
        pick_rel = _audit_rows(db, so_id, action_type="SO_PICK_RELEASED")
        assert len(pick_rel) == 1

        assert result["from_status"] == "PICKED"
        assert result["to_status"] == "PICKED"

    def test_release_only_on_shipped_does_not_unship(self, _db_transaction):
        """SHIPPED -> SHIPPED in release-only must NOT clear shipment
        fields. need_unship requires new_status != SHIPPED."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(
            status="SHIPPED",
            tracking_number="1Z999AA10123456784",
            carrier="UPS",
        )
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="SHIPPED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="SHIPPED",
            release_pick_task_ids=[pt_id], username="op",
        )

        hdr = db.execute(
            sa_text("SELECT tracking_number, carrier FROM sales_orders "
                    "WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone()
        assert hdr.tracking_number == "1Z999AA10123456784"
        assert hdr.carrier == "UPS"

    def test_release_only_on_packed_does_not_unpack(self, _db_transaction):
        """PACKED -> PACKED in release-only must NOT zero quantity_packed.
        need_unpack requires new_idx < PACKED."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PACKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="PACKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="PACKED",
            release_pick_task_ids=[pt_id], username="op",
        )

        qpacked = db.execute(
            sa_text("SELECT quantity_packed FROM sales_order_lines "
                    "WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone().quantity_packed
        assert qpacked == 2


# ----------------------------------------------------------------------
# Partial-release behavior
# ----------------------------------------------------------------------


class TestPartialRelease:
    def test_partial_release_target_picked_succeeds(self, _db_transaction):
        """Two pick_tasks on the SO; release one, keep one; target=PICKED.
        Status stays at PICKED, one pick released, one remains."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=6)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=4,
                                  qty_picked=4, status="PICKED")
        pt_a = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")
        pt_b = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="PICKED",
            release_pick_task_ids=[pt_a], username="op",
        )

        statuses = {r.pick_task_id: r.status for r in db.execute(
            sa_text("SELECT pick_task_id, status FROM pick_tasks "
                    "WHERE so_id = :s"),
            {"s": so_id},
        ).fetchall()}
        assert statuses[pt_a] == "RELEASED"
        assert statuses[pt_b] == "PICKED"
        # One pick released -> SOL.quantity_picked drops by 2.
        qty_picked = db.execute(
            sa_text("SELECT quantity_picked FROM sales_order_lines "
                    "WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone().quantity_picked
        assert qty_picked == 2

    def test_partial_release_target_below_picked_rejects(self, _db_transaction):
        """Two picks; release one, target=OPEN -> remaining qty_picked=2
        means picked_qty_remaining guard fires."""
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=6)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=4,
                                  qty_picked=4, status="PICKED")
        pt_a = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          qty_to_pick=2, status="PICKED")

        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=so_id, new_status="OPEN",
                release_pick_task_ids=[pt_a], username="op",
            )
        assert exc.value.kind == "picked_qty_remaining"
        assert exc.value.context["remaining_picked"] == 2
        assert exc.value.context["target_status"] == "OPEN"


# ----------------------------------------------------------------------
# Error paths: every RevertNotAllowed kind
# ----------------------------------------------------------------------


class TestRevertNotAllowed:
    def test_invalid_status_rejects(self, _db_transaction):
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="PICKED")
        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=so_id, new_status="BOGUS",
                release_pick_task_ids=[], username="op",
            )
        assert exc.value.kind == "invalid_status"

    def test_so_not_found_rejects(self, _db_transaction):
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=9_999_999, new_status="OPEN",
                release_pick_task_ids=[], username="op",
            )
        assert exc.value.kind == "not_found"

    def test_cancelled_so_rejects(self, _db_transaction):
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="CANCELLED")
        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=so_id, new_status="OPEN",
                release_pick_task_ids=[], username="op",
            )
        assert exc.value.kind == "cancelled"
        assert exc.value.context["current_status"] == "CANCELLED"

    def test_forward_transition_rejects(self, _db_transaction):
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        so_id = _insert_so(status="PICKED")
        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=so_id, new_status="PACKED",
                release_pick_task_ids=[], username="op",
            )
        assert exc.value.kind == "not_backward"
        assert exc.value.context["current_status"] == "PICKED"
        assert exc.value.context["target_status"] == "PACKED"

    def test_pick_task_not_on_so_rejects(self, _db_transaction):
        """A pick_task_id that belongs to a different SO must not be
        accepted -- the operator could otherwise release someone else's
        inventory by guessing IDs."""
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10)
        # SO 1: the one being reverted.
        so_id = _insert_so(status="PICKED")
        _insert_so_line(so_id, item_id, qty_ordered=2, qty_picked=2,
                        status="PICKED")
        # SO 2: a totally unrelated SO with its own pick_task.
        other_so = _insert_so(status="PICKED")
        other_sol = _insert_so_line(other_so, item_id, qty_ordered=2,
                                     qty_picked=2, status="PICKED")
        other_pt = _insert_pick_task(other_so, other_sol, item_id, bin_id=3,
                                      qty_to_pick=2, status="PICKED")

        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=so_id, new_status="OPEN",
                release_pick_task_ids=[other_pt], username="op",
            )
        assert exc.value.kind == "pick_task_missing"
        assert other_pt in exc.value.context["missing_ids"]

    def test_pick_task_wrong_state_rejects(self, _db_transaction):
        """Re-releasing an already-RELEASED pick_task must fail loudly
        rather than silently double-adjusting inventory."""
        from services.sales_order_service import (
            RevertNotAllowed, revert_sales_order_status,
        )
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="RELEASED")

        with pytest.raises(RevertNotAllowed) as exc:
            revert_sales_order_status(
                db, so_id=so_id, new_status="OPEN",
                release_pick_task_ids=[pt_id], username="op",
            )
        assert exc.value.kind == "pick_task_wrong_state"
        assert pt_id in exc.value.context["wrong_state_ids"]


# ----------------------------------------------------------------------
# Audit-log shape
# ----------------------------------------------------------------------


class TestAuditShape:
    def test_status_reverted_row_only_when_status_changes(self, _db_transaction):
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        rev_rows = _audit_rows(db, so_id, action_type="SO_STATUS_REVERTED")
        assert len(rev_rows) == 1
        details = rev_rows[0].details
        assert details["from_status"] == "PICKED"
        assert details["to_status"] == "OPEN"
        assert details["released_pick_tasks"] == [pt_id]
        assert details["unpacked"] is False
        assert details["unshipped"] is False

    def test_one_pick_released_row_per_released_task(self, _db_transaction):
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=6)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=4,
                                  qty_picked=4, status="PICKED")
        pt_a = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")
        pt_b = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_a, pt_b], username="op",
        )

        rows = _audit_rows(db, so_id, action_type="SO_PICK_RELEASED")
        assert len(rows) == 2
        ids = {r.details["pick_task_id"] for r in rows}
        assert ids == {pt_a, pt_b}
        for r in rows:
            assert r.details["item_id"] == item_id
            assert r.details["bin_id"] == 3
            assert r.details["quantity"] == 2

    def test_unpacked_row_when_unpack_fires(self, _db_transaction):
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PACKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_picked=2, qty_packed=2,
                                  status="PACKED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                          qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="PICKED",
            release_pick_task_ids=[], username="op",
        )

        rows = _audit_rows(db, so_id, action_type="SO_UNPACKED")
        assert len(rows) == 1
        assert rows[0].details["from_status"] == "PACKED"
        assert rows[0].details["to_status"] == "PICKED"

    def test_unshipped_row_carries_prev_values(self, _db_transaction):
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(
            status="SHIPPED",
            tracking_number="1Z999AA10123456784",
            carrier="UPS",
        )
        _insert_so_line(so_id, item_id, qty_ordered=2, qty_picked=2,
                        qty_packed=2, status="SHIPPED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="PACKED",
            release_pick_task_ids=[], username="op",
        )

        rows = _audit_rows(db, so_id, action_type="SO_UNSHIPPED")
        assert len(rows) == 1
        details = rows[0].details
        assert details["prev_tracking_number"] == "1Z999AA10123456784"
        assert details["prev_carrier"] == "UPS"


# ----------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------


class TestEdgeCases:
    def test_short_pick_zero_quantity_skips_inventory(self, _db_transaction):
        """A pick_task with quantity_picked=0 (the operator marked it
        short with zero units physically moved) must not write a
        SO_PICK_RELEASED audit row and must not move inventory, but
        must still flip the task to RELEASED and undo the allocation
        the original create_pick_batch booked so a subsequent revert
        prompt does not re-offer it and a re-scan sees the line as
        needing coverage."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_allocated=2, qty_picked=0,
                                  status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, qty_picked=0,
                                   status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        # Inventory untouched (no physical move to restore).
        assert _get_inv_on_hand(item_id, bin_id=3) == 10
        # pick_task flips to RELEASED so it does not re-surface in
        # future revert prompts.
        pt_status = db.execute(
            sa_text("SELECT status FROM pick_tasks WHERE pick_task_id = :p"),
            {"p": pt_id},
        ).fetchone().status
        assert pt_status == "RELEASED"
        # sol.quantity_allocated drops back to 0 so a re-scan's
        # `quantity_ordered > quantity_allocated` filter picks the line
        # up again.
        sol_row = db.execute(
            sa_text("SELECT quantity_allocated, quantity_picked "
                    "  FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert sol_row.quantity_allocated == 0
        assert sol_row.quantity_picked == 0
        # No SO_PICK_RELEASED audit row -- no physical inventory event
        # to narrate.
        rows = _audit_rows(db, so_id, action_type="SO_PICK_RELEASED")
        assert rows == []

    def test_multiple_pick_tasks_release_decrements_correctly(self, _db_transaction):
        """Same SOL has two PICKED tasks (e.g. picked from two bins).
        Releasing both decrements quantity_picked by the cumulative
        quantity and restores each bin independently."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        _set_inv(item_id, bin_id=4, qty_on_hand=7)
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=5,
                                  qty_picked=5, status="PICKED")
        pt_a = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")
        pt_b = _insert_pick_task(so_id, sol_id, item_id, bin_id=4,
                                  qty_to_pick=3, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_a, pt_b], username="op",
        )

        assert _get_inv_on_hand(item_id, bin_id=3) == 10
        assert _get_inv_on_hand(item_id, bin_id=4) == 10
        qty_picked = db.execute(
            sa_text("SELECT quantity_picked FROM sales_order_lines "
                    "WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone().quantity_picked
        assert qty_picked == 0


# ----------------------------------------------------------------------
# Regression: release must also decrement sales_order_lines.
# quantity_allocated. Without this, a re-scan after revert sees lines
# as "fully allocated" and create_pick_batch skips them, producing a
# batch with fewer pick_tasks than the SO needs. Layer 2A then refuses
# to complete the batch and the picker is stuck.
# ----------------------------------------------------------------------


class TestReleaseDecrementsAllocated:
    def test_release_zeroes_quantity_allocated_on_line(self, _db_transaction):
        """A full release of a single PICKED task must drop both
        sol.quantity_picked and sol.quantity_allocated back to 0 so a
        re-scan re-allocates cleanly."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        so_id = _insert_so(status="PICKED")
        # Seed mirrors post-create_pick_batch + confirm_pick state:
        # the line was allocated and then fully picked.
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_allocated=2, qty_picked=2,
                                  status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        sol_row = db.execute(
            sa_text("SELECT quantity_allocated, quantity_picked "
                    "  FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert sol_row.quantity_picked == 0
        assert sol_row.quantity_allocated == 0

    def test_partial_release_decrements_allocated_per_task(self, _db_transaction):
        """Two PICKED tasks on one line, only one released. Allocation
        drops by that task's quantity_to_pick, picked drops by the
        same task's quantity_picked. The other task's allocation stays
        on the line until it is released too."""
        from services.sales_order_service import revert_sales_order_status
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=8)
        _set_inv(item_id, bin_id=4, qty_on_hand=7)
        # Status PICKED so target=PICKED stays equal (release-only),
        # keeping the un-released task in place without tripping
        # picked_qty_remaining.
        so_id = _insert_so(status="PICKED")
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=5,
                                  qty_allocated=5, qty_picked=5,
                                  status="PICKED")
        pt_a = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                  qty_to_pick=2, status="PICKED")
        _insert_pick_task(so_id, sol_id, item_id, bin_id=4,
                          qty_to_pick=3, status="PICKED")

        revert_sales_order_status(
            db, so_id=so_id, new_status="PICKED",
            release_pick_task_ids=[pt_a], username="op",
        )

        sol_row = db.execute(
            sa_text("SELECT quantity_allocated, quantity_picked "
                    "  FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        # Only pt_a (qty=2) was released, so the line keeps the
        # remaining task's 3 units allocated + picked.
        assert sol_row.quantity_allocated == 3
        assert sol_row.quantity_picked == 3

    def test_release_then_recreate_batch_produces_pick_tasks(self, _db_transaction):
        """End-to-end regression for the prod-stuck-pick bug. After
        full release + revert to OPEN, a fresh create_pick_batch run on
        the same SO must allocate all the lines again and produce one
        pick_task per line. Pre-fix: zero pick_tasks were produced
        because sol.quantity_allocated still equalled quantity_ordered
        and the coverage query returned no rows."""
        import os as _os
        # The route stack expects the test pepper.
        _os.environ.setdefault("SENTRY_TOKEN_PEPPER",
                                "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")
        from services.sales_order_service import revert_sales_order_status
        from services.picking_service import create_pick_batch
        db = _db_transaction
        _ensure_user("op")
        item_id = _insert_item()
        # Sufficient stock to fully cover both rounds: pre-pick bin has
        # 10 on hand, the pick consumes 2, release restores to 10.
        _set_inv(item_id, bin_id=3, qty_on_hand=10)
        # Bin must be PICKABLE for create_pick_batch's inventory query
        # to return it. The seed bin_id=3 already exists in seed_data
        # as PICKABLE in the test DB; if a different bin_id is needed,
        # adjust here.
        so_id = _insert_so(status="PICKED")
        so_num = db.execute(
            sa_text("SELECT so_number FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().so_number
        sol_id = _insert_so_line(so_id, item_id, qty_ordered=2,
                                  qty_allocated=2, qty_picked=2,
                                  status="PICKED")
        pt_id = _insert_pick_task(so_id, sol_id, item_id, bin_id=3,
                                   qty_to_pick=2, status="PICKED")

        # Revert PICKED -> OPEN with full release.
        revert_sales_order_status(
            db, so_id=so_id, new_status="OPEN",
            release_pick_task_ids=[pt_id], username="op",
        )

        # Re-create a batch for the same SO. Pre-fix this would create
        # a batch with zero pick_tasks; post-fix it should allocate the
        # line again and produce exactly one pick_task for qty=2.
        result = create_pick_batch(
            db, so_identifiers=[so_num], warehouse_id=1, username="op",
        )

        # The batch should carry the SO and one pick_task for qty 2.
        assert result["total_items"] == 2
        assert len(result["tasks"]) == 1
        new_pt = result["tasks"][0]
        assert new_pt["quantity_to_pick"] == 2
        # The task list output does not expose so_line_id; verify the
        # FK lands on our line via the pick_tasks row.
        new_sol_id = db.execute(
            sa_text("SELECT so_line_id FROM pick_tasks "
                    "WHERE pick_task_id = :p"),
            {"p": new_pt["pick_task_id"]},
        ).fetchone().so_line_id
        assert new_sol_id == sol_id
        # Line is allocated again from scratch.
        sol_row = db.execute(
            sa_text("SELECT quantity_allocated, quantity_picked "
                    "  FROM sales_order_lines WHERE so_line_id = :s"),
            {"s": sol_id},
        ).fetchone()
        assert sol_row.quantity_allocated == 2
        assert sol_row.quantity_picked == 0
