"""PO line-edit reopen + purchaseorderedit.completed outbound event.

Covers stikman28/sentry-wms#55: bumping quantity_ordered on a RECEIVED
line must reopen it (RECEIVED -> PARTIAL) so the over-receipt becomes
receivable, and every admin PO edit emits purchaseorderedit.completed so
the ERP can reverse-sync the change.

Fixture: seed PO-2026-005 (po_id 5) is a single line -- item 20
(TST-020), quantity_ordered 100 -- so the PO header status tracks the
one line directly. Warehouse 1, staging bin 1 (matches the other admin
PO tests).
"""

import json

from db_test_context import get_raw_connection
from services.events_schema_registry import get_validator

PO_ID = 5
SKU = "TST-020"
BIN = 1


def _receive(client, headers, item_id, qty, po_id=PO_ID, bin_id=BIN):
    return client.post(
        "/api/receiving/receive",
        json={
            "po_id": po_id,
            "items": [{"item_id": item_id, "quantity": qty, "bin_id": bin_id}],
        },
        headers=headers,
    )


def _detail(client, headers, po_id=PO_ID):
    return client.get(
        f"/api/admin/purchase-orders/{po_id}", headers=headers,
    ).get_json()


def _line(detail, idx=0):
    return detail["lines"][idx]


def _po_edit_events(po_id=PO_ID):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT payload FROM integration_events "
        " WHERE aggregate_type = 'purchase_order' AND aggregate_id = %s "
        "   AND event_type = 'purchaseorderedit.completed' "
        " ORDER BY event_id",
        (po_id,),
    )
    rows = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in cur.fetchall()]
    cur.close()
    return rows


def _assert_valid(payload):
    errors = list(get_validator("purchaseorderedit.completed", 1).iter_errors(payload))
    assert errors == [], errors


class TestOverReceiptReopen:
    def test_qty_bump_reopens_received_line_and_makes_overreceipt_receivable(
        self, client, auth_headers,
    ):
        # Receive the full order -> line + PO go RECEIVED.
        assert _receive(client, auth_headers, item_id=20, qty=100).status_code == 200
        before = _detail(client, auth_headers)
        line = _line(before)
        assert line["status"] == "RECEIVED"
        assert before["purchase_order"]["status"] == "RECEIVED"

        # The extra unit is hard-blocked today: receiving accepts only
        # OPEN/PARTIAL POs, so a fully-RECEIVED PO refuses any further
        # receipt -- the over-receipt is stuck until the PO reopens.
        blocked = _receive(client, auth_headers, item_id=20, qty=1)
        assert blocked.status_code == 400
        assert "cannot receive" in blocked.get_json()["error"]

        # Bump quantity_ordered: the line reopens to PARTIAL (and the PO
        # with it) so the remaining unit becomes a normal in-order receipt.
        resp = client.patch(
            f"/api/admin/purchase-orders/{PO_ID}/lines/{line['po_line_id']}",
            json={"quantity_ordered": 101},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["quantity_ordered"] == 101
        assert body["line_status"] == "PARTIAL"
        assert body["po_status"] == "PARTIAL"

        # The over-receipt is now receivable without the global override.
        assert _receive(client, auth_headers, item_id=20, qty=1).status_code == 200
        after = _line(_detail(client, auth_headers))
        assert after["quantity_received"] == 101
        assert after["status"] == "RECEIVED"
        assert _detail(client, auth_headers)["purchase_order"]["status"] == "RECEIVED"

    def test_qty_below_received_still_rejected(self, client, auth_headers):
        # The reopen path must not weaken the non-negative-variance guard.
        _receive(client, auth_headers, item_id=20, qty=40)
        line = _line(_detail(client, auth_headers))
        resp = client.patch(
            f"/api/admin/purchase-orders/{PO_ID}/lines/{line['po_line_id']}",
            json={"quantity_ordered": 30},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "less than" in resp.get_json()["error"]


class TestPurchaseOrderEditEvent:
    def test_line_qty_edit_emits_sku_qualified_event(self, client, auth_headers):
        line = _line(_detail(client, auth_headers))
        resp = client.patch(
            f"/api/admin/purchase-orders/{PO_ID}/lines/{line['po_line_id']}",
            json={"quantity_ordered": 120},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        events = _po_edit_events()
        assert len(events) == 1
        payload = events[0]
        assert payload["po_number"] == "PO-2026-005"
        assert payload["status"] == "OPEN"  # nothing received yet
        assert payload["changes"] == [{
            "field": f"line[{SKU}].quantity_ordered",
            "old_value": "100",
            "new_value": "120",
        }]
        assert payload["purchase_order_external_id"]  # uuid fallback, non-empty
        _assert_valid(payload)

    def test_no_event_when_quantity_unchanged(self, client, auth_headers):
        line = _line(_detail(client, auth_headers))
        resp = client.patch(
            f"/api/admin/purchase-orders/{PO_ID}/lines/{line['po_line_id']}",
            json={"quantity_ordered": line["quantity_ordered"]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert _po_edit_events() == []

    def test_header_edit_emits_event(self, client, auth_headers):
        resp = client.put(
            f"/api/admin/purchase-orders/{PO_ID}",
            json={"notes": "rush -- vendor backorder cleared"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        events = _po_edit_events()
        assert len(events) == 1
        payload = events[0]
        assert {
            "field": "notes",
            "old_value": None,
            "new_value": "rush -- vendor backorder cleared",
        } in payload["changes"]
        _assert_valid(payload)

    def test_add_line_reopens_received_po_and_emits(self, client, auth_headers):
        # Fully receive the PO, then add a fresh unreceived line.
        _receive(client, auth_headers, item_id=20, qty=100)
        assert _detail(client, auth_headers)["purchase_order"]["status"] == "RECEIVED"

        resp = client.post(
            f"/api/admin/purchase-orders/{PO_ID}/lines",
            json={"item_id": 1, "quantity_ordered": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["po_status"] == "PARTIAL"

        events = _po_edit_events()
        assert len(events) == 1
        assert events[0]["changes"] == [{
            "field": "line[TST-001]",
            "old_value": None,
            "new_value": "5",
        }]
        assert events[0]["status"] == "PARTIAL"
        _assert_valid(events[0])

    def test_remove_line_emits_event(self, client, auth_headers):
        add = client.post(
            f"/api/admin/purchase-orders/{PO_ID}/lines",
            json={"item_id": 1, "quantity_ordered": 7},
            headers=auth_headers,
        )
        po_line_id = add.get_json()["po_line_id"]
        resp = client.delete(
            f"/api/admin/purchase-orders/{PO_ID}/lines/{po_line_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Two edits this PO: the add then the remove. Assert the latest.
        events = _po_edit_events()
        assert len(events) == 2
        assert events[-1]["changes"] == [{
            "field": "line[TST-001]",
            "old_value": "7",
            "new_value": None,
        }]
        _assert_valid(events[-1])
