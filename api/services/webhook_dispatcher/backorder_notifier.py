"""Fire-and-forget Teams notification dispatcher for backorder.* events.

Each backorder.* emit site (partial_fulfill, cancel_backorder, the
parent-cancel cascade, and the receipt-hook matcher) calls
:func:`dispatch_backorder_notification` AFTER its DB transaction
commits. The helper spawns a daemon thread that:

1. Opens its own psycopg2 connection (the request's connection is
   closed once the response is sent).
2. Loads matching :code:`notification_webhooks` rows for the
   warehouse + event_type filter, gated on :code:`enabled = TRUE`.
3. For each match, decrypts the URL, builds the adaptive card via
   :mod:`teams_adapter`, and POSTs.
4. Logs the outcome and exits.

Why a thread, not a polling worker:

- Teams responses can take seconds. Doing the send synchronously in
  the request path would couple operator HTTP latency to Teams
  uptime; the request path already carries enough downstream
  integration latency.
- A separate polling worker would need a cursor table, retry
  state, and cooperation with the existing dispatcher's process
  manager. That is the right shape for a future delivery-tracking
  iteration; for now fire-and-forget is the proportionate response
  to "send a chat ping when stock arrives."
- Silent drops on transient Teams failures are acceptable because
  the /backorders dashboard is the operational source of truth.
  The :code:`integration_events` row persists regardless, so a
  follow-up worker (or an operator running a replay script) can
  back-fill from the outbox without losing context.

Caller contract: must be called AFTER the parent DB transaction
commits. Calling before commit means the thread may run before the
row is visible to its own connection. None of the current call
sites violate this; tests assert the ordering.
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from services.credential_vault import decrypt_string
from services.webhook_dispatcher.teams_adapter import (
    build_adaptive_card,
    send_to_teams,
)


LOGGER = logging.getLogger("webhook_dispatcher.backorder_notifier")


_SENTRY_BASE_URL_ENV = "SENTRY_ADMIN_BASE_URL"


def _resolve_base_url() -> str:
    """Optional. The MessageCard "Open in Sentry" link falls back to
    a relative URL when this is unset; relative still resolves
    inside Teams desktop / web clients."""
    return os.environ.get(_SENTRY_BASE_URL_ENV, "")


def dispatch_backorder_notification(
    *,
    event_type: str,
    payload: Dict[str, Any],
    warehouse_id: int,
    database_url: Optional[str] = None,
) -> threading.Thread:
    """Spawn a daemon thread that fans the event to every matching
    notification_webhook for the warehouse.

    Returns the started thread. Tests wait on it via ``thread.join()``;
    production callers ignore the return value.

    ``database_url`` defaults to :code:`os.environ['DATABASE_URL']`;
    tests can pass a specific value (e.g. the
    :code:`TEST_DATABASE_URL`) to keep the worker on the test DB."""
    if database_url is None:
        database_url = os.environ.get("DATABASE_URL", "")

    thread = threading.Thread(
        target=_dispatch,
        kwargs={
            "event_type":   event_type,
            "payload":      payload,
            "warehouse_id": warehouse_id,
            "database_url": database_url,
            "sentry_base_url": _resolve_base_url(),
        },
        daemon=True,
        name=f"backorder-notifier-{event_type}-{warehouse_id}",
    )
    thread.start()
    return thread


def _dispatch(
    *,
    event_type: str,
    payload: Dict[str, Any],
    warehouse_id: int,
    database_url: str,
    sentry_base_url: str,
) -> None:
    """Thread body. Wrapped in a broad try/except so a thread-side
    surprise (DB unreachable, malformed row, etc.) is logged and
    contained instead of bubbling into a noisy unhandled-exception
    log."""
    try:
        if not database_url:
            LOGGER.warning(
                "backorder_notifier: skipping dispatch for event_type=%s "
                "warehouse_id=%d -- DATABASE_URL unset",
                event_type, warehouse_id,
            )
            return

        webhooks = _load_matching_webhooks(
            database_url=database_url,
            event_type=event_type,
            warehouse_id=warehouse_id,
        )
        if not webhooks:
            LOGGER.info(
                "backorder_notifier: no enabled webhooks for "
                "event_type=%s warehouse_id=%d",
                event_type, warehouse_id,
            )
            return

        card = build_adaptive_card(
            event_type, payload, sentry_base_url=sentry_base_url
        )
        for webhook in webhooks:
            # Per-webhook isolation: a raise inside _send_one (e.g. a
            # mocked send_to_teams in a test, or an unexpected DB
            # access path) must NOT stop the loop. Otherwise one bad
            # row poisons every later webhook in the fan-out.
            try:
                _send_one(webhook, event_type, card)
            except Exception as exc:  # noqa: BLE001 -- per-webhook isolation
                LOGGER.exception(
                    "backorder_notifier: webhook_id=%s send raised: %s",
                    webhook.get("webhook_id"), exc,
                )
    except Exception as exc:  # noqa: BLE001 -- isolate thread
        LOGGER.exception(
            "backorder_notifier: unhandled error dispatching event_type=%s "
            "warehouse_id=%d: %s",
            event_type, warehouse_id, exc,
        )


def _load_matching_webhooks(
    *,
    database_url: str,
    event_type: str,
    warehouse_id: int,
) -> list:
    """Return enabled notification_webhooks rows for the warehouse
    whose event_filter includes :code:`event_type`. Opens and closes
    its own connection per call; intentional because the helper is
    called once per emit and threading-per-call is fine for the
    expected emit volume (handfuls per day, not bursts per
    second)."""
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT webhook_id, warehouse_id, channel_kind, url,
                       event_filter, enabled, secret, external_id
                  FROM notification_webhooks
                 WHERE warehouse_id = %s
                   AND enabled = TRUE
                   AND %s = ANY(event_filter)
                """,
                (warehouse_id, event_type),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _send_one(webhook: Dict[str, Any], event_type: str, card: Dict[str, Any]) -> None:
    """Decrypt URL, send card, log outcome. Each webhook is wrapped
    in its own try/except so one bad row does not poison the rest
    of the fan-out."""
    webhook_id = webhook.get("webhook_id")
    channel_kind = webhook.get("channel_kind")
    if channel_kind != "teams":
        LOGGER.warning(
            "backorder_notifier: skipping webhook_id=%s -- "
            "channel_kind=%r is not supported",
            webhook_id, channel_kind,
        )
        return
    try:
        plaintext_url = decrypt_string(webhook["url"])
    except Exception as exc:  # noqa: BLE001
        LOGGER.error(
            "backorder_notifier: webhook_id=%s URL decrypt failed: %s "
            "(stranded by an encryption-key rotation?)",
            webhook_id, exc,
        )
        return
    outcome = send_to_teams(plaintext_url, card)
    if outcome.delivered:
        LOGGER.info(
            "backorder_notifier: webhook_id=%s event_type=%s delivered "
            "(status=%s)",
            webhook_id, event_type, outcome.status_code,
        )
    else:
        LOGGER.warning(
            "backorder_notifier: webhook_id=%s event_type=%s failed "
            "(kind=%s status=%s detail=%s)",
            webhook_id, event_type, outcome.error_kind,
            outcome.status_code, outcome.error_detail,
        )
