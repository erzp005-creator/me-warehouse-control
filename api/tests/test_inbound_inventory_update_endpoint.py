"""POST /api/v1/inbound/inventory_update end-to-end tests.

The state-based inventory sync endpoint differs from the mapping-driven
resource endpoints: it takes a DESIRED final quantity, computes the delta
against current on-hand, and applies it as an APPROVED inventory
adjustment (emitting the adjustment event). These tests cover:

- Apply path: positive delta adds inventory, writes an adjustment row +
  audit row, emits the adjustment event, returns 201.
- Idempotent no-op: re-pushing the same target returns 200 with
  applied_delta=0 and writes no second adjustment.
- Negative delta removes inventory.
- item_not_found when no cross_system_mappings row resolves the SKU.
- inbound_resource scope violation when the token lacks inventory_update.
"""

import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hashlib

import db_test_context
from _wms_token_helpers import PEPPER
from services import token_cache


def _exec(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _query(sql, params=()):
    return _exec(sql, params)


def _insert_token(ss, plaintext, inbound_resources=("inventory_update",)):
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
                f"invupd-test-{uuid.uuid4().hex[:6]}",
                token_hash, "active",
                [1], [], [],
                ss,
                list(inbound_resources),
                False,
            ),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/inventory_update",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture
def scenario():
    """A source system, a fresh item mapped to a source SKU, and a seeded
    Pickable bin in warehouse 1. The item is created here (not a shared
    seed row) so the quantity math starts from a known zero."""
    ss = f"invupd-{uuid.uuid4().hex[:8]}"
    _query(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (ss,),
    )
    item_external_id = str(uuid.uuid4())
    sku = f"INVUPD-SKU-{uuid.uuid4().hex[:8]}"
    item_id = _query(
        "INSERT INTO items (sku, item_name, external_id) "
        "VALUES (%s, %s, %s) RETURNING item_id",
        (sku, "Inventory Update Test Item", item_external_id),
    )[0][0]
    bin_row = _query(
        "SELECT bin_id, bin_code, warehouse_id FROM bins "
        "WHERE warehouse_id = 1 AND bin_type = 'Pickable' "
        "ORDER BY bin_id LIMIT 1"
    )[0]
    source_sku = f"SRC-{uuid.uuid4().hex[:8]}"
    _query(
        "INSERT INTO cross_system_mappings "
        "(source_system, source_type, source_id, canonical_type, canonical_id) "
        "VALUES (%s, 'item', %s, 'item', %s)",
        (ss, source_sku, item_external_id),
    )
    return {
        "ss": ss,
        "item_id": item_id,
        "item_external_id": item_external_id,
        "source_sku": source_sku,
        "bin_id": bin_row[0],
        "bin_code": bin_row[1],
        "warehouse_id": bin_row[2],
    }


def _onhand(item_id, bin_id):
    rows = _query(
        "SELECT quantity_on_hand FROM inventory WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    return rows[0][0] if rows else 0


class TestInventoryUpdateEndpoint:
    def test_apply_positive_delta_seeds_inventory(self, client, scenario):
        plaintext = "invupd-apply"
        _insert_token(scenario["ss"], plaintext)
        resp = _post(client, plaintext, _body(scenario, 50))

        assert resp.status_code == 201, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["applied_delta"] == 50
        assert body["current_quantity"] == 50
        assert body["noop"] is False
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

        # inventory landed in the target bin
        assert _onhand(scenario["item_id"], scenario["bin_id"]) == 50

        # an APPROVED adjustment row was written for the delta
        adj = _query(
            "SELECT quantity_change, status FROM inventory_adjustments "
            "WHERE item_id = %s AND bin_id = %s ORDER BY adjustment_id DESC LIMIT 1",
            (scenario["item_id"], scenario["bin_id"]),
        )
        assert adj and adj[0][0] == 50 and adj[0][1] == "APPROVED"

        # the adjustment event was emitted onto the outbox
        ev = _query(
            "SELECT COUNT(*) FROM integration_events "
            "WHERE aggregate_type = 'inventory_adjustment' "
            "  AND aggregate_id = %s",
            (adj_id_for(scenario),),
        )
        assert ev[0][0] >= 1

    def test_same_target_is_idempotent_noop(self, client, scenario):
        plaintext = "invupd-noop"
        _insert_token(scenario["ss"], plaintext)
        first = _post(client, plaintext, _body(scenario, 30))
        assert first.status_code == 201
        assert first.get_json()["applied_delta"] == 30

        before = _adjustment_count(scenario)
        second = _post(client, plaintext, _body(scenario, 30))
        assert second.status_code == 200
        body = second.get_json()
        assert body["applied_delta"] == 0
        assert body["noop"] is True
        # no second adjustment row, no extra event
        assert _adjustment_count(scenario) == before

    def test_lower_target_removes_inventory(self, client, scenario):
        plaintext = "invupd-remove"
        _insert_token(scenario["ss"], plaintext)
        _post(client, plaintext, _body(scenario, 40))
        resp = _post(client, plaintext, _body(scenario, 15))
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["applied_delta"] == -25
        assert body["current_quantity"] == 15
        assert _onhand(scenario["item_id"], scenario["bin_id"]) == 15

    def test_unknown_source_sku_is_404(self, client, scenario):
        plaintext = "invupd-missing"
        _insert_token(scenario["ss"], plaintext)
        body = _body(scenario, 10)
        body["source_payload"]["item_external_id"] = "NO-SUCH-SKU"
        resp = _post(client, plaintext, body)
        assert resp.status_code == 404
        assert resp.get_json()["error_kind"] == "item_not_found"

    def test_scope_violation_without_inventory_update_resource(self, client, scenario):
        plaintext = "invupd-scope"
        _insert_token(scenario["ss"], plaintext, inbound_resources=["sales_orders"])
        resp = _post(client, plaintext, _body(scenario, 10))
        assert resp.status_code == 403


def _body(scenario, target_quantity):
    return {
        "external_id": f"EXT-{uuid.uuid4().hex[:8]}",
        "external_version": "v1",
        "source_payload": {
            "item_external_id": scenario["source_sku"],
            "bin_code": scenario["bin_code"],
            "warehouse_id": scenario["warehouse_id"],
            "target_quantity": target_quantity,
            "reason_code": "cutover_seed",
        },
    }


def _adjustment_count(scenario):
    return _query(
        "SELECT COUNT(*) FROM inventory_adjustments WHERE item_id = %s AND bin_id = %s",
        (scenario["item_id"], scenario["bin_id"]),
    )[0][0]


def adj_id_for(scenario):
    return _query(
        "SELECT adjustment_id FROM inventory_adjustments "
        "WHERE item_id = %s AND bin_id = %s ORDER BY adjustment_id DESC LIMIT 1",
        (scenario["item_id"], scenario["bin_id"]),
    )[0][0]
