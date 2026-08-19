"""Zero-row retention regression tests.

When a bin empties, the inventory row must be retained at
quantity_on_hand = 0, never deleted, so the snapshot stays complete and
downstream consumers can distinguish "went to zero" from "never
tracked". Every mutation path that previously deleted at zero is
exercised here:

- admin direct adjustment REMOVE      (admin_users.direct_adjustment)
- admin CSV inventory-adjustment import (admin_items._import_inventory_adjustment)
- admin inter-warehouse transfer      (admin_warehouse.create_inter_warehouse_transfer)
- inbound state-based inventory set   (inbound._inventory_update_post, Pipe B)

The bin-to-bin transfer path (inventory_service.move_inventory) is
covered by test_transfers.py::test_transfer_retains_zero_row_when_bin_empties,
and the snapshot endpoint's zero-row emission by
test_snapshot.py::TestKeysetPaging::test_zero_row_is_included_in_snapshot.
"""

import hashlib
import json
import uuid

import pytest

import db_test_context
from _wms_token_helpers import PEPPER
from services import token_cache


def _query_val(sql, params=None):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def _execute(sql, params=None):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        if cur.description is not None:
            return cur.fetchone()
        return None
    finally:
        cur.close()


def _on_hand(item_id, bin_id):
    """quantity_on_hand for (item, bin), or None if the row is gone."""
    return _query_val(
        "SELECT quantity_on_hand FROM inventory "
        "WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


class TestDirectAdjustmentRetainsZeroRow:
    def test_remove_all_retains_row_at_zero(self, client, auth_headers):
        # Seed: item 6 (TST-006) in bin 8 (A-02-03) with 10 on hand.
        assert _on_hand(6, 8) == 10

        resp = client.post(
            "/api/admin/adjustments/direct",
            json={
                "item_id": 6,
                "bin_id": 8,
                "warehouse_id": 1,
                "adjustment_type": "REMOVE",
                "quantity": 10,
                "reason": "Zero-retention regression",
            },
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201), resp.get_data(as_text=True)

        assert _on_hand(6, 8) == 0, (
            "Direct-adjustment REMOVE to zero must retain the row at 0"
        )


class TestImportAdjustmentRetainsZeroRow:
    def test_negative_import_to_zero_retains_row(self, client, auth_headers):
        # Seed: item 8 (TST-008) in bin 10 (B-01-02) with 15 on hand.
        assert _on_hand(8, 10) == 15

        resp = client.post(
            "/api/admin/import/inventory-adjustments",
            json={
                "records": [
                    {
                        "sku": "TST-008",
                        "warehouse": "APT-LAB",
                        "bin": "B-01-02",
                        "qty": -15,
                        "memo": "Zero-retention regression",
                    }
                ]
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
        assert data["imported"] == 1
        assert data["skipped"] == 0

        assert _on_hand(8, 10) == 0, (
            "CSV adjustment import to zero must retain the row at 0"
        )


class TestInterWarehouseTransferRetainsZeroRow:
    def test_move_all_units_retains_source_row_at_zero(self, client, auth_headers):
        # Warehouse 2 (VIRTUAL) has no bins in the seed; create one
        # inside the test transaction so the transfer has a destination.
        zone = _execute(
            "INSERT INTO zones (warehouse_id, zone_code, zone_name, zone_type) "
            "VALUES (2, 'VSTG', 'Virtual Staging', 'STORAGE') RETURNING zone_id"
        )
        dest_bin = _execute(
            "INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, "
            " bin_type, pick_sequence, putaway_sequence, description, external_id) "
            "VALUES (%s, 2, 'V-01', 'V-01', 'Pickable', 0, 0, 'Virtual bin', %s) "
            "RETURNING bin_id",
            (zone[0], str(uuid.uuid4())),
        )

        # Seed: item 9 (TST-009) in bin 11 (B-01-03) with 20 on hand.
        assert _on_hand(9, 11) == 20

        resp = client.post(
            "/api/admin/inter-warehouse-transfer",
            json={
                "item_id": 9,
                "from_bin_id": 11,
                "from_warehouse_id": 1,
                "to_bin_id": dest_bin[0],
                "to_warehouse_id": 2,
                "quantity": 20,
                "reason": "Zero-retention regression",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_data(as_text=True)

        assert _on_hand(9, 11) == 0, (
            "Inter-warehouse transfer emptying the source bin must retain "
            "the row at 0"
        )
        assert _on_hand(9, dest_bin[0]) == 20


class TestInboundInventorySetRetainsZeroRow:
    """POST /api/v1/inbound/inventory_update with target_quantity=0
    (Pipe B state-based sync) must land the row at 0, not delete it."""

    def _insert_token(self, ss, plaintext):
        conn = db_test_context.get_raw_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
                "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
                (ss,),
            )
            token_hash = hashlib.sha256((PEPPER + plaintext).encode()).hexdigest()
            cur.execute(
                "INSERT INTO wms_tokens "
                "(token_name, token_hash, status, warehouse_ids, event_types, "
                " endpoints, source_system, inbound_resources, mapping_override) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING token_id",
                (
                    f"zero-retention-{uuid.uuid4().hex[:6]}",
                    token_hash, "active",
                    [1], [], [],
                    ss,
                    ["inventory_update"],
                    False,
                ),
            )
            return cur.fetchone()[0]
        finally:
            cur.close()

    def test_set_to_zero_retains_row(self, client, monkeypatch):
        # Pin schema validation off for this request: test_events_service
        # leaks SENTRY_VALIDATE_EVENT_SCHEMAS=true into the session, and
        # this endpoint's adjustment.applied payload carries
        # applied_by_user_external_id=None (Pipe B has no Sentry user)
        # while the schema requires a uuid string -- a pre-existing
        # mismatch unrelated to zero retention.
        monkeypatch.setenv("SENTRY_VALIDATE_EVENT_SCHEMAS", "false")
        ss = f"zerotest-{uuid.uuid4().hex[:8]}"
        plaintext = f"zero-plain-{uuid.uuid4().hex[:8]}"
        self._insert_token(ss, plaintext)

        # Map an external item id onto seed item 20 (TST-020,
        # 200 on hand in bin 12 / BULK-01).
        item_external_id = _query_val(
            "SELECT external_id FROM items WHERE item_id = 20"
        )
        _execute(
            "INSERT INTO cross_system_mappings "
            "(source_system, source_type, source_id, canonical_type, canonical_id) "
            "VALUES (%s, 'item', 'EXT-20', 'item', %s) RETURNING mapping_id",
            (ss, str(item_external_id)),
        )
        assert _on_hand(20, 12) == 200

        resp = client.post(
            "/api/v1/inbound/inventory_update",
            headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
            data=json.dumps({
                "external_id": f"zero-adj-{uuid.uuid4().hex[:8]}",
                "external_version": "v1",
                "source_payload": {
                    "item_external_id": "EXT-20",
                    "bin_code": "BULK-01",
                    "warehouse_id": 1,
                    "target_quantity": 0,
                    "reason_code": "CYCLE_COUNT",
                },
            }),
        )
        assert resp.status_code == 201, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["applied_delta"] == -200
        assert body["target_quantity"] == 0

        assert _on_hand(20, 12) == 0, (
            "Inbound inventory set to zero must retain the row at 0"
        )
