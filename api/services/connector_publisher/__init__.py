"""Pipe C connector-publisher daemon.

A standalone process (``python -m services.connector_publisher``) that, on each
cycle:

  1. reconciles per-channel sellable availability against live inventory
     (services.channel_availability_service), and
  2. debounce-publishes each active channel's dirty rows to its HTTP sink
     (services.connector_publisher.publish).

A cycle fires when the existing ``integration_events_visible`` NOTIFY wakes the
daemon (an inventory event landed) or when the periodic reconcile interval
elapses (the self-healing guarantee that catches allocation changes, which emit
no event). Single main loop -- per-channel debounce + a per-channel token bucket
provide the throttling the dispatcher needed threads for; availability is
last-writer-wins, so there is no ordered-delivery requirement to isolate.
"""

import logging
import os
import select
import signal
import threading
import time
from datetime import datetime, timezone

import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.channel_availability_service import recompute_active_channels
from services.webhook_dispatcher.rate_limiter import TokenBucket

from . import env_validator
from . import publish as publish_module

HEARTBEAT_FILE_DEFAULT = "/tmp/connector-publisher-heartbeat"
HEARTBEAT_INTERVAL_S = 5
LISTEN_CHANNEL = "integration_events_visible"

LOGGER = logging.getLogger("connector_publisher")


