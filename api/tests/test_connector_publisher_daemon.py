"""Daemon-orchestration contract for the connector-publisher: the debounce gate,
the backlog drain loop, the healthcheck staleness check, and env validation. The
per-batch publish itself is covered in test_connector_publisher_publish; here we
exercise the loop logic around it with an injected http_post (no network).
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text as sa_text  # noqa: E402

import models.database as md  # noqa: E402
from db_test_context import get_raw_connection  # noqa: E402
from services.channel_availability_service import recompute_channel  # noqa: E402
from services.connector_publisher import ConnectorPublisher, env_validator  # noqa: E402
from services.connector_publisher.healthcheck import is_healthy  # noqa: E402


# ---------------------------------------------------------------- helpers

def _mk_channel(channel_id, *, batch_size=2, rate=1000):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO channels (channel_id, display_name, delivery_url, batch_size, "
        "rate_limit_per_second, created_by, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (channel_id, channel_id, "https://sink.example.com/availability",
         batch_size, rate, "tester", str(uuid.uuid4())),
    )
    cur.close()


def _mk_item_with_stock(qty):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = f"PC-{uuid.uuid4().hex[:10]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, is_active, external_id) "
        "VALUES (%s, %s, TRUE, %s) RETURNING item_id",
        (sku, sku, str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.execute("SELECT bin_id, warehouse_id FROM bins WHERE bin_type='Pickable' "
                "ORDER BY bin_id LIMIT 1")
    bin_id, wh = cur.fetchone()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) "
        "VALUES (%s, %s, %s, %s)",
        (item_id, bin_id, wh, qty),
    )
    cur.close()
    return item_id, sku


def _channel_row(session, channel_id):
    return session.execute(
        sa_text("SELECT * FROM channels WHERE channel_id = :c"), {"c": channel_id}
    ).fetchone()


def _dirty_count(session, channel_id):
    return session.execute(
        sa_text("SELECT COUNT(*) AS n FROM channel_availability "
                "WHERE channel_id = :c AND current_version > last_version"),
        {"c": channel_id},
    ).fetchone().n


# ---------------------------------------------------------------- debounce

class TestDebounceGate:
    def test_due_logic(self):
        pub = ConnectorPublisher()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Never published -> due.
        assert pub._due(SimpleNamespace(debounce_seconds=30, last_published_at=None), now)
        # debounce 0 -> always due.
        assert pub._due(SimpleNamespace(debounce_seconds=0, last_published_at=now), now)
        # Inside the window -> not due.
        recent = now - timedelta(seconds=10)
        assert not pub._due(
            SimpleNamespace(debounce_seconds=30, last_published_at=recent), now)
        # Past the window -> due.
        old = now - timedelta(seconds=40)
        assert pub._due(
            SimpleNamespace(debounce_seconds=30, last_published_at=old), now)


# ---------------------------------------------------------------- drain loop

class TestDrainChannel:
    def test_backlog_drains_in_batches(self, _db_transaction, monkeypatch):
        # Exercise the loop, not the SSRF guard: bypass the dispatch-time check
        # (the fake sink hostname does not resolve). The guard's real behavior is
        # covered in test_connector_publisher_publish.
        monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
        session = md.SessionLocal()
        try:
            _mk_channel("amz", batch_size=2)
            skus = []
            for _ in range(5):
                _, sku = _mk_item_with_stock(3)
                skus.append(sku)
            recompute_channel(session, "amz", {"skus": skus})
            session.commit()
            assert _dirty_count(session, "amz") == 5

            calls = []

            def fake(url, payload):
                calls.append(len(payload["items"]))
                return 200, "ok"

            pub = ConnectorPublisher()
            pub._drain_channel(session, _channel_row(session, "amz"), fake)

            # 5 rows, batch_size 2 -> 2 + 2 + 1 across three POSTs, backlog cleared.
            assert calls == [2, 2, 1]
            assert _dirty_count(session, "amz") == 0
        finally:
            session.close()

    def test_drain_stops_on_failure(self, _db_transaction, monkeypatch):
        monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
        session = md.SessionLocal()
        try:
            _mk_channel("amz", batch_size=2)
            skus = [sku for _, sku in (_mk_item_with_stock(3) for _ in range(4))]
            recompute_channel(session, "amz", {"skus": skus})
            session.commit()

            calls = []

            def failing(url, payload):
                calls.append(1)
                return 500, "boom"

            pub = ConnectorPublisher()
            pub._drain_channel(session, _channel_row(session, "amz"), failing)

            # One failed batch, then stop -- a sick sink is not hammered.
            assert calls == [1]
            # Rows stay dirty (nothing advanced).
            assert _dirty_count(session, "amz") == 4
        finally:
            session.close()


# ---------------------------------------------------------------- healthcheck

class TestHealthcheck:
    def test_fresh_heartbeat_is_healthy(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text("x")
        mtime = os.path.getmtime(str(hb))
        assert is_healthy(str(hb), threshold_s=30, now_fn=lambda: mtime + 5)

    def test_stale_heartbeat_is_unhealthy(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text("x")
        mtime = os.path.getmtime(str(hb))
        assert not is_healthy(str(hb), threshold_s=30, now_fn=lambda: mtime + 100)

    def test_missing_heartbeat_is_unhealthy(self, tmp_path):
        assert not is_healthy(str(tmp_path / "nope"), threshold_s=30)


# ---------------------------------------------------------------- env validation

class TestEnvValidation:
    def test_out_of_range_fails_fast(self, monkeypatch):
        monkeypatch.setenv("CONNECTOR_PUBLISHER_RECONCILE_INTERVAL_S", "999999")
        with pytest.raises(SystemExit):
            env_validator.validate_or_die()

    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("CONNECTOR_PUBLISHER_FALLBACK_POLL_MS", raising=False)
        assert env_validator.int_var("CONNECTOR_PUBLISHER_FALLBACK_POLL_MS") == 2000

    def test_kill_switch_flag(self, monkeypatch):
        monkeypatch.setenv("CONNECTOR_PUBLISHER_ENABLED", "false")
        assert env_validator.enabled() is False
        monkeypatch.setenv("CONNECTOR_PUBLISHER_ENABLED", "true")
        assert env_validator.enabled() is True
