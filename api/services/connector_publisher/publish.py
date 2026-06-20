"""Per-channel publish: claim dirty availability rows, transform, POST to the
channel's HTTP sink, advance or back off.

Kept free of the daemon's threads / LISTEN loop so it is unit-testable: the
caller injects ``http_post`` (a fake in tests, a real requests call in
production) and a live DB session. The orchestration in ``__init__`` owns
debounce, the per-channel rate-limit token bucket, and scheduling; this module
owns one batch.

Availability is current-state (last-writer-wins per channel+item), so a failed
publish is never lost data -- the row stays dirty and the next cycle re-sends
whatever the number is by then. Retries exist only to ride out a transient sink
outage; the final attempt parks the batch in the per-row DLQ.
"""

import logging

import requests
from sqlalchemy import text

from services.webhook_dispatcher.ssrf_guard import SsrfRejected, assert_url_safe

LOGGER = logging.getLogger("connector_publisher")

# Backoff (seconds) indexed by the row's new attempt_count. The attempt after
# the last entry parks the row in the DLQ.
RETRY_BACKOFF_S = [30, 120, 600, 3600]
MAX_ATTEMPTS = len(RETRY_BACKOFF_S) + 1  # 5th failure -> DLQ


def apply_transform(item, transform):
    """Declarative per-item transform: rename keys, then inject constants. No
    expression evaluation. ``{"rename": {"sku": "seller_sku"}, "constants":
    {"fulfillment_channel": "AMAZON_NA"}}`` turns ``{"sku": "X", "available": 3}``
    into ``{"seller_sku": "X", "available": 3, "fulfillment_channel": "AMAZON_NA"}``.
    """
    transform = transform or {}
    renames = transform.get("rename") or {}
    constants = transform.get("constants") or {}
    out = {renames.get(k, k): v for k, v in item.items()}
    out.update(constants)
    return out


def _default_http_post(url, payload, *, connect_timeout_s=5.0, read_timeout_s=8.0):
    """Real sink POST. Returns (status_code, body_text). Network errors raise --
    the caller treats a raise as a failed batch."""
    resp = requests.post(url, json=payload, timeout=(connect_timeout_s, read_timeout_s))
    return resp.status_code, (resp.text or "")[:512]


def _claim_dirty(db, channel_id, batch_size):
    return db.execute(
        text(
            """
            SELECT ca.item_id, ca.current_version, ca.available_qty,
                   ca.attempt_count, i.sku
              FROM channel_availability ca
              JOIN items i ON i.item_id = ca.item_id
             WHERE ca.channel_id = :cid
               AND ca.current_version > ca.last_version
               AND ca.dlq = FALSE
               AND (ca.next_attempt_at IS NULL OR ca.next_attempt_at <= NOW())
             ORDER BY ca.current_version ASC
             LIMIT :batch
            """
        ),
        {"cid": channel_id, "batch": batch_size},
    ).fetchall()


def _build_payload(channel_id, rows, transform):
    items = [
        apply_transform({"sku": r.sku, "available": int(r.available_qty)}, transform)
        for r in rows
    ]
    return {"channel_id": channel_id, "items": items}


def _mark_published(db, channel_id, rows):
    # last_version advances to the exact version published. If a concurrent
    # recompute bumped current_version even higher meanwhile, the row stays
    # dirty (current_version > last_version) and republishes next cycle with the
    # newer number -- no update is ever silently dropped.
    for r in rows:
        db.execute(
            text(
                """
                UPDATE channel_availability
                   SET last_version      = :cv,
                       last_published_at  = NOW(),
                       attempt_count      = 0,
                       next_attempt_at    = NULL,
                       dlq                = FALSE,
                       last_error         = NULL,
                       updated_at         = NOW()
                 WHERE channel_id = :cid AND item_id = :iid
                """
            ),
            {"cv": r.current_version, "cid": channel_id, "iid": r.item_id},
        )
    db.execute(
        text(
            "UPDATE channels SET last_published_at = NOW(), updated_at = NOW() "
            "WHERE channel_id = :cid"
        ),
        {"cid": channel_id},
    )


def _mark_failed(db, channel_id, rows, err):
    for r in rows:
        new_attempt = int(r.attempt_count) + 1
        if new_attempt >= MAX_ATTEMPTS:
            backoff = RETRY_BACKOFF_S[-1]
            dlq = True
        else:
            backoff = RETRY_BACKOFF_S[new_attempt - 1]
            dlq = False
        db.execute(
            text(
                """
                UPDATE channel_availability
                   SET attempt_count   = :attempt,
                       next_attempt_at  = NOW() + make_interval(secs => :backoff),
                       dlq              = :dlq,
                       last_error       = :err,
                       updated_at       = NOW()
                 WHERE channel_id = :cid AND item_id = :iid
                """
            ),
            {
                "attempt": new_attempt,
                "backoff": backoff,
                "dlq": dlq,
                "err": (err or "")[:512],
                "cid": channel_id,
                "iid": r.item_id,
            },
        )


def _pause_channel(db, channel_id, reason):
    db.execute(
        text(
            "UPDATE channels SET status = 'paused', pause_reason = :r, "
            "updated_at = NOW() WHERE channel_id = :cid AND status = 'active'"
        ),
        {"r": reason, "cid": channel_id},
    )


def publish_channel(db, channel, *, http_post=None, ssrf_check=None):
    """Publish one batch of dirty rows for ``channel`` (a mapping/row with
    channel_id, delivery_url, transform, batch_size). Returns a result dict:
    ``{"published": n}`` on success, ``{"failed": n}`` on a sink error,
    ``{"skipped": "no_dirty"}`` when nothing is pending, or
    ``{"paused": reason}`` when the sink URL fails the SSRF guard.

    ``ssrf_check`` defaults to the real dispatch-time guard; it is injectable so
    a test can drive the pause branch without depending on DNS (the guard's own
    private-IP detection is covered by the webhook-dispatcher SSRF tests). The
    caller commits. All writes here are inside the caller's transaction so a
    crash mid-batch rolls back cleanly and the rows stay dirty.
    """
    http_post = http_post or _default_http_post
    ssrf_check = ssrf_check or assert_url_safe
    channel_id = channel.channel_id
    rows = _claim_dirty(db, channel_id, channel.batch_size)
    if not rows:
        return {"skipped": "no_dirty"}

    # Dispatch-time SSRF guard, same posture as the webhook dispatcher: a sink
    # that resolves to a private / loopback / IMDS address is a misconfiguration
    # (or an attack), so pause the channel rather than POST to it.
    try:
        ssrf_check(channel.delivery_url)
    except SsrfRejected as exc:
        LOGGER.error("channel %s sink failed SSRF guard: %s", channel_id, exc)
        _pause_channel(db, channel_id, "malformed_config")
        return {"paused": "malformed_config"}

    payload = _build_payload(channel_id, rows, channel.transform)
    try:
        status, body = http_post(channel.delivery_url, payload)
    except Exception as exc:  # noqa: BLE001 -- any network error is a failed batch
        LOGGER.warning("channel %s publish errored: %s", channel_id, exc)
        _mark_failed(db, channel_id, rows, str(exc))
        return {"failed": len(rows)}

    if 200 <= status < 300:
        _mark_published(db, channel_id, rows)
        return {"published": len(rows)}

    LOGGER.warning("channel %s sink returned HTTP %s: %s", channel_id, status, body)
    _mark_failed(db, channel_id, rows, f"HTTP {status}: {body}")
    return {"failed": len(rows)}