class ConnectorPublisher:
    def __init__(self):
        self._shutdown = threading.Event()
        self._buckets = {}            # channel_id -> TokenBucket
        self._engine = None
        self._Session = None
        self._listen_conn = None
        self._database_url = None
        self._heartbeat_file = env_validator.str_var(
            "CONNECTOR_PUBLISHER_HEARTBEAT_FILE", HEARTBEAT_FILE_DEFAULT
        )

    @property
    def enabled(self):
        return env_validator.enabled()

    # ---- lifecycle ----------------------------------------------------

    def run(self):
        env_validator.validate_or_die()
        self._install_signal_handlers()

        if not self.enabled:
            LOGGER.critical(
                "connector-publisher kill-switch active: "
                "CONNECTOR_PUBLISHER_ENABLED=false; writing heartbeats but "
                "publishing nothing. Unset it (or set true) and restart."
            )
            self._heartbeat_only_loop()
            return

        self._database_url = (
            os.environ.get("PUBLISHER_DATABASE_URL") or os.environ["DATABASE_URL"]
        )
        self._engine = create_engine(
            self._database_url, pool_pre_ping=True, pool_recycle=1800
        )
        self._Session = sessionmaker(bind=self._engine)
        self._open_listen()
        LOGGER.info("connector-publisher started")
        try:
            self._main_loop()
        finally:
            self._drain()

    def _install_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum, _frame):
        LOGGER.info("received signal %s; draining and shutting down", signum)
        self._shutdown.set()

    def _drain(self):
        if self._listen_conn is not None:
            try:
                self._listen_conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self._engine is not None:
            self._engine.dispose()
        LOGGER.info("connector-publisher stopped")

    # ---- main loop ----------------------------------------------------

    def _main_loop(self):
        poll_s = env_validator.int_var("CONNECTOR_PUBLISHER_FALLBACK_POLL_MS") / 1000.0
        reconcile_interval = env_validator.int_var(
            "CONNECTOR_PUBLISHER_RECONCILE_INTERVAL_S"
        )
        last_reconcile = 0.0
        last_heartbeat = 0.0
        self._write_heartbeat()

        while not self._shutdown.is_set():
            woke = self._wait_for_wake(poll_s)
            now = time.monotonic()
            if woke or (now - last_reconcile) >= reconcile_interval:
                try:
                    self._cycle()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("publish cycle failed: %s", exc)
                last_reconcile = time.monotonic()
            if (time.monotonic() - last_heartbeat) >= HEARTBEAT_INTERVAL_S:
                self._write_heartbeat()
                last_heartbeat = time.monotonic()

    def _heartbeat_only_loop(self):
        # Kill-switch mode: keep the container's healthcheck green without doing
        # any work, so an operator can disable the daemon without the
        # orchestrator restarting it on a stale heartbeat.
        while not self._shutdown.is_set():
            self._write_heartbeat()
            self._shutdown.wait(timeout=HEARTBEAT_INTERVAL_S)

    # ---- one cycle ----------------------------------------------------

    def _cycle(self):
        session = self._Session()
        try:
            recompute_active_channels(session)
            self._advance_cursor(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        self._publish_pass()

    def _advance_cursor(self, session):
        # Observability only: record how far the stream has been seen. The sweep
        # reconciles against live inventory, so correctness does not depend on
        # this cursor -- it just lets an operator see "swept through event N".
        session.execute(
            text(
                """
                UPDATE channel_recompute_state
                   SET last_cursor = GREATEST(
                           last_cursor,
                           COALESCE((SELECT MAX(event_id) FROM integration_events
                                      WHERE visible_at IS NOT NULL), 0)),
                       updated_at = NOW()
                 WHERE only_row = TRUE
                """
            )
        )

    def _publish_pass(self):
        connect_s = env_validator.int_var("CONNECTOR_PUBLISHER_HTTP_CONNECT_TIMEOUT_MS") / 1000.0
        read_s = env_validator.int_var("CONNECTOR_PUBLISHER_HTTP_READ_TIMEOUT_MS") / 1000.0

        def http_post(url, payload):
            return publish_module._default_http_post(
                url, payload, connect_timeout_s=connect_s, read_timeout_s=read_s
            )

        session = self._Session()
        try:
            channels = session.execute(
                text("SELECT * FROM channels WHERE status = 'active'")
            ).fetchall()
            now = datetime.now(timezone.utc)
            for ch in channels:
                if self._shutdown.is_set():
                    break
                if not self._due(ch, now):
                    continue
                self._drain_channel(session, ch, http_post)
        finally:
            session.close()

    def _due(self, ch, now):
        """A channel publishes at most once per debounce window. The window
        collapses every change accumulated since the last publish into the next
        batch, which is the whole point of Pipe C: marketplaces get a debounced
        snapshot, not a fan-out of every inventory tick."""
        if not ch.debounce_seconds or ch.last_published_at is None:
            return True
        return (now - ch.last_published_at).total_seconds() >= ch.debounce_seconds

    def _drain_channel(self, session, ch, http_post):
        bucket = self._bucket_for(ch)
        while not self._shutdown.is_set():
            if not bucket.acquire(timeout_s=1.0, shutdown=self._shutdown):
                return
            try:
                result = publish_module.publish_channel(session, ch, http_post=http_post)
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                LOGGER.exception("channel %s publish failed: %s", ch.channel_id, exc)
                return
            # Keep draining only while full batches keep coming back; a partial
            # or empty batch means the backlog is cleared. Stop on failure /
            # pause so a sick channel is not hammered.
            published = result.get("published")
            if published is None or published < ch.batch_size:
                return

    def _bucket_for(self, ch):
        bucket = self._buckets.get(ch.channel_id)
        if bucket is None:
            bucket = TokenBucket(ch.rate_limit_per_second)
            self._buckets[ch.channel_id] = bucket
        else:
            bucket.set_rate(ch.rate_limit_per_second)  # pick up a config change
        return bucket

    # ---- LISTEN/NOTIFY wake -------------------------------------------

    def _open_listen(self):
        try:
            self._listen_conn = psycopg2.connect(self._database_url)
            self._listen_conn.autocommit = True
            cur = self._listen_conn.cursor()
            cur.execute(f"LISTEN {LISTEN_CHANNEL};")
            cur.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "could not open LISTEN connection (%s); falling back to poll only",
                exc,
            )
            self._listen_conn = None

    def _wait_for_wake(self, timeout_s):
        """Return True if a NOTIFY arrived within timeout_s, False on timeout.
        Falls back to a plain sleep when the LISTEN connection is unavailable, so
        the periodic reconcile still runs."""
        if self._listen_conn is None:
            self._shutdown.wait(timeout=timeout_s)
            return False
        try:
            ready, _, _ = select.select([self._listen_conn], [], [], timeout_s)
            if not ready:
                return False
            self._listen_conn.poll()
            got = bool(self._listen_conn.notifies)
            self._listen_conn.notifies.clear()
            return got
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("LISTEN wake error: %s; reopening", exc)
            self._open_listen()
            return False

    # ---- heartbeat ----------------------------------------------------

    def _write_heartbeat(self):
        try:
            with open(self._heartbeat_file, "w") as fh:
                fh.write(str(time.time()))
        except OSError as exc:
            LOGGER.warning("could not write heartbeat file: %s", exc)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ConnectorPublisher().run()
