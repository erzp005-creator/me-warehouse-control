"""GET /api/v1/pos/sales-orders/<so_number> contract (attach-order lookup).

Sentry-first SO lookup for the POS Replacement / Exchange / Refund attach-order
step: returns the SO header plus its lines (sku, name, ordered + shipped qty) so
the cashier can pick which items. Warehouse-scoped to the token, 404-conflated
out of scope, mirroring /availability.
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
def pos_token(seed_data):
    plaintext = f"pos-test-{uuid.uuid4()}"
    token_id = insert_token(
        name="POS Register 1",
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["pos.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


@pytest.fixture()
def pos_token_other_warehouse(seed_data):
    plaintext = f"pos-test-wh99-{uuid.uuid4()}"
    token_id = insert_token(
        name="POS Other Warehouse",
        plaintext=plaintext,
        warehouse_ids=[99],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["pos.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


def _insert_item(sku):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (sku, item_name, upc, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING item_id",
        (sku, "Widget", uuid.uuid4().hex[:12], str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id


def _insert_so(so_number, *, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Jane Buyer", "SHIPPED", warehouse_id, "sale", "web", str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _insert_so_line(so_id, item_id, *, qty_ordered=3, qty_shipped=3, line_number=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_shipped, line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_shipped, line_number, "SHIPPED"),
    )
    so_line_id = cur.fetchone()[0]
    cur.close()
    return so_line_id


def _hdr(tok):
    return {"X-WMS-Token": tok["plaintext"]}


class TestSalesOrderLookup:
    def test_returns_so_with_lines(self, client, pos_token):
        son = f"POS-{uuid.uuid4().hex[:8]}"
        so_id = _insert_so(son, warehouse_id=1)
        sku = f"SKU-{uuid.uuid4().hex[:8]}"
        item = _insert_item(sku)
        _insert_so_line(so_id, item, qty_ordered=3, qty_shipped=2, line_number=1)

        resp = client.get(f"/api/v1/pos/sales-orders/{son}", headers=_hdr(pos_token))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["so_number"] == son
        assert body["order_type"] == "sale"
        assert len(body["lines"]) == 1
        ln = body["lines"][0]
        assert ln["sku"] == sku
        assert ln["quantity_ordered"] == 3
        assert ln["quantity_shipped"] == 2
        assert resp.headers["X-Sentry-Canonical-Model"] == "DRAFT-v1"

    def test_returns_customer_phone_and_ship_to(self, client, pos_token):
        # The attach-order lookup carries the order's customer phone + the
        # structured ship-to so the POS Replacement/Exchange flow can
        # auto-attach the customer and prefill the new SO's destination.
        son = f"POS-{uuid.uuid4().hex[:8]}"
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sales_orders "
            "(so_number, customer_name, customer_phone, status, warehouse_id, "
            " order_type, order_source, external_id, shipping_address_name, "
            " shipping_address_line1, shipping_address_line2, "
            " shipping_address_city, shipping_address_state, "
            " shipping_address_postal_code, shipping_address_country, "
            " shipping_address_phone) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING so_id",
            (
                son, "Jane Buyer", "303-555-0199", "SHIPPED", 1, "sale", "web",
                str(uuid.uuid4()), "Jane Buyer", "742 Evergreen Terrace",
                "Apt 2", "Denver", "CO", "80202", "US", "303-555-0199",
            ),
        )
        so_id = cur.fetchone()[0]
        cur.close()
        item = _insert_item(f"SKU-{uuid.uuid4().hex[:8]}")
        _insert_so_line(so_id, item)

        resp = client.get(f"/api/v1/pos/sales-orders/{son}", headers=_hdr(pos_token))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["customer_phone"] == "303-555-0199"
        ship = body["shipping_address"]
        assert ship is not None
        assert ship["name"] == "Jane Buyer"
        assert ship["line1"] == "742 Evergreen Terrace"
        assert ship["line2"] == "Apt 2"
        assert ship["city"] == "Denver"
        assert ship["state"] == "CO"
        assert ship["postal_code"] == "80202"

    def test_no_ship_to_returns_null(self, client, pos_token):
        # An order with no structured address returns shipping_address: null
        # (the POS leaves the ship-to blank for the rep to fill).
        son = f"POS-{uuid.uuid4().hex[:8]}"
        so_id = _insert_so(son, warehouse_id=1)
        item = _insert_item(f"SKU-{uuid.uuid4().hex[:8]}")
        _insert_so_line(so_id, item)

        resp = client.get(f"/api/v1/pos/sales-orders/{son}", headers=_hdr(pos_token))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["shipping_address"] is None
        assert body["customer_phone"] is None

    def test_unknown_so_returns_404(self, client, pos_token):
        resp = client.get(
            f"/api/v1/pos/sales-orders/POS-{uuid.uuid4().hex[:8]}",
            headers=_hdr(pos_token),
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_kind"] == "order_not_found"

    def test_out_of_scope_so_returns_404(self, client, pos_token_other_warehouse):
        son = f"POS-{uuid.uuid4().hex[:8]}"
        _insert_so(son, warehouse_id=1)  # token scope is [99]
        resp = client.get(
            f"/api/v1/pos/sales-orders/{son}",
            headers=_hdr(pos_token_other_warehouse),
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_kind"] == "order_not_found"

    def test_missing_token_returns_401(self, client, seed_data):
        resp = client.get("/api/v1/pos/sales-orders/POS-1")
        assert resp.status_code == 401

    def test_overlong_so_number_returns_422(self, client, pos_token):
        resp = client.get(
            "/api/v1/pos/sales-orders/" + ("A" * 65), headers=_hdr(pos_token)
        )
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "invalid_query_param"
