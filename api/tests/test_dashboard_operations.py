"""Operations dashboard: /received and /shipping-health.

Covers:
- /api/v1/dashboard/shipping-health:
    bubble set is admin-defined via app_settings.dashboard_bubble_origins;
    no setting -> by_source: [] (nothing implicit from data);
    a configured channel renders even at zero count (LEFT JOIN);
    orders_received / orders_shipped honor the range; need_to_ship_today
    is current-state and excludes SHIPPED / CANCELLED / FRAUD_REVIEW;
    the drill-down `orders` list rides behind the ship-today count;
    label comes from the setting, falling back to the origin value;
    warehouse scoping; warehouse_id required -> 422; bad range -> 422.
- /api/v1/dashboard/received:
    per-PO aggregation of RECEIVE audit rows in range; warehouse_id
    required -> 422.
"""

import json
import os
import sys
from datetime import date, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_test_context


TODAY = date.today()


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


def _set_bubbles(entries):
    """Write app_settings.dashboard_bubble_origins. entries is a list of
    {"origin", "label"} dicts, or None to clear the row entirely."""
    if entries is None:
        _exec("DELETE FROM app_settings WHERE key = 'dashboard_bubble_origins'")
        return
    _exec(
        "INSERT INTO app_settings (key, value) VALUES ('dashboard_bubble_origins', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (json.dumps(entries),),
    )


_SO_SEQ = [9000]


def _seed_so(*, order_origin, status="OPEN", warehouse_id=1,
             ship_by_date=None, created_at=None, shipped_at=None,
             customer_name="ACME"):
    """Insert a minimal sales_orders row with a unique so_number."""
    _SO_SEQ[0] += 1
    n = _SO_SEQ[0]
    _exec(
        "INSERT INTO sales_orders "
        "  (so_number, so_barcode, external_id, status, warehouse_id, "
        "   order_origin, customer_name, ship_by_date, created_at, shipped_at) "
        "VALUES (%s, %s, gen_random_uuid(), %s, %s, %s, %s, %s, "
        "        COALESCE(%s, NOW()), %s)",
        (f"OPS-{n}", f"OPS-{n}", status, warehouse_id, order_origin,
         customer_name, ship_by_date, created_at, shipped_at),
    )
    return f"OPS-{n}"


def _get(client, auth_headers, path):
    return client.get(path, headers=auth_headers)


def _by_origin(payload):
    return {row["order_origin"]: row for row in payload["by_source"]}


@pytest.fixture(autouse=True)
def _clean_dashboard_state():
    # Isolate from other suites: drop our setting + any rows we seeded.
    _set_bubbles(None)
    _exec("DELETE FROM sales_orders WHERE so_number LIKE 'OPS-%%'")
    yield
    _set_bubbles(None)
    _exec("DELETE FROM sales_orders WHERE so_number LIKE 'OPS-%%'")


class TestShippingHealthBubbleSet:
    def test_no_setting_returns_empty_by_source(self, client, auth_headers):
        # Nothing configured -> the dashboard surfaces nothing implicitly.
        _seed_so(order_origin="AMAZON", ship_by_date=TODAY)
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/shipping-health?warehouse_id=1")
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["by_source"] == []

    def test_configured_channel_renders_at_zero(self, client, auth_headers):
        # A configured channel with no matching orders still renders a
        # bubble (LEFT JOIN), so the desk sees a green-check, not a gap.
        _set_bubbles([{"origin": "EBAY", "label": "eBay"}])
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/shipping-health?warehouse_id=1")
        rows = _by_origin(resp.get_json())
        assert set(rows) == {"EBAY"}
        assert rows["EBAY"]["label"] == "eBay"
        assert rows["EBAY"]["orders_received"] == 0
        assert rows["EBAY"]["need_to_ship_today"] == 0
        assert rows["EBAY"]["orders"] == []

    def test_label_falls_back_to_origin(self, client, auth_headers):
        _set_bubbles([{"origin": "DIRECTSALE"}])  # no label
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/shipping-health?warehouse_id=1")
        rows = _by_origin(resp.get_json())
        assert rows["DIRECTSALE"]["label"] == "DIRECTSALE"

    def test_malformed_setting_yields_no_bubbles(self, client, auth_headers):
        _exec(
            "INSERT INTO app_settings (key, value) VALUES "
            "('dashboard_bubble_origins', 'not json') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/shipping-health?warehouse_id=1")
        assert resp.status_code == 200
        assert resp.get_json()["by_source"] == []


