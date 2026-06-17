"""Teams adapter card builder + send_to_teams.

build_adaptive_card is a pure function so this file is mostly assertions
on the JSON shape. send_to_teams is tested against the SSRF guard
(network-free) and against mocked requests.post for the happy and
4xx paths.
"""

from unittest.mock import patch

import pytest

from services.webhook_dispatcher import teams_adapter
from services.webhook_dispatcher.teams_adapter import (
    SendOutcome,
    build_adaptive_card,
    send_to_teams,
)


_OPENED_PAYLOAD = {
    "backorder_so_external_id": "11111111-2222-3333-4444-555555555555",
    "backorder_so_number": "SO-9001-BO",
    "parent_so_external_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "parent_so_number": "SO-9001",
    "warehouse_id": 1,
    "customer_name": "Jane Doe",
    "items": [
        {"item_external_id": "ext-1", "sku": "SKU-A", "qty": 3},
        {"item_external_id": "ext-2", "sku": "SKU-B", "qty": 1},
    ],
    "opened_by_user_external_id": "user-ext",
    "opened_at": "2026-05-19T12:00:00Z",
}

_FULFILLABLE_PAYLOAD = {
    **_OPENED_PAYLOAD,
    "days_waiting": 7,
    "fulfillable_at": "2026-05-26T12:00:00Z",
}

_CANCELLED_PAYLOAD = {
    "backorder_so_external_id": "id",
    "backorder_so_number": "SO-9001-BO",
    "parent_so_number": "SO-9001",
    "warehouse_id": 1,
    "cancellation_reason": "customer_asked",
    "cancelled_by_user_external_id": "user-ext",
    "cancelled_at": "2026-05-20T08:00:00Z",
}


class TestCardBuilder:
    def test_opened_card_shape(self):
        card = build_adaptive_card("backorder.opened", _OPENED_PAYLOAD)
        assert card["@type"] == "MessageCard"
        assert "SO-9001" in card["title"]
        assert "Jane Doe" in card["text"]
        assert "SKU-A" in card["text"]
        assert "SKU-B" in card["text"]
        # One Open-in-Sentry action, linking with focus=<bo>
        actions = card["potentialAction"]
        assert len(actions) == 1
        uri = actions[0]["targets"][0]["uri"]
        assert "focus=SO-9001-BO" in uri

    def test_fulfillable_card_shape(self):
        card = build_adaptive_card("backorder.fulfillable", _FULFILLABLE_PAYLOAD)
        assert "SO-9001-BO" in card["title"]
        assert "Days waiting" in card["text"]
        assert "7" in card["text"]

    def test_cancelled_card_shape(self):
        card = build_adaptive_card("backorder.cancelled", _CANCELLED_PAYLOAD)
        assert "SO-9001-BO" in card["title"]
        assert "customer_asked" in card["text"]

    def test_unknown_event_type_raises(self):
        with pytest.raises(ValueError):
            build_adaptive_card("not.an.event", {})

    def test_items_summary_truncates_after_five(self):
        payload = dict(_OPENED_PAYLOAD)
        payload["items"] = [
            {"item_external_id": f"e{i}", "sku": f"SKU-{i}", "qty": i}
            for i in range(1, 9)
        ]
        card = build_adaptive_card("backorder.opened", payload)
        # First 5 items + a "(+3 more)" line are present.
        assert "SKU-1" in card["text"]
        assert "SKU-5" in card["text"]
        assert "(+3 more)" in card["text"]
        # Items past the truncation point are NOT inlined.
        assert "SKU-7" not in card["text"]


class TestSendToTeams:
    def test_send_succeeds_on_2xx(self):
        class _Resp:
            status_code = 200
            text = "1"

        with patch.object(teams_adapter, "requests") as mock_requests:
            mock_requests.post.return_value = _Resp()
            # requests.exceptions stays a real module so the except
            # clauses bind without blowing up.
            import requests as _real_requests
            mock_requests.exceptions = _real_requests.exceptions

            outcome = send_to_teams(
                "https://outlook.office.com/webhook/x",
                {"@type": "MessageCard"},
            )
        assert outcome.delivered is True
        assert outcome.status_code == 200
        assert outcome.error_kind is None

    def test_send_returns_teams_rejected_on_4xx(self):
        class _Resp:
            status_code = 410
            text = "channel deleted"

        with patch.object(teams_adapter, "requests") as mock_requests:
            mock_requests.post.return_value = _Resp()
            import requests as _real_requests
            mock_requests.exceptions = _real_requests.exceptions

            outcome = send_to_teams(
                "https://outlook.office.com/webhook/x",
                {"@type": "MessageCard"},
            )
        assert outcome.delivered is False
        assert outcome.status_code == 410
        assert outcome.error_kind == "teams_rejected"

    def test_ssrf_guard_rejects_private_address(self, monkeypatch):
        # test_webhook_dispatcher_http_client.py:29 sets
        # SENTRY_ALLOW_INTERNAL_WEBHOOKS=true at module import time
        # (permanent leak into the test session env). Force the var
        # off so this assertion exercises the default-secure path
        # regardless of test ordering.
        monkeypatch.delenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", raising=False)

        # 169.254.169.254 is the AWS IMDS address; ssrf_guard MUST reject.
        outcome = send_to_teams(
            "http://169.254.169.254/latest/meta-data/",
            {"@type": "MessageCard"},
        )
        assert outcome.delivered is False
        assert outcome.error_kind == "ssrf_rejected"
        assert outcome.status_code is None

    def test_network_error_captured_as_outcome(self):
        import requests as _real_requests

        with patch.object(teams_adapter, "requests") as mock_requests:
            mock_requests.exceptions = _real_requests.exceptions
            mock_requests.post.side_effect = _real_requests.exceptions.ConnectionError(
                "boom"
            )
            outcome = send_to_teams(
                "https://outlook.office.com/webhook/x",
                {"@type": "MessageCard"},
            )
        assert outcome.delivered is False
        assert outcome.error_kind == "network_error"
        assert "boom" in outcome.error_detail
