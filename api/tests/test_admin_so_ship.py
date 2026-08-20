"""HTTP + service tests for the admin-ship path.

POST /api/admin/sales-orders/<so_id>/admin-ship hand-stamps shipped quantity on
a picked-but-unshipped SO -- the corrective fix for orders that reached SHIPPED
with quantity_shipped=0 on their lines, so the Create RMA button never rendered.
It writes the fulfillment header + lines, stamps quantity_shipped =
quantity_picked, and writes an ACTION_SHIP audit row.

The ship.confirmed event splits on pre-ship status:
  * A stranded-SHIPPED order already had its revenue booked in the GL at import,
    so the repair emits NOTHING (re-emitting would double-count downstream).
  * A genuinely-unshipped PICKED / PACKED order emits exactly one ship.confirmed,
    with carrier derived from ship_method.
The already-fulfilled guard (409) still means at most one event per SO. Voiding a
corrective ship preserves the order's original tracking/carrier/shipped_at, and a
legacy partial can be shipped with acknowledge_shortfall.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection


# ---- seed helpers -----------------------------------------------------------

def _insert_so(status="SHIPPED", order_type="sale", warehouse_id=1,
               tracking=None, carrier=None, shipped_at=None, ship_method=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, order_type, warehouse_id, external_id, "
        " tracking_number, carrier, shipped_at, ship_method) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING so_id, external_id",
        (f"SO-ADMINSHIP-{uuid.uuid4().hex[:8]}", "Cust", status, order_type,
         warehouse_id, str(uuid.uuid4()), tracking, carrier, shipped_at, ship_method),
    )
    so_id, external_id = cur.fetchone()
    cur.close()
    return so_id, external_id


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


def _insert_line(so_id, item_id, *, qty_ordered=1, qty_picked=1, qty_shipped=0, status="SHIPPED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_order_lines "
        "(so_id, item_id, quantity_ordered, quantity_picked, quantity_shipped, "
        " line_number, status) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s) RETURNING so_line_id",
        (so_id, item_id, qty_ordered, qty_picked, qty_shipped, status),
    )
    sol_id = cur.fetchone()[0]
    cur.close()
    return sol_id


def _insert_fulfillment(so_id, warehouse_id=1, status="SHIPPED"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO item_fulfillments (so_id, warehouse_id, status, external_id) "
        "VALUES (%s, %s, %s, %s) RETURNING fulfillment_id",
        (so_id, warehouse_id, status, str(uuid.uuid4())),
    )
    fid = cur.fetchone()[0]
    cur.close()
    return fid


def _admin_headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


def _read_line(sol_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_shipped, status FROM sales_order_lines WHERE so_line_id = %s",
        (sol_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {"quantity_shipped": row[0], "status": row[1]}


def _count(sql, params):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    n = cur.fetchone()[0]
    cur.close()
    return n


def _ship_confirmed_count(so_id):
    return _count(
        "SELECT COUNT(*) FROM integration_events "
        "WHERE aggregate_id = %s AND event_type = 'ship.confirmed'",
        (so_id,),
    )


def _fulfillment_line_count(so_id):
    return _count(
        "SELECT COUNT(*) FROM item_fulfillment_lines ifl "
        "JOIN item_fulfillments f ON f.fulfillment_id = ifl.fulfillment_id "
        "WHERE f.so_id = %s",
        (so_id,),
    )


def _ship_confirmed_carrier(so_id):
    """The carrier on the most recent ship.confirmed payload for the SO, or None
    when no event was emitted."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT payload->>'carrier' FROM integration_events "
        "WHERE aggregate_id = %s AND event_type = 'ship.confirmed' "
        "ORDER BY event_id DESC LIMIT 1",
        (so_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _so_ship_fields(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT status, tracking_number, carrier, shipped_at "
        "FROM sales_orders WHERE so_id = %s",
        (so_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {"status": row[0], "tracking_number": row[1], "carrier": row[2],
            "shipped_at": row[3]}


def _live_fulfillment(so_id):
    """(fulfillment_id, pre_ship_status) of the SO's live (non-voided)
    fulfillment -- what the void caller resolves and passes in."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT fulfillment_id, pre_ship_status FROM item_fulfillments "
        "WHERE so_id = %s AND status != 'VOIDED' ORDER BY fulfillment_id DESC LIMIT 1",
        (so_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _void_via_service(so_id, so_external_id, fulfillment_id, pre_ship_status):
    """Drive record_void_ship at the service layer (the dockd route is the only
    HTTP caller and needs a wms_token). The test session joins the outer
    transaction via a savepoint, so commit() makes the writes visible to raw
    reads without escaping the test's rollback."""
    from models import database as _dbmod
    from services.shipping_service import record_void_ship

    session = _dbmod.SessionLocal()
    try:
        record_void_ship(
            session,
            so_id=so_id,
            so_number="SO-VOID-TEST",
            so_external_id=so_external_id,
            warehouse_id=1,
            fulfillment_id=fulfillment_id,
            pre_ship_status=pre_ship_status,
            operator_username="admin",
            operator_external_id=str(uuid.uuid4()),
            reason="test void",
            source_txn_id=str(uuid.uuid4()),
        )
        session.commit()
    finally:
        session.close()


def _ship_url(so_id):
    return f"/api/admin/sales-orders/{so_id}/admin-ship"


# ---- tests ------------------------------------------------------------------

class TestAdminShipHappyPath:
    def test_stranded_shipped_so_gets_fulfillment_no_event(self, client):
        """The 648704 case: header SHIPPED, line picked=1 shipped=0, no
        fulfillment. Admin-ship stamps shipped, creates the fulfillment, writes
        the audit row, and satisfies the Create RMA gate -- but emits NO
        ship.confirmed: the order's revenue was already booked in the GL at
        import, so re-emitting would double-count downstream."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED")
        sol_id = _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0)

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["lines_shipped"] == 1
        assert body["total_quantity"] == 1

        line = _read_line(sol_id)
        assert line["quantity_shipped"] == 1  # Create RMA gate: shipped > 0
        assert _fulfillment_line_count(so_id) == 1
        assert _ship_confirmed_count(so_id) == 0  # repair path: no event
        # ACTION_SHIP audit row landed for the SO.
        assert _count(
            "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'SO' "
            "AND entity_id = %s AND action_type = 'SHIP'",
            (so_id,),
        ) >= 1

    def test_picked_so_ships_emits_event_with_derived_carrier(self, client):
        """A genuinely-unshipped PICKED order never reached the GL, so admin-ship
        DOES emit one ship.confirmed. The order has no carrier value (a real ship
        sets it), so the payload carrier is derived from ship_method."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="PICKED", ship_method="UPS Ground")
        sol_id = _insert_line(so_id, item_id, qty_ordered=2, qty_picked=2, qty_shipped=0, status="PICKED")

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert _read_line(sol_id)["quantity_shipped"] == 2
        status = _count("SELECT COUNT(*) FROM sales_orders WHERE so_id = %s AND status = 'SHIPPED'", (so_id,))
        assert status == 1
        assert _ship_confirmed_count(so_id) == 1
        # carrier resolved from "UPS Ground" -> "UPS", not the SO's NULL carrier.
        assert _ship_confirmed_carrier(so_id) == "UPS"


class TestAdminShipGuards:
    def test_already_fulfilled_returns_409_no_second_event(self, client):
        """The double-count guard: an SO that already has a live fulfillment is
        refused, so no second ship.confirmed is ever emitted."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED")
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=1, status="SHIPPED")
        _insert_fulfillment(so_id, status="SHIPPED")

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 409, resp.get_json()
        assert resp.get_json()["kind"] == "already_fulfilled"
        assert _ship_confirmed_count(so_id) == 0  # none emitted

    def test_voided_fulfillment_does_not_block(self, client):
        """A VOIDED fulfillment is not a live one -- admin-ship may still run."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED")
        sol_id = _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0)
        _insert_fulfillment(so_id, status="VOIDED")

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert _read_line(sol_id)["quantity_shipped"] == 1

    def test_nothing_to_ship_returns_422(self, client):
        """A line already shipped=picked (e.g. a POS-shaped SO) has nothing to
        ship."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED")
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=1, status="SHIPPED")

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 422, resp.get_json()
        assert resp.get_json()["kind"] == "nothing_to_ship"

    def test_return_order_rejected(self, client):
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED", order_type="return")
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0)

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 422, resp.get_json()
        assert resp.get_json()["kind"] == "not_eligible"

    def test_wrong_status_rejected(self, client):
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="OPEN")
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=0, qty_shipped=0, status="OPEN")

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 422, resp.get_json()
        assert resp.get_json()["kind"] == "wrong_status"


class TestAdminShipBinResolution:
    def test_corrective_line_uses_a_bin_in_the_so_warehouse(self, client):
        """A stranded-SHIPPED order has no pick_tasks, so the corrective
        fulfillment line resolves a real bin in the SO's OWN warehouse -- never a
        hardcoded id that could belong to another warehouse. Old code wrote bin 1
        (a warehouse-1 bin) regardless; here the SO is in warehouse 2."""
        headers = _admin_headers(client)
        # The seed ships no bins in warehouse 2; add exactly one so resolution is
        # unambiguous and provably not the warehouse-1 bin 1.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bins (zone_id, warehouse_id, bin_code, bin_barcode, "
            " bin_type, pick_sequence, putaway_sequence, external_id) "
            "VALUES (1, 2, %s, %s, 'Pickable', 0, 0, gen_random_uuid()) "
            "RETURNING bin_id",
            (f"W2-{uuid.uuid4().hex[:6]}", f"W2-{uuid.uuid4().hex[:6]}"),
        )
        wh2_bin = cur.fetchone()[0]
        cur.close()

        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED", warehouse_id=2)
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0)

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 200, resp.get_json()

        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT ifl.bin_id, b.warehouse_id FROM item_fulfillment_lines ifl "
            "JOIN item_fulfillments f ON f.fulfillment_id = ifl.fulfillment_id "
            "JOIN bins b ON b.bin_id = ifl.bin_id WHERE f.so_id = %s",
            (so_id,),
        )
        bin_id, wh = cur.fetchone()
        cur.close()
        assert wh == 2, f"corrective line bin is in warehouse {wh}, expected 2"
        assert bin_id == wh2_bin


class TestAdminShipVoidPreservation:
    def test_void_of_corrective_ship_preserves_legacy_ship_fields(self, client):
        """Voiding a corrective admin-ship (pre_ship_status == SHIPPED) must NOT
        erase the order's original imported tracking/carrier/shipped_at -- the
        admin-ship only copied them into the fulfillment, it did not create
        them."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, ext = _insert_so(
            status="SHIPPED", tracking="1Z-LEGACY", carrier="UPS",
            shipped_at="2024-01-15 12:00:00+00",
        )
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0)

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 200, resp.get_json()
        fulfillment_id, pre = _live_fulfillment(so_id)
        assert pre == "SHIPPED"

        _void_via_service(so_id, ext, fulfillment_id, pre)

        fields = _so_ship_fields(so_id)
        assert fields["tracking_number"] == "1Z-LEGACY"
        assert fields["carrier"] == "UPS"
        assert fields["shipped_at"] is not None
        assert fields["status"] == "SHIPPED"  # reverted to the pre-repair state

    def test_void_of_picked_admin_ship_still_nulls_shipped_at(self, client):
        """The contrast: a PICKED admin-ship DID set shipped_at (the order had
        none), so voiding it nulls shipped_at and reverts to PICKED -- unchanged
        behavior for the non-corrective case."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, ext = _insert_so(status="PICKED")
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0, status="PICKED")

        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 200, resp.get_json()
        fulfillment_id, pre = _live_fulfillment(so_id)
        assert pre == "PICKED"

        _void_via_service(so_id, ext, fulfillment_id, pre)

        fields = _so_ship_fields(so_id)
        assert fields["shipped_at"] is None
        assert fields["status"] == "PICKED"


class TestAdminShipShortfall:
    def test_silent_shortfall_refused_then_acknowledged(self, client):
        """A line under-picked with no SHORT / wave marker (a legacy partial) is
        refused with the blocking SKU surfaced; acknowledge_shortfall then ships
        the picked floor."""
        headers = _admin_headers(client)
        item_id = _insert_item()
        so_id, _ = _insert_so(status="PICKED", ship_method="USPS Priority")
        sol_id = _insert_line(so_id, item_id, qty_ordered=2, qty_picked=1, qty_shipped=0, status="PICKED")

        # Refused, with the blocking SKU named.
        resp = client.post(_ship_url(so_id), headers=headers)
        assert resp.status_code == 422, resp.get_json()
        body = resp.get_json()
        assert body["kind"] == "silent_shortfall"
        assert body["lines"], body
        assert body["lines"][0]["ordered"] == 2
        assert body["lines"][0]["picked"] == 1
        # Nothing shipped on the refusal.
        assert _read_line(sol_id)["quantity_shipped"] == 0
        assert _fulfillment_line_count(so_id) == 0

        # Acknowledged -> ships the picked floor (1), emits the PICKED-path event.
        resp2 = client.post(_ship_url(so_id), headers=headers,
                            json={"acknowledge_shortfall": True})
        assert resp2.status_code == 200, resp2.get_json()
        assert _read_line(sol_id)["quantity_shipped"] == 1
        assert _ship_confirmed_count(so_id) == 1


class TestAdminShipAuth:
    def test_non_admin_without_override_returns_403(self, client):
        admin_headers = _admin_headers(client)
        created = client.post(
            "/api/admin/users",
            json={"username": f"shipuser-{uuid.uuid4().hex[:6]}", "password": "password123",
                  "full_name": "Ship User", "role": "USER", "warehouse_ids": [1]},
            headers=admin_headers,
        )
        assert created.status_code in (200, 201), created.get_json()
        user = created.get_json()
        client.put(
            f"/api/admin/users/{user['user_id']}/permissions",
            json={"page_keys": ["sales-orders"]}, headers=admin_headers,
        )
        login = client.post("/api/auth/login",
                            json={"username": user["username"], "password": "password123"})
        user_headers = {"Authorization": f"Bearer {login.get_json()['token']}"}

        item_id = _insert_item()
        so_id, _ = _insert_so(status="SHIPPED")
        _insert_line(so_id, item_id, qty_ordered=1, qty_picked=1, qty_shipped=0)

        resp = client.post(_ship_url(so_id), headers=user_headers)
        assert resp.status_code == 403, resp.get_json()
