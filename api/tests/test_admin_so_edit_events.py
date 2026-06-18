"""Admin SO-edit event emission (so-ship-events).

Covers the PUT /api/admin/sales-orders/<id> edit surface:

  * Every successful header edit emits salesorderedit.completed carrying
    the per-field diff (sales orders previously emitted nothing on edit).
  * A status -> SHIPPED flip on that same edit is a real ship: it routes
    through record_ship (item_fulfillments row + line flips + ACTION_SHIP
    audit + ship.confirmed event), gated on PICKED/PACKED + every line
    picked + tracking/carrier present, and additionally emits ship.confirmed.

Drives the real handler via the cookie-auth + SQLAlchemy-savepoint
fixture and asserts the integration_events rows land with the right
envelope and validate against the registered JSON Schemas. Reuses the
picking/packing drivers and event-readback helpers from
test_event_emission so the setup matches the existing ship.confirmed test.
"""

import json
import uuid

from db_test_context import get_raw_connection

# Boot the registry so payloads validate, and reuse the existing drivers.
from services.events_schema_registry import get_validator  # noqa: F401
from test_event_emission import (
    _advance_so_through_picking,
    _advance_so_to_packed,
    _assert_payload_matches_schema,
    _query_event_rows,
)

SO_NUMBER = "SO-2026-001"
SO_ID = 1
TRACKING = "1Z999AA10123456784"


