"""Tests for the dispatcher self-monitor + Teams health alerting.

Coverage:

  * build_dispatcher_alert_card renders each alert type with its
    numbers + theme; unknown type raises.
  * ALERT_EVENT_TYPES stays in sync with the schema allowlist.
  * Each health check fires when its condition is met and the fan-out
    sends a card to the subscribed Teams webhook.
  * Opt-in: with no webhook subscribed, the monitor does no work and
    sends nothing even when a condition is live.
  * Anti-spam: a persistent condition sends once, then again only
    after the cooldown; a cleared condition re-arms.
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

from services.credential_vault import encrypt_string
from services.webhook_dispatcher import dispatcher_health as health_module
from services.webhook_dispatcher import teams_adapter

from tests.test_webhook_dispatcher_dispatch import (  # noqa: E402
    _conn,
    _emit_event,
    _make_subscription,
    _wait_for_visible,
)
from tests.test_webhook_dispatcher_shutdown import _seed_in_flight  # noqa: E402


# ---------------------------------------------------------------------
# Test fan-out sink + DB helpers
# ---------------------------------------------------------------------


class _RecordingSend:
    """Injectable send_fn replacement. Records (url, card) per call and
    reports delivered=True so the fan-out logs a success."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, card):
        self.calls.append((url, card))
        return True

    @property
    def alert_titles(self):
        return [c.get("title") for _u, c in self.calls]


def _make_webhook(event_filter, enabled=True, warehouse_id=1):
    """Insert a teams notification_webhook subscribed to ``event_filter``.
    Returns (webhook_id, cleanup_fn). URL is Fernet-encrypted like the
    admin CRUD does so the fan-out's decrypt path is exercised."""
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO notification_webhooks
                (warehouse_id, channel_kind, url, event_filter, enabled,
                 created_by, external_id)
            VALUES (%s, 'teams', %s, %s, %s, 'pytest', %s)
            RETURNING webhook_id
            """,
            (
                warehouse_id,
                encrypt_string("https://example.test/teams-hook"),
                event_filter,
                enabled,
                str(uuid.uuid4()),
            ),
        )
        webhook_id = cur.fetchone()[0]
    finally:
        conn.close()

    def cleanup():
        c = _conn()
        c.autocommit = True
        try:
            c.cursor().execute(
                "DELETE FROM notification_webhooks WHERE webhook_id = %s",
                (webhook_id,),
            )
        finally:
            c.close()

    return webhook_id, cleanup


def _seed_dlq(sub_id, event_id, error_kind="4xx"):
    conn = _conn()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO webhook_deliveries
                (subscription_id, event_id, attempt_number, status,
                 scheduled_at, completed_at, error_kind, secret_generation)
            VALUES (%s, %s, 1, 'dlq', NOW(), NOW(), %s, 1)
            RETURNING delivery_id
            """,
            (sub_id, event_id, error_kind),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _set_paused(sub_id, reason="dlq_ceiling"):
    conn = _conn()
    conn.autocommit = True
    try:
        conn.cursor().execute(
            "UPDATE webhook_subscriptions SET status='paused', pause_reason=%s "
            "WHERE subscription_id=%s",
            (reason, sub_id),
        )
    finally:
        conn.close()


def _monitor(send, **kw):
    """HealthMonitor wired to the recording sink, with low thresholds so
    a single seeded row trips the check."""
    defaults = dict(
        stall_age_s=1,
        dlq_threshold=1,
        lag_threshold=1,
        cooldown_s=1800.0,
        send_fn=send,
    )
    defaults.update(kw)
    return health_module.HealthMonitor(os.environ["DATABASE_URL"], **defaults)


def _cleanup_event(event_ids):
    if not event_ids:
        return
    c = _conn()
    c.autocommit = True
    try:
        c.cursor().execute(
            "DELETE FROM integration_events WHERE event_id = ANY(%s)",
            (list(event_ids),),
        )
    finally:
        c.close()


