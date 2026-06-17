"""Teams adapter for the backorder.* event family.

The adapter has two pure functions and one IO function:

- :func:`build_adaptive_card(event_type, payload)` returns a Microsoft
  Teams MessageCard dict ready to POST to a Teams incoming-webhook
  URL. Pure, no IO, no DB; trivial to unit-test.
- :func:`send_to_teams(url, card)` POSTs the card. Wraps the URL in
  the dispatcher's existing SSRF guard so a misconfigured webhook
  cannot point at AWS IMDS or other internal addresses. Returns
  :class:`SendOutcome`.
- The polling worker that drains :code:`integration_events` and
  fans out to :code:`notification_webhooks` is deferred to a
  follow-up; for now the test-send endpoint
  (:code:`POST /admin/notification-webhooks/<id>/test-send`) is the
  only live caller of :func:`send_to_teams`. The events themselves
  emit unconditionally, so the worker has a backlog to drain when
  it lands.

MessageCard format intentionally over Adaptive Cards: incoming-webhook
URLs in Teams accept the legacy MessageCard JSON directly. Adaptive
Cards 1.5+ require a Power Automate flow as middleware, which would
add a per-tenant moving part we do not need for a warehouse
notification surface.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from .ssrf_guard import SsrfRejected, assert_url_safe


_SENTRY_ADMIN_BASE_DEFAULT = "/sales-orders"
_THEME_OPENED      = "FF8C00"   # warm orange: attention needed
_THEME_FULFILLABLE = "2E7D32"   # green: ready to ship
_THEME_CANCELLED   = "9E9E9E"   # gray: terminal / informational

_REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class SendOutcome:
    """Result of a single :func:`send_to_teams` call.

    ``status_code`` is the HTTP status from the Teams endpoint, or
    None if the SSRF guard or a network error fired before any
    response was received. ``error_kind`` is a short string for
    logging / dashboards; mirrors the dispatcher's existing error
    taxonomy."""
    delivered: bool
    status_code: Optional[int] = None
    error_kind: Optional[str] = None
    error_detail: Optional[str] = None


def _sentry_link(base_url: str, so_number: str) -> str:
    """Compose the deep-link the card's action button hands to the
    operator. ``base_url`` is the absolute base
    (e.g. ``https://sentry.example.com``); on a stack that runs the
    admin behind a relative path, callers can pass an empty string
    and the focus query-param alone will resolve against the page
    that opens the link."""
    suffix = f"?focus={so_number}"
    if not base_url:
        return f"{_SENTRY_ADMIN_BASE_DEFAULT}{suffix}"
    return f"{base_url.rstrip('/')}{_SENTRY_ADMIN_BASE_DEFAULT}{suffix}"


def _items_summary(items: list) -> str:
    """Render the items list for the card body. Keeps the line short
    enough to render cleanly in the Teams desktop client (one line
    per item, up to 5; trailing "+N more" if longer)."""
    if not items:
        return "(no items)"
    head = items[:5]
    rendered = [f"- {it['sku']} × {it['qty']}" for it in head]
    if len(items) > 5:
        rendered.append(f"- (+{len(items) - 5} more)")
    return "\n".join(rendered)


def build_adaptive_card(event_type: str, payload: Dict[str, Any], *, sentry_base_url: str = "") -> Dict[str, Any]:
    """Return the Teams MessageCard dict for one of the backorder.*
    events. Raises :class:`ValueError` for an unknown event_type so a
    caller bug fails loud rather than sending an empty card."""
    if event_type == "backorder.opened":
        return _build_opened_card(payload, sentry_base_url)
    if event_type == "backorder.fulfillable":
        return _build_fulfillable_card(payload, sentry_base_url)
    if event_type == "backorder.cancelled":
        return _build_cancelled_card(payload, sentry_base_url)
    raise ValueError(f"teams_adapter: unsupported event_type {event_type!r}")


def _base_card(*, theme: str, summary: str, title: str, text_body: str) -> Dict[str, Any]:
    return {
        "@type":       "MessageCard",
        "@context":    "https://schema.org/extensions",
        "themeColor":  theme,
        "summary":     summary,
        "title":       title,
        "text":        text_body,
    }


def _open_action(label: str, uri: str) -> Dict[str, Any]:
    return {
        "@type":   "OpenUri",
        "name":    label,
        "targets": [{"os": "default", "uri": uri}],
    }


def _build_opened_card(payload: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    bo_number     = payload.get("backorder_so_number", "")
    parent_number = payload.get("parent_so_number", "")
    customer      = payload.get("customer_name") or "(unknown customer)"
    items         = payload.get("items", [])
    link          = _sentry_link(base_url, bo_number)

    title = f"Item shorted on {parent_number}"
    body = (
        f"**Customer:** {customer}\n\n"
        f"**Backorder:** {bo_number}\n\n"
        f"**Items shorted:**\n{_items_summary(items)}"
    )
    card = _base_card(
        theme=_THEME_OPENED,
        summary=f"Backorder opened: {bo_number}",
        title=title,
        text_body=body,
    )
    card["potentialAction"] = [_open_action("Open in Sentry", link)]
    return card


def _build_fulfillable_card(payload: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    bo_number     = payload.get("backorder_so_number", "")
    parent_number = payload.get("parent_so_number", "")
    customer      = payload.get("customer_name") or "(unknown customer)"
    items         = payload.get("items", [])
    days          = payload.get("days_waiting", 0)
    link          = _sentry_link(base_url, bo_number)

    title = f"Backorder ready to ship: {bo_number}"
    body = (
        f"**Customer:** {customer}\n\n"
        f"**Original SO:** {parent_number}\n\n"
        f"**Days waiting:** {days}\n\n"
        f"**Items:**\n{_items_summary(items)}"
    )
    card = _base_card(
        theme=_THEME_FULFILLABLE,
        summary=f"Backorder fulfillable: {bo_number}",
        title=title,
        text_body=body,
    )
    card["potentialAction"] = [_open_action("Open in Sentry", link)]
    return card


def _build_cancelled_card(payload: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    bo_number     = payload.get("backorder_so_number", "")
    parent_number = payload.get("parent_so_number", "")
    reason        = payload.get("cancellation_reason", "other")
    link          = _sentry_link(base_url, bo_number)

    title = f"Backorder cancelled: {bo_number}"
    body = (
        f"**Original SO:** {parent_number}\n\n"
        f"**Reason:** {reason}"
    )
    card = _base_card(
        theme=_THEME_CANCELLED,
        summary=f"Backorder cancelled: {bo_number}",
        title=title,
        text_body=body,
    )
    card["potentialAction"] = [_open_action("Open in Sentry", link)]
    return card


def send_to_teams(url: str, card: Dict[str, Any]) -> SendOutcome:
    """POST the card to the Teams webhook URL. SSRF-guarded; a URL
    that resolves to a private range is rejected without an HTTP
    call. Network and Teams-side errors are captured and returned as
    :class:`SendOutcome` rather than raised so a caller can log a
    structured failure without try/except wrapping."""
    try:
        assert_url_safe(url)
    except SsrfRejected as exc:
        return SendOutcome(
            delivered=False,
            error_kind="ssrf_rejected",
            error_detail=str(exc),
        )

    try:
        resp = requests.post(
            url,
            json=card,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json"},
        )
    except requests.exceptions.Timeout as exc:
        return SendOutcome(
            delivered=False,
            error_kind="timeout",
            error_detail=str(exc),
        )
    except requests.exceptions.RequestException as exc:
        return SendOutcome(
            delivered=False,
            error_kind="network_error",
            error_detail=str(exc),
        )

    # Teams accepts the card and returns 200 with body "1". Any 2xx
    # is treated as delivered; non-2xx is a failure.
    if 200 <= resp.status_code < 300:
        return SendOutcome(delivered=True, status_code=resp.status_code)
    return SendOutcome(
        delivered=False,
        status_code=resp.status_code,
        error_kind="teams_rejected",
        error_detail=(resp.text or "")[:500],
    )
