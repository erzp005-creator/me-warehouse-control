"""Fire-and-forget Teams notifier.

Tests for services/webhook_dispatcher/backorder_notifier.py. The
helper spawns a daemon thread that opens its own psycopg2
connection. Because pytest's conftest uses a SAVEPOINT-rolled-back
transaction for isolation, rows the test inserts via the API are
NOT visible to a fresh outside-the-transaction connection. So these
unit tests mock _load_matching_webhooks to bypass real DB
round-trip; the integration test
(test_partial_fulfill_fires_notification_for_configured_webhook)
asserts the wrapper-is-invoked contract at the call site without
exercising the thread internals.
"""

import os
from unittest.mock import patch

from db_test_context import get_raw_connection

from services.webhook_dispatcher import backorder_notifier
from services.webhook_dispatcher.backorder_notifier import (
    dispatch_backorder_notification,
)
from services.webhook_dispatcher.teams_adapter import SendOutcome


_OPENED_PAYLOAD = {
    "backorder_so_external_id": "11111111-2222-3333-4444-555555555555",
    "backorder_so_number": "SO-NOTIFIER-BO",
    "parent_so_external_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "parent_so_number": "SO-NOTIFIER",
    "warehouse_id": 1,
    "customer_name": "Test",
    "items": [{"item_external_id": "ext-1", "sku": "SKU-X", "qty": 2}],
    "opened_by_user_external_id": "user-ext",
    "opened_at": "2026-05-19T12:00:00Z",
}


def _encrypted_url(plaintext="https://outlook.office.com/webhook/x"):
    """Build a Fernet-encrypted URL using the same key the notifier
    decrypts with; lets the unit tests round-trip through the real
    crypto path without depending on a DB row."""
    from services.credential_vault import encrypt_string
    return encrypt_string(plaintext)


def _fake_webhook(webhook_id=1, channel_kind="teams", url=None, event_filter=None):
    return {
        "webhook_id":   webhook_id,
        "warehouse_id": 1,
        "channel_kind": channel_kind,
        "url":          url or _encrypted_url(),
        "event_filter": event_filter or ["backorder.opened", "backorder.fulfillable"],
        "enabled":      True,
        "secret":       None,
        "external_id":  "00000000-0000-0000-0000-000000000000",
    }


class TestDispatch:
    def test_dispatch_fires_one_post_per_loaded_webhook(self):
        webhooks = [_fake_webhook(1), _fake_webhook(2)]
        with patch.object(
            backorder_notifier, "_load_matching_webhooks", return_value=webhooks
        ), patch.object(
            backorder_notifier, "send_to_teams",
            return_value=SendOutcome(delivered=True, status_code=200),
        ) as mock_send:
            thread = dispatch_backorder_notification(
                event_type="backorder.opened",
                payload=_OPENED_PAYLOAD,
                warehouse_id=1,
                database_url=os.environ["TEST_DATABASE_URL"],
            )
            thread.join(timeout=5)
        assert mock_send.call_count == 2

    def test_no_webhooks_skips_send(self):
        with patch.object(
            backorder_notifier, "_load_matching_webhooks", return_value=[]
        ), patch.object(
            backorder_notifier, "send_to_teams"
        ) as mock_send:
            thread = dispatch_backorder_notification(
                event_type="backorder.opened",
                payload=_OPENED_PAYLOAD,
                warehouse_id=1,
                database_url=os.environ["TEST_DATABASE_URL"],
            )
            thread.join(timeout=5)
        assert mock_send.call_count == 0

    def test_per_webhook_failure_isolation(self):
        webhooks = [_fake_webhook(1), _fake_webhook(2)]
        call_state = {"n": 0}

        def _fake_send(url, card):
            call_state["n"] += 1
            if call_state["n"] == 1:
                raise RuntimeError("simulated failure")
            return SendOutcome(delivered=True, status_code=200)

        with patch.object(
            backorder_notifier, "_load_matching_webhooks", return_value=webhooks
        ), patch.object(
            backorder_notifier, "send_to_teams", side_effect=_fake_send
        ):
            thread = dispatch_backorder_notification(
                event_type="backorder.opened",
                payload=_OPENED_PAYLOAD,
                warehouse_id=1,
                database_url=os.environ["TEST_DATABASE_URL"],
            )
            thread.join(timeout=5)
        # Both webhooks attempted despite the first raising.
        assert call_state["n"] == 2

    def test_non_teams_channel_skipped(self):
        webhooks = [_fake_webhook(1, channel_kind="slack")]
        with patch.object(
            backorder_notifier, "_load_matching_webhooks", return_value=webhooks
        ), patch.object(
            backorder_notifier, "send_to_teams"
        ) as mock_send:
            thread = dispatch_backorder_notification(
                event_type="backorder.opened",
                payload=_OPENED_PAYLOAD,
                warehouse_id=1,
                database_url=os.environ["TEST_DATABASE_URL"],
            )
            thread.join(timeout=5)
        assert mock_send.call_count == 0

    def test_decrypt_failure_skips_webhook_without_crashing(self):
        # url field is gibberish so decrypt fails. Other webhooks in
        # the list should still attempt.
        bad = _fake_webhook(1, url="not-real-ciphertext")
        good = _fake_webhook(2)
        with patch.object(
            backorder_notifier, "_load_matching_webhooks", return_value=[bad, good]
        ), patch.object(
            backorder_notifier, "send_to_teams",
            return_value=SendOutcome(delivered=True, status_code=200),
        ) as mock_send:
            thread = dispatch_backorder_notification(
                event_type="backorder.opened",
                payload=_OPENED_PAYLOAD,
                warehouse_id=1,
                database_url=os.environ["TEST_DATABASE_URL"],
            )
            thread.join(timeout=5)
        # Only the good one made it to the wire.
        assert mock_send.call_count == 1

    def test_empty_database_url_skips_silently(self):
        with patch.object(
            backorder_notifier, "send_to_teams"
        ) as mock_send:
            thread = dispatch_backorder_notification(
                event_type="backorder.opened",
                payload=_OPENED_PAYLOAD,
                warehouse_id=1,
                database_url="",
            )
            thread.join(timeout=5)
        assert mock_send.call_count == 0


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