# ---------------------------------------------------------------------
# Card builder (pure)
# ---------------------------------------------------------------------


class TestBuildDispatcherAlertCard:
    def test_delivery_stalled_card_carries_numbers(self):
        card = teams_adapter.build_dispatcher_alert_card(
            "dispatcher.delivery_stalled",
            {"subscription_id": "sub-1", "stuck_count": 3, "oldest_age_s": 412},
        )
        assert card["@type"] == "MessageCard"
        assert card["title"] == "Webhook delivery stalled"
        assert "sub-1" in card["text"]
        assert "3" in card["text"]
        assert "412" in card["text"]

    def test_dlq_growth_card_lists_error_kinds(self):
        card = teams_adapter.build_dispatcher_alert_card(
            "dispatcher.dlq_growth",
            {"subscription_id": "sub-2", "dlq_count": 7,
             "recent_error_kinds": "4xx, ssrf_rejected"},
        )
        assert "7" in card["text"]
        assert "ssrf_rejected" in card["text"]

    def test_paused_card_shows_reason(self):
        card = teams_adapter.build_dispatcher_alert_card(
            "dispatcher.subscription_paused",
            {"subscription_id": "sub-3", "pause_reason": "dlq_ceiling"},
        )
        assert "dlq_ceiling" in card["text"]

    def test_unknown_alert_type_raises(self):
        with pytest.raises(ValueError, match="unsupported dispatcher alert_type"):
            teams_adapter.build_dispatcher_alert_card("dispatcher.bogus", {})


class TestAlertTypeContract:
    def test_alert_types_match_schema_allowlist(self):
        """dispatcher_health is the producer; the schema mirrors it for
        admin-layer validation. They must not drift."""
        from schemas.notification_webhooks import _DISPATCHER_ALERT_EVENT_TYPES
        assert set(health_module.ALERT_EVENT_TYPES) == set(
            _DISPATCHER_ALERT_EVENT_TYPES
        )


# ---------------------------------------------------------------------
# Health checks + fan-out (DB-backed)
# ---------------------------------------------------------------------


class TestDeliveryStalledAlert:
    def test_stuck_in_flight_fires_and_sends(self):
        sub_id, _, cleanup = _make_subscription()
        wid, wcleanup = _make_webhook(["dispatcher.delivery_stalled"])
        emitted = []
        try:
            e1 = _emit_event()
            emitted.append(e1)
            _seed_in_flight(sub_id, e1, attempted_offset_s=120)  # > stall_age_s=1

            send = _RecordingSend()
            sent = _monitor(send).run_checks()

            assert ("dispatcher.delivery_stalled", sub_id) in sent
            assert len(send.calls) == 1
            assert send.alert_titles == ["Webhook delivery stalled"]
        finally:
            wcleanup()
            cleanup()
            _cleanup_event(emitted)


class TestDlqGrowthAlert:
    def test_dlq_rows_fire_and_send_with_count(self):
        sub_id, _, cleanup = _make_subscription()
        wid, wcleanup = _make_webhook(["dispatcher.dlq_growth"])
        emitted = []
        try:
            e1 = _emit_event()
            emitted.append(e1)
            _seed_dlq(sub_id, e1, error_kind="4xx")
            _seed_dlq(sub_id, e1, error_kind="ssrf_rejected")

            send = _RecordingSend()
            sent = _monitor(send).run_checks()

            assert ("dispatcher.dlq_growth", sub_id) in sent
            # The card body carries the count (2) and both error kinds.
            _url, card = send.calls[0]
            assert "2" in card["text"]
            assert "ssrf_rejected" in card["text"]
        finally:
            wcleanup()
            cleanup()
            _cleanup_event(emitted)