class TestShippingHealthCounts:
    def test_need_to_ship_today_excludes_terminal_statuses(self, client, auth_headers):
        _set_bubbles([{"origin": "AMAZON", "label": "Amazon"}])
        # Two open orders due today -> counted; shipped / cancelled /
        # fraud-held / future-dated are not.
        _seed_so(order_origin="AMAZON", status="OPEN", ship_by_date=TODAY)
        _seed_so(order_origin="AMAZON", status="PACKED", ship_by_date=TODAY)
        _seed_so(order_origin="AMAZON", status="SHIPPED", ship_by_date=TODAY)
        _seed_so(order_origin="AMAZON", status="CANCELLED", ship_by_date=TODAY)
        _seed_so(order_origin="AMAZON", status="FRAUD_REVIEW", ship_by_date=TODAY)
        _seed_so(order_origin="AMAZON", status="OPEN",
                 ship_by_date=TODAY + timedelta(days=3))
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/shipping-health?warehouse_id=1")
        rows = _by_origin(resp.get_json())
        assert rows["AMAZON"]["need_to_ship_today"] == 2
        # Drill-down carries the two due-today open orders, oldest first.
        assert len(rows["AMAZON"]["orders"]) == 2
        assert {o["status"] for o in rows["AMAZON"]["orders"]} == {"OPEN", "PACKED"}

    def test_shipped_count_honors_range_and_status(self, client, auth_headers):
        _set_bubbles([{"origin": "AMAZON", "label": "Amazon"}])
        _seed_so(order_origin="AMAZON", status="SHIPPED", shipped_at=TODAY)
        _seed_so(order_origin="AMAZON", status="SHIPPED",
                 shipped_at=TODAY - timedelta(days=10))  # outside today range
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/shipping-health?warehouse_id=1")
        rows = _by_origin(resp.get_json())
        assert rows["AMAZON"]["orders_shipped"] == 1

    def test_warehouse_scoping(self, client, auth_headers):
        _set_bubbles([{"origin": "AMAZON", "label": "Amazon"}])
        _seed_so(order_origin="AMAZON", status="OPEN", warehouse_id=1,
                 ship_by_date=TODAY)
        _seed_so(order_origin="AMAZON", status="OPEN", warehouse_id=2,
                 ship_by_date=TODAY)
        rows = _by_origin(_get(
            client, auth_headers,
            "/api/v1/dashboard/shipping-health?warehouse_id=1").get_json())
        assert rows["AMAZON"]["need_to_ship_today"] == 1


class TestShippingHealthValidation:
    def test_warehouse_id_required(self, client, auth_headers):
        resp = _get(client, auth_headers, "/api/v1/dashboard/shipping-health")
        assert resp.status_code == 422

    def test_bad_range(self, client, auth_headers):
        resp = _get(
            client, auth_headers,
            "/api/v1/dashboard/shipping-health?warehouse_id=1"
            "&start=2026-06-10&end=2026-06-01")
        assert resp.status_code == 422

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/dashboard/shipping-health?warehouse_id=1")
        assert resp.status_code == 401


class TestReceived:
    def test_warehouse_id_required(self, client, auth_headers):
        resp = _get(client, auth_headers, "/api/v1/dashboard/received")
        assert resp.status_code == 422

    def test_returns_pos_shape(self, client, auth_headers):
        # No assertion on specific PO contents (seed-dependent); just the
        # response shape + 200 so a regression in the SQL surfaces.
        resp = _get(client, auth_headers,
                    "/api/v1/dashboard/received?warehouse_id=1")
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert "pos" in body and isinstance(body["pos"], list)
        assert body["warehouse_id"] == 1