class TestCallSiteWiring:
    """Assert the call-site wrapper is invoked from each emit path
    with the right event_type and warehouse_id. Wrapping
    dispatch_backorder_notification at the route module captures
    the call BEFORE the daemon thread spawns, so these tests do
    not depend on cross-connection visibility."""

    def test_partial_fulfill_invokes_dispatcher(self, client, auth_headers):
        sol_id = _query_val(
            "SELECT so_line_id FROM sales_order_lines WHERE so_id = 1 LIMIT 1"
        )
        with patch(
            "routes.admin.admin_orders.dispatch_backorder_notification"
        ) as mock_dispatch:
            resp = client.post(
                "/api/admin/sales-orders/1/partial-fulfill",
                json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.get_json()
        assert mock_dispatch.call_count == 1
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["event_type"] == "backorder.opened"
        assert kwargs["warehouse_id"] == 1
        assert kwargs["payload"]["backorder_so_number"] == "SO-2026-001-BO"

    def test_cancel_backorder_invokes_dispatcher(self, client, auth_headers):
        sol_id = _query_val(
            "SELECT so_line_id FROM sales_order_lines WHERE so_id = 1 LIMIT 1"
        )
        bo_resp = client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )
        bo_so_id = bo_resp.get_json()["backorder_so"]["so_id"]

        with patch(
            "routes.admin.admin_orders.dispatch_backorder_notification"
        ) as mock_dispatch:
            resp = client.post(
                f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
                json={"cancellation_reason": "found"},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.get_json()
        # Exactly one backorder.cancelled dispatch for the BO.
        assert mock_dispatch.call_count == 1
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["event_type"] == "backorder.cancelled"
        assert kwargs["warehouse_id"] == 1
        assert kwargs["payload"]["cancellation_reason"] == "found"

    def test_parent_cancel_cascade_invokes_dispatcher_for_child(self, client, auth_headers):
        sol_id = _query_val(
            "SELECT so_line_id FROM sales_order_lines WHERE so_id = 1 LIMIT 1"
        )
        client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 1}]},
            headers=auth_headers,
        )

        with patch(
            "routes.admin.admin_orders.dispatch_backorder_notification"
        ) as mock_dispatch:
            resp = client.post(
                "/api/admin/sales-orders/1/cancel",
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.get_json()
        # One cascade-fired backorder.cancelled.
        assert mock_dispatch.call_count == 1
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["event_type"] == "backorder.cancelled"
        assert kwargs["payload"]["cancellation_reason"] == "parent_cancelled"

    def test_receipt_hook_invokes_dispatcher_when_bo_flips(self, client, auth_headers):
        # Reproduce the happy path from test_receiving_backorder_match:
        # zero on-hand, partial-fulfill, then receive enough to satisfy.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE inventory SET quantity_on_hand = 0, quantity_allocated = 0 "
            " WHERE item_id = 1"
        )
        cur.close()
        sol_id = _query_val(
            "SELECT so_line_id FROM sales_order_lines WHERE so_id = 1 LIMIT 1"
        )
        client.post(
            "/api/admin/sales-orders/1/partial-fulfill",
            json={"lines": [{"so_line_id": sol_id, "short_qty": 2}]},
            headers=auth_headers,
        )

        with patch(
            "routes.receiving.dispatch_backorder_notification"
        ) as mock_dispatch:
            resp = client.post(
                "/api/receiving/receive",
                json={
                    "po_id": 1,
                    "items": [{"item_id": 1, "quantity": 5, "bin_id": 3}],
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.get_json()
        # One backorder.fulfillable card fired.
        assert mock_dispatch.call_count == 1
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["event_type"] == "backorder.fulfillable"
        assert kwargs["warehouse_id"] == 1