class TestSubscriptionPausedAlert:
    def test_paused_subscription_fires(self):
        sub_id, _, cleanup = _make_subscription()
        wid, wcleanup = _make_webhook(["dispatcher.subscription_paused"])
        try:
            _set_paused(sub_id, reason="pending_ceiling")

            send = _RecordingSend()
            sent = _monitor(send).run_checks()

            assert ("dispatcher.subscription_paused", sub_id) in sent
            _url, card = send.calls[0]
            assert "pending_ceiling" in card["text"]
        finally:
            wcleanup()
            cleanup()


class TestSubscriptionLaggingAlert:
    def test_backlog_past_cursor_fires(self):
        sub_id, _, cleanup = _make_subscription()
        wid, wcleanup = _make_webhook(["dispatcher.subscription_lagging"])
        emitted = []
        try:
            # Subscription cursor stays at 0; two visible events past it
            # are a backlog of >= lag_threshold(1).
            e1 = _emit_event()
            e2 = _emit_event()
            emitted.extend([e1, e2])
            _wait_for_visible(e1)
            _wait_for_visible(e2)

            send = _RecordingSend()
            sent = _monitor(send).run_checks()

            assert ("dispatcher.subscription_lagging", sub_id) in sent
        finally:
            wcleanup()
            cleanup()
            _cleanup_event(emitted)


class TestOptIn:
    def test_no_subscriber_means_no_work_no_send(self):
        """With no webhook subscribed to the alert type, a live stuck
        delivery produces NO send -- the monitor is opt-in."""
        sub_id, _, cleanup = _make_subscription()
        emitted = []
        try:
            e1 = _emit_event()
            emitted.append(e1)
            _seed_in_flight(sub_id, e1, attempted_offset_s=120)

            send = _RecordingSend()
            sent = _monitor(send).run_checks()

            assert sent == []
            assert send.calls == []
        finally:
            cleanup()
            _cleanup_event(emitted)


class TestAntiSpam:
    def test_persistent_condition_dedupes_then_repeats_after_cooldown(self):
        sub_id, _, cleanup = _make_subscription()
        wid, wcleanup = _make_webhook(["dispatcher.subscription_paused"])
        try:
            _set_paused(sub_id, reason="dlq_ceiling")
            send = _RecordingSend()
            monitor = _monitor(send, cooldown_s=300.0)

            # t=0: first sight -> send.
            sent1 = monitor.run_checks(_now=0.0)
            # t=10s: still paused, inside cooldown -> no send.
            sent2 = monitor.run_checks(_now=10.0)
            # t=400s: past the 300s cooldown -> send again.
            sent3 = monitor.run_checks(_now=400.0)

            assert len(sent1) == 1
            assert sent2 == []
            assert len(sent3) == 1
            assert len(send.calls) == 2
        finally:
            wcleanup()
            cleanup()

    def test_cleared_condition_rearms(self):
        sub_id, _, cleanup = _make_subscription()
        wid, wcleanup = _make_webhook(["dispatcher.subscription_paused"])
        try:
            _set_paused(sub_id, reason="dlq_ceiling")
            send = _RecordingSend()
            monitor = _monitor(send, cooldown_s=10_000.0)

            sent1 = monitor.run_checks(_now=0.0)
            assert len(sent1) == 1

            # Resolve the condition (operator resumed the subscription).
            conn = _conn()
            conn.autocommit = True
            conn.cursor().execute(
                "UPDATE webhook_subscriptions SET status='active', "
                "pause_reason=NULL WHERE subscription_id=%s",
                (sub_id,),
            )
            conn.close()
            sent2 = monitor.run_checks(_now=1.0)  # within cooldown, but cleared
            assert sent2 == []

            # It pauses again -> must alert immediately despite the long
            # cooldown, because the prior alert re-armed when it cleared.
            _set_paused(sub_id, reason="dlq_ceiling")
            sent3 = monitor.run_checks(_now=2.0)
            assert len(sent3) == 1
            assert len(send.calls) == 2
        finally:
            wcleanup()
            cleanup()
