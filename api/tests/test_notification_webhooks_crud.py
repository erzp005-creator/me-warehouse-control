"""notification_webhooks CRUD + test-send.

Covers the admin surface: create / list / patch / rotate-url /
delete / test-send. URL plaintext is never returned (the list and
create responses expose only a masked host preview).
"""

from unittest.mock import patch

from db_test_context import get_raw_connection


def _query_val(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


_VALID_URL = "https://outlook.office.com/webhook/teamsink"


class TestCreateAndList:
    def test_create_then_list(self, client, auth_headers):
        resp = client.post(
            "/api/admin/notification-webhooks",
            json={
                "warehouse_id": 1,
                "channel_kind": "teams",
                "url": _VALID_URL,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["warehouse_id"] == 1
        assert body["channel_kind"] == "teams"
        assert body["enabled"] is True
        assert "host_preview" in body
        assert "url" not in body  # plaintext URL never returned

        # The default filter is the backorder-opened/fulfillable pair.
        assert body["event_filter"] == ["backorder.opened", "backorder.fulfillable"]

        # List shows the row.
        list_resp = client.get(
            "/api/admin/notification-webhooks?warehouse_id=1",
            headers=auth_headers,
        )
        assert list_resp.status_code == 200
        rows = list_resp.get_json()["notification_webhooks"]
        assert len(rows) == 1
        assert rows[0]["webhook_id"] == body["webhook_id"]
        assert "url" not in rows[0]

    def test_create_with_explicit_event_filter(self, client, auth_headers):
        resp = client.post(
            "/api/admin/notification-webhooks",
            json={
                "warehouse_id": 1,
                "channel_kind": "teams",
                "url": _VALID_URL,
                "event_filter": ["backorder.cancelled"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.get_json()["event_filter"] == ["backorder.cancelled"]

    def test_create_rejects_unknown_channel_kind(self, client, auth_headers):
        resp = client.post(
            "/api/admin/notification-webhooks",
            json={
                "warehouse_id": 1,
                "channel_kind": "slack",
                "url": _VALID_URL,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_rejects_unknown_event_filter_value(self, client, auth_headers):
        resp = client.post(
            "/api/admin/notification-webhooks",
            json={
                "warehouse_id": 1,
                "channel_kind": "teams",
                "url": _VALID_URL,
                "event_filter": ["backorder.opened", "totally.made.up"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_rejects_unknown_warehouse(self, client, auth_headers):
        resp = client.post(
            "/api/admin/notification-webhooks",
            json={
                "warehouse_id": 9999,
                "channel_kind": "teams",
                "url": _VALID_URL,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400


def _create_webhook(client, auth_headers):
    resp = client.post(
        "/api/admin/notification-webhooks",
        json={"warehouse_id": 1, "channel_kind": "teams", "url": _VALID_URL},
        headers=auth_headers,
    )
    return resp.get_json()["webhook_id"]


class TestPatch:
    def test_disable_webhook(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)
        resp = client.patch(
            f"/api/admin/notification-webhooks/{webhook_id}",
            json={"enabled": False},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False

    def test_update_event_filter(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)
        resp = client.patch(
            f"/api/admin/notification-webhooks/{webhook_id}",
            json={"event_filter": ["backorder.cancelled"]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["event_filter"] == ["backorder.cancelled"]

    def test_patch_with_no_fields_rejected(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)
        resp = client.patch(
            f"/api/admin/notification-webhooks/{webhook_id}",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestRotateUrl:
    def test_rotate_changes_ciphertext(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)
        before = _query_val(
            "SELECT url FROM notification_webhooks WHERE webhook_id = %s",
            (webhook_id,),
        )

        resp = client.post(
            f"/api/admin/notification-webhooks/{webhook_id}/rotate-url",
            json={"url": "https://outlook.office.com/webhook/rotated"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        after = _query_val(
            "SELECT url FROM notification_webhooks WHERE webhook_id = %s",
            (webhook_id,),
        )
        assert before != after  # ciphertext changed
        assert "rotated" not in after  # value is encrypted, not plaintext


class TestDelete:
    def test_delete_returns_200_and_removes_row(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)
        resp = client.delete(
            f"/api/admin/notification-webhooks/{webhook_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        gone = _query_val(
            "SELECT COUNT(*) FROM notification_webhooks WHERE webhook_id = %s",
            (webhook_id,),
        )
        assert gone == 0

    def test_delete_nonexistent_returns_404(self, client, auth_headers):
        resp = client.delete(
            "/api/admin/notification-webhooks/999999",
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestTestSend:
    def test_test_send_happy_path_invokes_teams(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)

        class _Resp:
            status_code = 200
            text = "1"

        with patch(
            "services.webhook_dispatcher.teams_adapter.requests.post",
            return_value=_Resp(),
        ) as mock_post:
            resp = client.post(
                f"/api/admin/notification-webhooks/{webhook_id}/test-send",
                headers=auth_headers,
            )

        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["delivered"] is True
        assert body["status_code"] == 200
        # Card was sent with our valid URL (the decrypt round-trip
        # worked and the SSRF guard let it through).
        assert mock_post.call_count == 1
        sent_url, sent_kwargs = mock_post.call_args.args, mock_post.call_args.kwargs
        assert sent_url[0] == _VALID_URL
        assert sent_kwargs["json"]["@type"] == "MessageCard"

    def test_test_send_returns_outcome_on_teams_rejection(self, client, auth_headers):
        webhook_id = _create_webhook(client, auth_headers)

        class _Resp:
            status_code = 410
            text = "gone"

        with patch(
            "services.webhook_dispatcher.teams_adapter.requests.post",
            return_value=_Resp(),
        ):
            resp = client.post(
                f"/api/admin/notification-webhooks/{webhook_id}/test-send",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["delivered"] is False
        assert body["status_code"] == 410
        assert body["error_kind"] == "teams_rejected"

    def test_test_send_404_for_unknown_webhook(self, client, auth_headers):
        resp = client.post(
            "/api/admin/notification-webhooks/999999/test-send",
            headers=auth_headers,
        )
        assert resp.status_code == 404