def _fulfillment_rows(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT fulfillment_id, status, tracking_number, carrier, "
        "       shipped_at, pre_ship_status, shipped_at::date::text AS shipped_date "
        "  FROM item_fulfillments WHERE so_id = %s ORDER BY fulfillment_id",
        (so_id,),
    )
    cols = [c.name for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    return rows


def _so_row(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT status, tracking_number, carrier, shipped_at::date::text AS shipped_date "
        "  FROM sales_orders WHERE so_id = %s",
        (so_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {"status": row[0], "tracking_number": row[1], "carrier": row[2], "shipped_date": row[3]}


def _ship_audit_via_admin(so_id):
    """ACTION_SHIP audit rows for this SO carrying the admin-edit marker."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT details FROM audit_log "
        " WHERE entity_type = 'SO' AND entity_id = %s "
        "   AND details->>'via' = 'admin_so_edit'",
        (so_id,),
    )
    rows = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in cur.fetchall()]
    cur.close()
    return rows


def _put_so(client, auth_headers, so_id, body, request_id):
    return client.put(
        f"/api/admin/sales-orders/{so_id}",
        json=body,
        headers={**auth_headers, "X-Request-ID": request_id},
    )


class TestShipTransitionEmission:
    def test_picked_to_shipped_emits_both_events(self, client, auth_headers, seed_data):
        _advance_so_through_picking(client, auth_headers, SO_NUMBER)

        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {"status": "SHIPPED", "tracking_number": TRACKING, "carrier": "UPS"},
            request_id,
        )
        assert resp.status_code == 200, resp.get_json()

        rows = _query_event_rows(request_id)
        by_type = {r["event_type"]: r for r in rows}
        assert set(by_type) == {"ship.confirmed", "salesorderedit.completed"}

        # ship.confirmed: real shipment payload, picked units reported even
        # though the order never went through packing.
        ship = by_type["ship.confirmed"]["payload"]
        assert ship["tracking_numbers"] == [TRACKING]
        assert ship["carrier"] == "UPS"
        pkg = ship["packages"][0]
        assert len(pkg["lines"]) >= 1
        assert any(line["quantity_packed"] >= 1 for line in pkg["lines"]), (
            "a ship from PICKED must report the picked units, not 0"
        )
        _assert_payload_matches_schema("ship.confirmed", 1, ship)

        # salesorderedit.completed: carries the per-field diff.
        edit = by_type["salesorderedit.completed"]["payload"]
        assert edit["so_number"] == SO_NUMBER
        assert edit["status"] == "SHIPPED"
        changed = {c["field"]: c for c in edit["changes"]}
        assert changed["status"]["new_value"] == "SHIPPED"
        assert changed["status"]["old_value"] == "PICKED"
        assert changed["tracking_number"]["new_value"] == TRACKING
        assert uuid.UUID(edit["edited_by_user_external_id"])
        _assert_payload_matches_schema("salesorderedit.completed", 1, edit)

        # State: a real fulfillment row + SHIPPED header + ACTION_SHIP audit.
        fulfillments = _fulfillment_rows(SO_ID)
        assert len(fulfillments) == 1
        assert fulfillments[0]["status"] == "SHIPPED"
        assert fulfillments[0]["tracking_number"] == TRACKING
        assert fulfillments[0]["pre_ship_status"] == "PICKED"
        assert _so_row(SO_ID)["status"] == "SHIPPED"
        assert len(_ship_audit_via_admin(SO_ID)) == 1

    def test_manual_ship_without_carrier_derives_it_from_ship_method(
        self, client, auth_headers, seed_data
    ):
        # The prod scenario: the admin modal has no carrier field, so a manual
        # ship sends Ship Method + Tracking but no carrier. The carrier must be
        # derived (not rejected) so ship.confirmed/1 still carries a non-null
        # carrier, and the header/fulfillment/event all agree.
        _advance_so_through_picking(client, auth_headers, SO_NUMBER)

        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {
                "status": "SHIPPED",
                "tracking_number": TRACKING,
                "ship_method": "UPS (UPS Ground)",
            },
            request_id,
        )
        assert resp.status_code == 200, resp.get_json()

        ship = {
            r["event_type"]: r["payload"] for r in _query_event_rows(request_id)
        }["ship.confirmed"]
        assert ship["carrier"] == "UPS"
        assert ship["service_level"] == "UPS (UPS Ground)"
        _assert_payload_matches_schema("ship.confirmed", 1, ship)

        assert _so_row(SO_ID)["carrier"] == "UPS"
        assert _fulfillment_rows(SO_ID)[0]["carrier"] == "UPS"

    def test_manual_ship_generic_ship_method_defaults_carrier_to_other(
        self, client, auth_headers, seed_data
    ):
        # A ship method that names no carrier ("Standard", "FreeEconomy", ...)
        # must still ship: carrier falls back to "Other" rather than blocking.
        _advance_so_through_picking(client, auth_headers, SO_NUMBER)

        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {
                "status": "SHIPPED",
                "tracking_number": TRACKING,
                "ship_method": "Standard",
            },
            request_id,
        )
        assert resp.status_code == 200, resp.get_json()

        ship = {
            r["event_type"]: r["payload"] for r in _query_event_rows(request_id)
        }["ship.confirmed"]
        assert ship["carrier"] == "Other"
        _assert_payload_matches_schema("ship.confirmed", 1, ship)
        assert _so_row(SO_ID)["carrier"] == "Other"

    def test_backdated_shipped_date_flows_to_fulfillment_and_event(
        self, client, auth_headers, seed_data
    ):
        _advance_so_through_picking(client, auth_headers, SO_NUMBER)

        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {
                "status": "SHIPPED",
                "tracking_number": TRACKING,
                "carrier": "UPS",
                "shipped_at": "2026-05-01",
            },
            request_id,
        )
        assert resp.status_code == 200, resp.get_json()

        # Both the fulfillment row and the SO header carry the operator date,
        # and ship.confirmed.completed_at reflects it (not NOW()).
        assert _fulfillment_rows(SO_ID)[0]["shipped_date"] == "2026-05-01"
        assert _so_row(SO_ID)["shipped_date"] == "2026-05-01"
        ship = {
            r["event_type"]: r["payload"] for r in _query_event_rows(request_id)
        }["ship.confirmed"]
        assert ship["completed_at"].startswith("2026-05-01")

    def test_open_to_shipped_rejected_requires_picked_or_packed(
        self, client, auth_headers, seed_data
    ):
        # so_id 2 is a fresh seed SO, never advanced through picking.
        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, 2,
            {"status": "SHIPPED", "tracking_number": TRACKING, "carrier": "UPS"},
            request_id,
        )
        assert resp.status_code == 422, resp.get_json()
        assert "PICKED or PACKED" in resp.get_json()["error"]
        assert _query_event_rows(request_id) == []
        assert _fulfillment_rows(2) == []

    def test_ship_without_tracking_rejected(self, client, auth_headers, seed_data):
        _advance_so_through_picking(client, auth_headers, SO_NUMBER)

        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {"status": "SHIPPED", "carrier": "UPS"},
            request_id,
        )
        assert resp.status_code == 422, resp.get_json()
        assert "tracking_number" in resp.get_json()["error"]
        assert _query_event_rows(request_id) == []
        assert _fulfillment_rows(SO_ID) == []

    def test_underpicked_line_rejected_and_rolled_back(
        self, client, auth_headers, seed_data
    ):
        _advance_so_through_picking(client, auth_headers, SO_NUMBER)
        # Manufacture a silent shortfall: raise one line's ordered qty above
        # its picked qty with no short-close marker. record_ship's guard must
        # refuse the ship, and the whole edit must roll back.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_order_lines SET quantity_ordered = quantity_ordered + 10 "
            " WHERE so_line_id = (SELECT MIN(so_line_id) FROM sales_order_lines WHERE so_id = %s)",
            (SO_ID,),
        )
        cur.close()

        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {"status": "SHIPPED", "tracking_number": TRACKING, "carrier": "UPS", "memo": "rush"},
            request_id,
        )
        assert resp.status_code == 422, resp.get_json()
        assert "under-picked" in resp.get_json()["error"]
        # Nothing landed: no events, no fulfillment, status unchanged, and the
        # sidecar memo edit rolled back with the ship.
        assert _query_event_rows(request_id) == []
        assert _fulfillment_rows(SO_ID) == []
        assert _so_row(SO_ID)["status"] == "PICKED"


class TestNonShipEditEmission:
    def test_memo_edit_emits_only_salesorderedit_completed(
        self, client, auth_headers, seed_data
    ):
        request_id = str(uuid.uuid4())
        resp = _put_so(
            client, auth_headers, SO_ID,
            {"memo": "leave at side door"},
            request_id,
        )
        assert resp.status_code == 200, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "salesorderedit.completed"
        payload = rows[0]["payload"]
        changed = {c["field"]: c for c in payload["changes"]}
        assert changed["memo"]["new_value"] == "leave at side door"
        _assert_payload_matches_schema("salesorderedit.completed", 1, payload)
        # A non-ship edit creates no shipment record.
        assert _fulfillment_rows(SO_ID) == []

    def test_no_op_save_emits_nothing(self, client, auth_headers, seed_data):
        # Re-save the same memo twice; the second save changes nothing.
        _put_so(client, auth_headers, SO_ID, {"memo": "x"}, str(uuid.uuid4()))
        request_id = str(uuid.uuid4())
        resp = _put_so(client, auth_headers, SO_ID, {"memo": "x"}, request_id)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json().get("unchanged") is True
        assert _query_event_rows(request_id) == []

    def test_edit_on_already_shipped_order_emits_only_edit_event(
        self, client, auth_headers, seed_data
    ):
        # Ship it first, then correct the memo on the shipped order.
        _advance_so_to_packed(client, auth_headers, SO_NUMBER)
        ship_req = str(uuid.uuid4())
        ship_resp = _put_so(
            client, auth_headers, SO_ID,
            {"status": "SHIPPED", "tracking_number": TRACKING, "carrier": "UPS"},
            ship_req,
        )
        assert ship_resp.status_code == 200, ship_resp.get_json()

        edit_req = str(uuid.uuid4())
        edit_resp = _put_so(
            client, auth_headers, SO_ID,
            {"memo": "corrected after ship"},
            edit_req,
        )
        assert edit_resp.status_code == 200, edit_resp.get_json()

        rows = _query_event_rows(edit_req)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "salesorderedit.completed"
        # No second fulfillment from the post-ship edit.
        assert len(_fulfillment_rows(SO_ID)) == 1
