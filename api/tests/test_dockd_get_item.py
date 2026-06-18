"""GET /api/v1/dockd/items/<barcode> contract.

Auth-gated item lookup for dockd's Bin Sticker + Item Labels (lookup
mode) features. Mirrors the lookup_bp shape but takes an X-WMS-Token.

Coverage:
- 200 happy path: response shape + DRAFT-v1 header + item/locations
- 200 SKU lookup matches by sku
- 200 UPC lookup matches by upc
- 200 alias lookup matches by barcode_aliases @> ...
- Warehouse scoping: locations in non-allowed warehouses are filtered
  from the response; the item still 200s (catalog row isn't warehouse-
  scoped)
- 404 not_found body shape + DRAFT-v1 header
- 422 invalid_barcode for malformed path parameter
- 401 missing / unknown token
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

from _wms_token_helpers import delete_token, insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def dockd_token(seed_data):
    """Pure-direction dockd station token: dockd.dispatch slug,
    warehouse_id 1, no inbound / outbound markers."""
    plaintext = f"dockd-item-test-{uuid.uuid4()}"
    token_id = insert_token(
        name="Item Lookup Station",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


@pytest.fixture()
def dockd_token_other_warehouse(seed_data):
    plaintext = f"dockd-item-test-wh99-{uuid.uuid4()}"
    token_id = insert_token(
        name="Other Warehouse Station",
        plaintext=plaintext,
        warehouse_ids=[99],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["dockd.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


def _insert_item(sku=None, item_name="Widget", upc="0123456789012",
                 category="general", weight_lbs=None, barcode_aliases=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = sku or f"SKU-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO items "
        "  (sku, item_name, upc, category, weight_lbs, "
        "   barcode_aliases, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) RETURNING item_id",
        (
            sku, item_name, upc, category, weight_lbs,
            barcode_aliases, str(uuid.uuid4()),
        ),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id, sku


def _set_inv(item_id, bin_id, qty_on_hand, qty_allocated=0, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory "
        "  (item_id, bin_id, warehouse_id, quantity_on_hand, quantity_allocated) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
        "SET quantity_on_hand = EXCLUDED.quantity_on_hand, "
        "    quantity_allocated = EXCLUDED.quantity_allocated",
        (item_id, bin_id, warehouse_id, qty_on_hand, qty_allocated),
    )
    cur.close()


# ----------------------------------------------------------------------
# Auth + DRAFT header
# ----------------------------------------------------------------------


class TestAuthAndHeader:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/api/v1/dockd/items/SKU-X")
        assert resp.status_code == 401

    def test_unknown_token_returns_401(self, client, seed_data):
        resp = client.get(
            "/api/v1/dockd/items/SKU-X",
            headers={"X-WMS-Token": "not-real"},
        )
        assert resp.status_code == 401

    def test_happy_response_carries_draft_header(self, client, dockd_token):
        item_id, sku = _insert_item()
        resp = client.get(
            f"/api/v1/dockd/items/{sku}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_404_response_carries_draft_header(self, client, dockd_token):
        resp = client.get(
            "/api/v1/dockd/items/SKU-DOES-NOT-EXIST",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 404
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"
        body = resp.get_json()
        assert body["error_kind"] == "not_found"

    def test_422_response_carries_draft_header(self, client, dockd_token):
        resp = client.get(
            "/api/v1/dockd/items/has spaces",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 422
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"
        body = resp.get_json()
        assert body["error_kind"] == "invalid_barcode"


# ----------------------------------------------------------------------
# Path-parameter validation
# ----------------------------------------------------------------------


class TestPathParameter:
    def test_disallowed_chars_return_422(self, client, dockd_token):
        # Single quote / semicolon are explicitly rejected -- the bind
        # parameter handling is the real defence, but the regex is the
        # outer wall so a SQL-injection-shaped path never gets close
        # to the DB.
        resp = client.get(
            "/api/v1/dockd/items/has'chars",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_barcode"

    def test_too_long_returns_422(self, client, dockd_token):
        long_barcode = "A" * 129
        resp = client.get(
            f"/api/v1/dockd/items/{long_barcode}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 422


# ----------------------------------------------------------------------
# Lookup paths: UPC, SKU, alias
# ----------------------------------------------------------------------


class TestLookupBy:
    def test_upc_lookup(self, client, dockd_token):
        upc = f"UPC{uuid.uuid4().hex[:8]}"
        item_id, sku = _insert_item(upc=upc)
        resp = client.get(
            f"/api/v1/dockd/items/{upc}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["item"]["item_id"] == item_id
        assert body["item"]["sku"] == sku
        assert body["item"]["upc"] == upc

    def test_sku_lookup(self, client, dockd_token):
        item_id, sku = _insert_item()
        resp = client.get(
            f"/api/v1/dockd/items/{sku}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["item"]["item_id"] == item_id

    def test_barcode_alias_lookup(self, client, dockd_token):
        alias = f"ALIAS-{uuid.uuid4().hex[:8]}"
        item_id, sku = _insert_item(
            barcode_aliases=f'["{alias}"]',
        )
        resp = client.get(
            f"/api/v1/dockd/items/{alias}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["item"]["item_id"] == item_id


# ----------------------------------------------------------------------
# Response shape: item fields + locations
# ----------------------------------------------------------------------


class TestResponseShape:
    def test_item_fields(self, client, dockd_token):
        item_id, sku = _insert_item(
            item_name="Test Widget Pro", category="widgets", weight_lbs="1.5",
        )
        resp = client.get(
            f"/api/v1/dockd/items/{sku}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        item = body["item"]
        assert set(item.keys()) == {
            "item_id", "sku", "item_name", "upc",
            "category", "weight_lbs",
        }
        assert item["item_name"] == "Test Widget Pro"
        assert item["category"] == "widgets"
        # weight_lbs returns as float, not decimal/string.
        assert item["weight_lbs"] == 1.5

    def test_locations_include_inventory(self, client, dockd_token):
        item_id, sku = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10, qty_allocated=3)
        _set_inv(item_id, bin_id=4, qty_on_hand=5)
        resp = client.get(
            f"/api/v1/dockd/items/{sku}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        locs = {l["bin_id"]: l for l in body["locations"]}
        assert 3 in locs
        assert 4 in locs
        assert locs[3]["quantity_on_hand"] == 10
        assert locs[3]["quantity_allocated"] == 3
        assert locs[3]["quantity_available"] == 7
        assert set(locs[3].keys()) == {
            "bin_id", "bin_code", "bin_type", "zone_name",
            "quantity_on_hand", "quantity_allocated", "quantity_available",
            "lot_number",
        }


# ----------------------------------------------------------------------
# Warehouse scoping
# ----------------------------------------------------------------------


class TestWarehouseScope:
    def test_item_still_200_when_locations_outside_scope(
        self, client, dockd_token_other_warehouse,
    ):
        # Item exists with inventory in warehouse 1; token is scoped
        # to warehouse 99. Item catalog row 200s, locations come back
        # empty -- the dockd UI sees the item shape but no bins to
        # print stickers from.
        item_id, sku = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10, warehouse_id=1)
        resp = client.get(
            f"/api/v1/dockd/items/{sku}",
            headers={
                "X-WMS-Token": dockd_token_other_warehouse["plaintext"],
            },
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["item"]["item_id"] == item_id
        assert body["locations"] == []

    def test_locations_filtered_to_token_warehouses(self, client, dockd_token):
        # Token is warehouse 1 only. Place stock in warehouse 1 (bin 3)
        # AND warehouse 2 (a fabricated bin). Only the warehouse-1
        # location should appear.
        item_id, sku = _insert_item()
        _set_inv(item_id, bin_id=3, qty_on_hand=10, warehouse_id=1)

        # Fabricate a warehouse-2 bin (seed has the warehouse but no
        # zones/bins).
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO zones "
            "  (zone_name, zone_code, zone_type, warehouse_id) "
            "VALUES (%s, %s, 'Pickable', 2) RETURNING zone_id",
            (
                f"Z-WH2-{uuid.uuid4().hex[:6]}",
                f"ZWH2-{uuid.uuid4().hex[:6]}",
            ),
        )
        zone_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO bins "
            "  (zone_id, warehouse_id, bin_code, bin_barcode, external_id) "
            "VALUES (%s, 2, %s, %s, %s) RETURNING bin_id",
            (
                zone_id,
                f"WH2-{uuid.uuid4().hex[:6]}",
                f"WH2BC-{uuid.uuid4().hex[:6]}",
                str(uuid.uuid4()),
            ),
        )
        wh2_bin = cur.fetchone()[0]
        cur.close()
        _set_inv(item_id, bin_id=wh2_bin, qty_on_hand=5, warehouse_id=2)

        resp = client.get(
            f"/api/v1/dockd/items/{sku}",
            headers={"X-WMS-Token": dockd_token["plaintext"]},
        )
        body = resp.get_json()
        bin_ids = {l["bin_id"] for l in body["locations"]}
        assert 3 in bin_ids
        assert wh2_bin not in bin_ids
