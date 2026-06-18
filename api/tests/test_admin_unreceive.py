"""HTTP + service-layer tests for the admin unreceive flow.

Two surfaces:

  * GET  /api/admin/purchase-orders/<po_id>/receipts -- receipt
                                                       history for the
                                                       admin Receiving
                                                       modal
  * POST /api/admin/receipts/<receipt_id>/unreceive -- reverse one
                                                      PO receipt

Walks the warehouse pool, decrements the PO line, recomputes PO
status, deletes the item_receipts row, writes ACTION_RECEIVE_CANCEL
audit + emits receipt.cancelled/1. End state must be indistinguishable
from "the receive never happened" plus the audit/event forensic trail.
"""

import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db_test_context import get_raw_connection
from services.events_schema_registry import get_validator


def _admin_headers(client):
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"},
    )
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


def _receive(client, headers, po_id, item_id, qty, bin_id):
    payload = {
        "po_id": po_id,
        "items": [{"item_id": item_id, "quantity": qty, "bin_id": bin_id}],
    }
    resp = client.post("/api/receiving/receive", json=payload, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["receipt_ids"][0]


def _read_inv(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory "
        " WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    if row is None:
        return None
    return {"quantity_on_hand": row[0], "quantity_allocated": row[1]}


def _read_pol(po_line_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_ordered, quantity_received, status "
        "  FROM purchase_order_lines WHERE po_line_id = %s",
        (po_line_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {
        "quantity_ordered": row[0],
        "quantity_received": row[1],
        "status": row[2],
    }


def _read_po_status(po_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM purchase_orders WHERE po_id = %s", (po_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0]


def _po_line_id_for(po_id, item_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT po_line_id FROM purchase_order_lines "
        " WHERE po_id = %s AND item_id = %s",
        (po_id, item_id),
    )
    row = cur.fetchone()
    cur.close()
    return row[0]


def _receipt_exists(receipt_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM item_receipts WHERE receipt_id = %s", (receipt_id,),
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def _audit_rows(po_id, action="RECEIVE_CANCEL"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT details FROM audit_log "
        " WHERE entity_type = 'PO' AND entity_id = %s "
        "   AND action_type = %s "
        " ORDER BY log_id DESC",
        (po_id, action),
    )
    rows = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in cur.fetchall()]
    cur.close()
    return rows


def _event_rows(receipt_id, event_type="receipt.cancelled"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT event_type, payload FROM integration_events "
        " WHERE aggregate_type = 'item_receipt' AND aggregate_id = %s "
        "   AND event_type = %s",
        (receipt_id, event_type),
    )
    rows = [
        {
            "event_type": r[0],
            "payload": r[1] if isinstance(r[1], dict) else json.loads(r[1]),
        }
        for r in cur.fetchall()
    ]
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# GET /admin/purchase-orders/<id>/receipts
# ---------------------------------------------------------------------------


class TestListPoReceipts:
    def test_returns_receipts_for_po(self, client, auth_headers, seed_data):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=3, bin_id=bid)
        admin = _admin_headers(client)
        resp = client.get("/api/admin/purchase-orders/1/receipts", headers=admin)
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        rids = [r["receipt_id"] for r in body["receipts"]]
        assert rid in rids
        sample = next(r for r in body["receipts"] if r["receipt_id"] == rid)
        assert sample["quantity_received"] == 3
        assert sample["item_id"] == 1
        assert sample["bin_id"] == bid
        assert sample["external_id"]

    def test_empty_list_for_unreceived_po(self, client, auth_headers, seed_data):
        admin = _admin_headers(client)
        resp = client.get("/api/admin/purchase-orders/1/receipts", headers=admin)
        assert resp.status_code == 200
        assert resp.get_json()["receipts"] == []


# ---------------------------------------------------------------------------
# POST /admin/receipts/<id>/unreceive -- happy path + invariants
# ---------------------------------------------------------------------------


class TestUnreceiveHappyPath:
    def test_reverses_inventory_and_deletes_receipt(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=4, bin_id=bid)
        assert _read_inv(1, bid)["quantity_on_hand"] == 4
        po_line_id = _po_line_id_for(1, 1)
        assert _read_pol(po_line_id)["quantity_received"] == 4

        admin = _admin_headers(client)
        resp = client.post(
            f"/api/admin/receipts/{rid}/unreceive",
            json={"reason": "double scan"},
            headers=admin,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["quantity_reversed"] == 4
        assert body["bin_decrements"] == [{"bin_id": bid, "quantity": 4}]

        # Inventory back to zero, PO line back to PENDING, receipt gone.
        assert _read_inv(1, bid)["quantity_on_hand"] == 0
        pol = _read_pol(po_line_id)
        assert pol["quantity_received"] == 0
        assert pol["status"] == "PENDING"
        assert not _receipt_exists(rid)

    def test_writes_audit_row_with_admin_source(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=2, bin_id=bid)
        admin = _admin_headers(client)
        client.post(
            f"/api/admin/receipts/{rid}/unreceive",
            json={"reason": "ops typo"},
            headers=admin,
        )
        rows = _audit_rows(1, "RECEIVE_CANCEL")
        assert rows, "audit row missing"
        latest = rows[0]
        assert latest["source"] == "admin_unreceive"
        assert latest["receipt_id"] == rid
        assert latest["quantity_reversed"] == 2
        assert latest["reason"] == "ops typo"
        assert latest["bin_decrements"] == [{"bin_id": bid, "quantity": 2}]

    def test_emits_receipt_cancelled_event(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=2, bin_id=bid)
        admin = _admin_headers(client)
        client.post(
            f"/api/admin/receipts/{rid}/unreceive",
            json={},
            headers=admin,
        )
        events = _event_rows(rid, "receipt.cancelled")
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["lines"][0]["quantity_reversed"] == 2
        # Validates against the registered v1 schema.
        validator = get_validator("receipt.cancelled", 1)
        errors = list(validator.iter_errors(payload))
        assert errors == [], errors


# ---------------------------------------------------------------------------
# Bin-walk: even after putaway moves the goods, unreceive lands
# ---------------------------------------------------------------------------


class TestUnreceiveBinWalk:
    def test_reverses_from_putaway_bin_when_receipt_bin_emptied(
        self, client, auth_headers, seed_data,
    ):
        recv_bid = seed_data["staging_bin_id"]
        # Receive into staging, then move every unit to a pickable bin
        # (simulates the warehouse putting away before noticing the
        # double-scan). Unreceive should still land by walking the
        # warehouse pool.
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=5, bin_id=recv_bid)
        putaway_bid = 4  # A-01-02 in apartment-lab seed (warehouse 1)
        conn = get_raw_connection()
        cur = conn.cursor()
        # Zero every other warehouse-1 bin so the walk has only the
        # putaway bin to choose from. The apartment-lab seed pre-stocks
        # item 1 in bin 3, which would otherwise be picked first.
        cur.execute(
            "UPDATE inventory SET quantity_on_hand = 0 "
            " WHERE item_id = 1 AND bin_id <> %s",
            (putaway_bid,),
        )
        cur.execute(
            "INSERT INTO inventory (item_id, bin_id, warehouse_id, "
            "                       quantity_on_hand, quantity_allocated) "
            "VALUES (1, %s, 1, 5, 0) "
            "ON CONFLICT (item_id, bin_id, lot_number) DO UPDATE "
            "SET quantity_on_hand = 5, quantity_allocated = 0",
            (putaway_bid,),
        )
        cur.close()

        admin = _admin_headers(client)
        resp = client.post(
            f"/api/admin/receipts/{rid}/unreceive", json={}, headers=admin,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        decremented_bins = {d["bin_id"]: d["quantity"] for d in body["bin_decrements"]}
        # Original receipt bin had no stock left, so the walk falls
        # through to the putaway bin and decrements all 5 there.
        assert decremented_bins == {putaway_bid: 5}
        assert _read_inv(1, putaway_bid)["quantity_on_hand"] == 0


# ---------------------------------------------------------------------------
# Guards: insufficient available, not_found
# ---------------------------------------------------------------------------


class TestUnreceiveGuards:
    def test_insufficient_available_returns_409(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=3, bin_id=bid)
        # Drain inventory to simulate the goods having been picked /
        # shipped on a separate SO since the receipt landed. Zero
        # every bin EXCEPT the receipt bin (which keeps 1 unit) so
        # the warehouse-pool total is exactly 1 -- short of the 3
        # required for the unreceive.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE inventory SET quantity_on_hand = 0 "
            " WHERE item_id = 1 AND bin_id <> %s",
            (bid,),
        )
        cur.execute(
            "UPDATE inventory SET quantity_on_hand = 1 "
            " WHERE item_id = 1 AND bin_id = %s",
            (bid,),
        )
        cur.close()

        admin = _admin_headers(client)
        resp = client.post(
            f"/api/admin/receipts/{rid}/unreceive", json={}, headers=admin,
        )
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["kind"] == "insufficient_available"
        assert body["warehouse_available"] == 1
        assert body["receipt_quantity"] == 3

        # Atomic rollback: receipt still exists, PO line untouched.
        assert _receipt_exists(rid)
        po_line_id = _po_line_id_for(1, 1)
        assert _read_pol(po_line_id)["quantity_received"] == 3

    def test_receipt_not_found_returns_404(self, client, auth_headers):
        admin = _admin_headers(client)
        resp = client.post(
            "/api/admin/receipts/99999999/unreceive", json={}, headers=admin,
        )
        assert resp.status_code == 404
        assert resp.get_json()["kind"] == "not_found"

    def test_double_unreceive_returns_404_on_second(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=2, bin_id=bid)
        admin = _admin_headers(client)
        first = client.post(
            f"/api/admin/receipts/{rid}/unreceive", json={}, headers=admin,
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/admin/receipts/{rid}/unreceive", json={}, headers=admin,
        )
        assert second.status_code == 404


# ---------------------------------------------------------------------------
# PO status: re-open RECEIVED PO when a line drops below ordered
# ---------------------------------------------------------------------------


class TestUnreceivePoStatusRecompute:
    def test_reopens_received_po(self, client, auth_headers, seed_data):
        bid = seed_data["staging_bin_id"]
        # Receive every line in full -- PO flips to RECEIVED.
        items = [
            {"item_id": 1, "quantity": 100, "bin_id": bid},
            {"item_id": 2, "quantity": 100, "bin_id": bid},
            {"item_id": 3, "quantity": 100, "bin_id": bid},
            {"item_id": 4, "quantity": 100, "bin_id": bid},
            {"item_id": 5, "quantity": 50, "bin_id": bid},
            {"item_id": 6, "quantity": 20, "bin_id": bid},
            {"item_id": 7, "quantity": 200, "bin_id": bid},
            {"item_id": 8, "quantity": 30, "bin_id": bid},
            {"item_id": 9, "quantity": 40, "bin_id": bid},
            {"item_id": 10, "quantity": 60, "bin_id": bid},
        ]
        resp = client.post(
            "/api/receiving/receive",
            json={"po_id": 1, "items": items},
            headers=auth_headers,
        )
        assert resp.get_json()["po_status"] == "RECEIVED"
        assert _read_po_status(1) == "RECEIVED"

        # Pick any one receipt and undo it -- PO must demote.
        admin = _admin_headers(client)
        first_rid = resp.get_json()["receipt_ids"][0]
        unrecv = client.post(
            f"/api/admin/receipts/{first_rid}/unreceive", json={},
            headers=admin,
        )
        assert unrecv.status_code == 200, unrecv.get_json()
        assert unrecv.get_json()["po_status"] == "PARTIAL"
        assert _read_po_status(1) == "PARTIAL"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestUnreceiveValidation:
    def test_reason_too_long_returns_422(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=1, bin_id=bid)
        admin = _admin_headers(client)
        resp = client.post(
            f"/api/admin/receipts/{rid}/unreceive",
            json={"reason": "x" * 501},
            headers=admin,
        )
        assert resp.status_code == 422

    def test_reason_wrong_type_returns_422(
        self, client, auth_headers, seed_data,
    ):
        bid = seed_data["staging_bin_id"]
        rid = _receive(client, auth_headers, po_id=1, item_id=1, qty=1, bin_id=bid)
        admin = _admin_headers(client)
        resp = client.post(
            f"/api/admin/receipts/{rid}/unreceive",
            json={"reason": 42},
            headers=admin,
        )
        assert resp.status_code == 422
