"""Dispatcher self-monitor + Teams health alerting.

The webhook dispatcher's worst failure mode is not a crash -- it is a
SILENT degradation. The 2026-06-03 incident (a wedged delivery that
head-of-line-blocked the whole subscription) ran ~22 hours before a
human noticed. The reliability fixes (#1 DNS-in-watchdog, #2 stale
in_flight reaper, #3 fast-DLQ) stop the catastrophic version of that;
this module makes the *remaining* degradations LOUD so an operator is
paged in minutes instead of finding out the next morning.

Design rules:

  * **Independent channel.** The monitor sends straight to a Teams
    incoming-webhook via :mod:`teams_adapter`, NOT through the
    dispatcher's own webhook_deliveries queue. If the alert about a
    delivery outage went through the delivery machinery, the outage
    would suppress its own alarm.

  * **Reuses the existing notification surface.** Operators do not
    learn a new config screen: a Teams channel subscribes to the
    synthetic ``dispatcher.*`` alert types on the same Notifications
    admin page (notification_webhooks rows) they already use for
    backorder.* cards. The monitor fans out to every enabled teams
    row whose ``event_filter`` includes the alert type. Dispatcher
    health is global, so the fan-out is NOT warehouse-scoped (an
    operator picks any warehouse_id when creating the row; the alert
    goes to whoever subscribed).

  * **Opt-in + zero-cost when unconfigured.** Each cycle first asks
    which alert types have a subscriber; if none, it runs no checks
    and sends nothing. Adding a webhook row turns it on with no
    redeploy.

  * **Anti-spam.** A condition fires once on transition, then at most
    once per cooldown while it persists, and re-arms when it clears.

Thresholds are env-tunable (see :mod:`env_validator`); the alert
*types* and the destination channel are editable in the admin UI.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from services.credential_vault import decrypt_string
from . import teams_adapter


LOGGER = logging.getLogger("webhook_dispatcher.dispatcher_health")


# Synthetic alert "event types". Mirrors
# schemas.notification_webhooks._DISPATCHER_ALERT_EVENT_TYPES and the
# admin UI's ALLOWED_EVENT_TYPES; kept in sync by the contract test.
ALERT_DELIVERY_STALLED = "dispatcher.delivery_stalled"
ALERT_DLQ_GROWTH = "dispatcher.dlq_growth"
ALERT_SUBSCRIPTION_LAGGING = "dispatcher.subscription_lagging"
ALERT_SUBSCRIPTION_PAUSED = "dispatcher.subscription_paused"

ALERT_EVENT_TYPES = (
    ALERT_DELIVERY_STALLED,
    ALERT_DLQ_GROWTH,
    ALERT_SUBSCRIPTION_LAGGING,
    ALERT_SUBSCRIPTION_PAUSED,
)


# A triggered alert is keyed (alert_type, subscription_id) so the same
# condition on two subscriptions alerts independently and dedupes
# independently.
AlertKey = Tuple[str, str]


def _default_send(url: str, card: dict) -> bool:
    """Production send path. Returns True when Teams accepted the card.
    Wrapped here so tests can inject a fake without importing the real
    network call."""
    outcome = teams_adapter.send_to_teams(url, card)
    return bool(outcome.delivered)


class HealthMonitor(threading.Thread):
    """Background thread that polls dispatcher health and fans out
    Teams alerts. One per dispatcher process; mirrors the
    :class:`wake.WakeOrchestrator` thread lifecycle (start / shutdown /
    join). Each ``run_checks`` cycle opens and closes its own DB
    connection so a dropped connection self-heals on the next tick
    without a reconnect state machine."""

    def __init__(
        self,
        database_url: str,
        *,
        check_interval_s: float = 60.0,
        stall_age_s: int = 60,
        dlq_threshold: int = 1,
        lag_threshold: int = 100,
        cooldown_s: float = 1800.0,
        shutdown: Optional[threading.Event] = None,
        send_fn: Optional[Callable[[str, dict], bool]] = None,
    ):
        super().__init__(daemon=True, name="webhook-dispatcher-health")
        self.database_url = database_url
        self.check_interval_s = check_interval_s
        self.stall_age_s = stall_age_s
        self.dlq_threshold = dlq_threshold
        self.lag_threshold = lag_threshold
        self.cooldown_s = cooldown_s
        self._shutdown = shutdown or threading.Event()
        self._send_fn = send_fn or _default_send
        # AlertKey -> last_sent monotonic timestamp. Drives the
        # transition + cooldown dedup.
        self._active: Dict[AlertKey, float] = {}

    # -- lifecycle ---------------------------------------------------

    def shutdown(self) -> None:
        self._shutdown.set()

    def run(self) -> None:
        LOGGER.info(
            "dispatcher health monitor started (interval=%.0fs, stall=%ds, "
            "dlq_threshold=%d, lag_threshold=%d, cooldown=%.0fs)",
            self.check_interval_s, self.stall_age_s, self.dlq_threshold,
            self.lag_threshold, self.cooldown_s,
        )
        # wait() returns False on timeout (run a cycle) and True when
        # shutdown fires (exit). Checks run on the cadence, not at boot,
        # so a flapping boot does not spam an alert immediately.
        while not self._shutdown.wait(self.check_interval_s):
            try:
                self.run_checks()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "dispatcher health check cycle failed (%s); will retry "
                    "next tick", exc,
                )

    # -- one cycle ---------------------------------------------------

    def run_checks(self, _now: Optional[float] = None) -> List[AlertKey]:
        """Run every subscribed check once, fan out the alerts that are
        due (new or past cooldown), and re-arm conditions that have
        cleared. Returns the list of AlertKeys that were SENT this cycle
        (the test hook). ``_now`` overrides the monotonic clock for
        deterministic cooldown tests."""
        now = _now if _now is not None else time.monotonic()
        conn = psycopg2.connect(self.database_url)
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=RealDictCursor)

            subscribed = self._subscribed_alert_types(cur)
            if not subscribed:
                # Nobody is listening: do no work, but still clear any
                # stale dedup state so an unsubscribe-then-resubscribe
                # re-alerts on the next real trigger.
                self._active.clear()
                return []

            triggered: Dict[AlertKey, dict] = {}
            if ALERT_DELIVERY_STALLED in subscribed:
                self._collect(triggered, ALERT_DELIVERY_STALLED,
                              self._check_delivery_stalled(cur))
            if ALERT_DLQ_GROWTH in subscribed:
                self._collect(triggered, ALERT_DLQ_GROWTH,
                              self._check_dlq_growth(cur))
            if ALERT_SUBSCRIPTION_LAGGING in subscribed:
                self._collect(triggered, ALERT_SUBSCRIPTION_LAGGING,
                              self._check_subscription_lagging(cur))
            if ALERT_SUBSCRIPTION_PAUSED in subscribed:
                self._collect(triggered, ALERT_SUBSCRIPTION_PAUSED,
                              self._check_subscription_paused(cur))

            sent: List[AlertKey] = []
            for key, details in triggered.items():
                last = self._active.get(key)
                if last is None or (now - last) >= self.cooldown_s:
                    alert_type = key[0]
                    self._fan_out(cur, alert_type, details)
                    self._active[key] = now
                    sent.append(key)

            # Re-arm: a condition that is no longer triggered drops out
            # of the dedup map so its next occurrence alerts immediately
            # rather than waiting out a stale cooldown.
            for key in [k for k in self._active if k not in triggered]:
                del self._active[key]

            return sent
        finally:
            conn.close()

    @staticmethod
    def _collect(acc: Dict[AlertKey, dict], alert_type: str, rows: List[dict]) -> None:
        for row in rows:
            sub_id = row["subscription_id"]
            acc[(alert_type, sub_id)] = {**row}

    # -- which alert types have a subscriber -------------------------

    def _subscribed_alert_types(self, cur) -> set:
        """The set of dispatcher.* alert types that at least one enabled
        Teams notification_webhook subscribes to. Empty -> the monitor
        is a no-op this cycle."""
        cur.execute(
            """
            SELECT DISTINCT unnest(event_filter) AS et
              FROM notification_webhooks
             WHERE enabled = TRUE
               AND channel_kind = 'teams'
            """
        )
        configured = {r["et"] for r in cur.fetchall()}
        return configured & set(ALERT_EVENT_TYPES)

    # -- individual checks (each returns list of detail dicts) -------

    def _check_delivery_stalled(self, cur) -> List[dict]:
        cur.execute(
            """
            SELECT subscription_id::text AS subscription_id,
                   COUNT(*)::int AS stuck_count,
                   EXTRACT(EPOCH FROM (NOW() - MIN(attempted_at)))::int
                       AS oldest_age_s
              FROM webhook_deliveries
             WHERE status = 'in_flight'
               AND attempted_at IS NOT NULL
               AND attempted_at < NOW() - make_interval(secs => %s)
             GROUP BY subscription_id
            """,
            (self.stall_age_s,),
        )
        return [dict(r) for r in cur.fetchall()]

    def _check_dlq_growth(self, cur) -> List[dict]:
        cur.execute(
            """
            SELECT subscription_id::text AS subscription_id,
                   COUNT(*)::int AS dlq_count,
                   array_agg(DISTINCT error_kind) AS recent_error_kinds
              FROM webhook_deliveries
             WHERE status = 'dlq'
             GROUP BY subscription_id
            HAVING COUNT(*) >= %s
            """,
            (self.dlq_threshold,),
        )
        out = []
        for r in cur.fetchall():
            d = dict(r)
            kinds = [k for k in (d.get("recent_error_kinds") or []) if k]
            d["recent_error_kinds"] = ", ".join(sorted(kinds)) if kinds else None
            out.append(d)
        return out

    def _check_subscription_lagging(self, cur) -> List[dict]:
        # Lag = events past the cursor that have cleared the visible_at
        # gate. Filter-agnostic (it does not apply the subscription
        # filter), so it can over-count for a narrowly-filtered
        # subscription -- acceptable for a "falling behind" signal whose
        # job is to be loud, not precise.
        cur.execute(
            """
            SELECT s.subscription_id::text AS subscription_id,
                   (SELECT COUNT(*)
                      FROM integration_events e
                     WHERE e.event_id > s.last_delivered_event_id
                       AND e.visible_at IS NOT NULL
                       AND e.visible_at <= NOW() - INTERVAL '2 seconds'
                   )::int AS lag_count
              FROM webhook_subscriptions s
             WHERE s.status = 'active'
            """
        )
        return [
            dict(r) for r in cur.fetchall()
            if (r["lag_count"] or 0) >= self.lag_threshold
        ]

    def _check_subscription_paused(self, cur) -> List[dict]:
        cur.execute(
            """
            SELECT subscription_id::text AS subscription_id,
                   pause_reason
              FROM webhook_subscriptions
             WHERE status = 'paused'
               AND pause_reason IS NOT NULL
            """
        )
        return [dict(r) for r in cur.fetchall()]

    # -- fan-out -----------------------------------------------------

    def _fan_out(self, cur, alert_type: str, details: dict) -> None:
        """Send the alert card to every enabled Teams webhook subscribed
        to ``alert_type``. Per-webhook isolation: one bad row (decrypt
        failure, send error) does not stop the rest."""
        cur.execute(
            """
            SELECT webhook_id, url
              FROM notification_webhooks
             WHERE enabled = TRUE
               AND channel_kind = 'teams'
               AND %s = ANY(event_filter)
            """,
            (alert_type,),
        )
        rows = cur.fetchall()
        if not rows:
            return
        try:
            card = teams_adapter.build_dispatcher_alert_card(alert_type, details)
        except ValueError as exc:
            LOGGER.error("dispatcher health: %s", exc)
            return
        for row in rows:
            webhook_id = row["webhook_id"]
            try:
                url = decrypt_string(row["url"])
            except Exception as exc:  # noqa: BLE001
                LOGGER.error(
                    "dispatcher health: webhook_id=%s URL decrypt failed "
                    "(key rotated?): %s", webhook_id, exc,
                )
                continue
            try:
                delivered = self._send_fn(url, card)
            except Exception as exc:  # noqa: BLE001 -- per-webhook isolation
                LOGGER.warning(
                    "dispatcher health: webhook_id=%s send raised for %s: %s",
                    webhook_id, alert_type, exc,
                )
                continue
            if delivered:
                LOGGER.info(
                    "dispatcher health: alerted %s on webhook_id=%s (sub=%s)",
                    alert_type, webhook_id, details.get("subscription_id"),
                )
            else:
                LOGGER.warning(
                    "dispatcher health: webhook_id=%s did not accept %s alert",
                    webhook_id, alert_type,
                )
