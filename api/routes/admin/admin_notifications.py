"""Admin CRUD for notification_webhooks (mig 068).

Per-warehouse Teams notification destinations for the backorder.*
event family. URLs are Fernet-encrypted at rest; the API never
returns the plaintext URL, only a masked host preview.

The live event -> Teams fan-out via a polling worker is deferred to
a follow-up; for now the test-send endpoint is the only direct
caller of send_to_teams. The events themselves emit
unconditionally (see partial_fulfill, cancel_backorder, and the
receipt hook), so the worker has a backlog to drain when it lands.
"""

import uuid
from urllib.parse import urlparse

from flask import g, jsonify
from sqlalchemy import text

from constants import ROLE_ADMIN
from middleware.auth_middleware import (
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.notification_webhooks import (
    CreateNotificationWebhookRequest,
    RotateNotificationWebhookUrlRequest,
    UpdateNotificationWebhookRequest,
    DEFAULT_EVENT_FILTER,
)
from services.credential_vault import decrypt_string, encrypt_string
from services.webhook_dispatcher.teams_adapter import (
    SendOutcome,
    build_adaptive_card,
    send_to_teams,
)
from utils.validation import validate_body


def _row_to_summary(row):
    """Public-safe row shape. Host preview is best-effort: a
    decrypt failure returns None rather than raising, so an
    operator can still see the row and delete it if the
    encryption key has rotated and the row is now stranded."""
    host_preview = None
    try:
        plaintext_url = decrypt_string(row.url)
        parsed = urlparse(plaintext_url)
        host_preview = parsed.netloc or parsed.path[:40]
    except Exception:
        host_preview = None
    return {
        "webhook_id":   row.webhook_id,
        "warehouse_id": row.warehouse_id,
        "channel_kind": row.channel_kind,
        "host_preview": host_preview,
        "event_filter": list(row.event_filter or []),
        "enabled":      row.enabled,
        "has_secret":   bool(row.secret),
        "external_id":  str(row.external_id),
        "created_at":   row.created_at.isoformat() if row.created_at else None,
        "created_by":   row.created_by,
    }


@admin_bp.route("/notification-webhooks", methods=["GET"])
@require_auth
@require_admin_or_page_permission("notifications")
@with_db
def list_notification_webhooks():
    from flask import request as flask_request
    warehouse_id = flask_request.args.get("warehouse_id", type=int)
    wh_clause = " WHERE warehouse_id = :wid " if warehouse_id else ""
    wh_params = {"wid": warehouse_id} if warehouse_id else {}
    rows = g.db.execute(
        text(
            "SELECT webhook_id, warehouse_id, channel_kind, url, event_filter, "
            "       enabled, secret, external_id, created_at, created_by "
            f"  FROM notification_webhooks {wh_clause}"
            " ORDER BY webhook_id DESC"
        ),
        wh_params,
    ).fetchall()
    return jsonify({
        "notification_webhooks": [_row_to_summary(r) for r in rows],
    })


@admin_bp.route("/notification-webhooks", methods=["POST"])
@require_auth
@require_admin_or_page_permission("notifications")
@validate_body(CreateNotificationWebhookRequest)
@with_db
def create_notification_webhook(validated):
    wh_exists = g.db.execute(
        text("SELECT 1 FROM warehouses WHERE warehouse_id = :wid"),
        {"wid": validated.warehouse_id},
    ).scalar()
    if not wh_exists:
        return jsonify({"error": f"warehouse_id {validated.warehouse_id} not found"}), 400

    event_filter = validated.event_filter or DEFAULT_EVENT_FILTER

    encrypted_url = encrypt_string(validated.url)
    row = g.db.execute(
        text(
            "INSERT INTO notification_webhooks ("
            "  warehouse_id, channel_kind, url, event_filter, enabled, "
            "  secret, created_by, external_id"
            ") VALUES ("
            "  :wid, :kind, :url, :filter, :enabled, :secret, :by, :ext_id"
            ") RETURNING webhook_id, warehouse_id, channel_kind, url, "
            "          event_filter, enabled, secret, external_id, "
            "          created_at, created_by"
        ),
        {
            "wid": validated.warehouse_id,
            "kind": validated.channel_kind,
            "url": encrypted_url,
            "filter": event_filter,
            "enabled": validated.enabled,
            "secret": validated.secret,
            "by": g.current_user["username"],
            "ext_id": str(uuid.uuid4()),
        },
    ).fetchone()
    g.db.commit()
    return jsonify(_row_to_summary(row)), 201


@admin_bp.route("/notification-webhooks/<int:webhook_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("notifications")
@validate_body(UpdateNotificationWebhookRequest)
@with_db
def update_notification_webhook(webhook_id, validated):
    if not any([
        validated.enabled is not None,
        validated.event_filter is not None,
        validated.secret is not None,
    ]):
        return jsonify({"error": "at least one field must be set"}), 400

    sets, params = [], {"wid": webhook_id}
    if validated.enabled is not None:
        sets.append("enabled = :enabled")
        params["enabled"] = validated.enabled
    if validated.event_filter is not None:
        sets.append("event_filter = :filter")
        params["filter"] = validated.event_filter
    if validated.secret is not None:
        sets.append("secret = :secret")
        params["secret"] = validated.secret

    row = g.db.execute(
        text(
            f"UPDATE notification_webhooks SET {', '.join(sets)} "
            " WHERE webhook_id = :wid "
            " RETURNING webhook_id, warehouse_id, channel_kind, url, "
            "          event_filter, enabled, secret, external_id, "
            "          created_at, created_by"
        ),
        params,
    ).fetchone()
    if not row:
        g.db.rollback()
        return jsonify({"error": "notification webhook not found"}), 404
    g.db.commit()
    return jsonify(_row_to_summary(row))


@admin_bp.route("/notification-webhooks/<int:webhook_id>/rotate-url", methods=["POST"])
@require_auth
@require_admin_or_page_permission("notifications")
@validate_body(RotateNotificationWebhookUrlRequest)
@with_db
def rotate_notification_webhook_url(webhook_id, validated):
    encrypted = encrypt_string(validated.url)
    row = g.db.execute(
        text(
            "UPDATE notification_webhooks SET url = :url "
            " WHERE webhook_id = :wid "
            " RETURNING webhook_id, warehouse_id, channel_kind, url, "
            "          event_filter, enabled, secret, external_id, "
            "          created_at, created_by"
        ),
        {"url": encrypted, "wid": webhook_id},
    ).fetchone()
    if not row:
        g.db.rollback()
        return jsonify({"error": "notification webhook not found"}), 404
    g.db.commit()
    return jsonify(_row_to_summary(row))


@admin_bp.route("/notification-webhooks/<int:webhook_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("notifications")
@with_db
def delete_notification_webhook(webhook_id):
    deleted = g.db.execute(
        text(
            "DELETE FROM notification_webhooks WHERE webhook_id = :wid "
            " RETURNING webhook_id"
        ),
        {"wid": webhook_id},
    ).fetchone()
    if not deleted:
        g.db.rollback()
        return jsonify({"error": "notification webhook not found"}), 404
    g.db.commit()
    return jsonify({"webhook_id": webhook_id, "message": "deleted"})


_SAMPLE_OPENED_PAYLOAD = {
    "backorder_so_external_id": "00000000-0000-0000-0000-000000000000",
    "backorder_so_number": "SO-TEST-BO",
    "parent_so_external_id": "00000000-0000-0000-0000-000000000000",
    "parent_so_number": "SO-TEST",
    "warehouse_id": 0,
    "customer_name": "Test Customer",
    "items": [
        {"item_external_id": "00000000-0000-0000-0000-000000000000", "sku": "DEMO-SKU", "qty": 1},
    ],
    "opened_by_user_external_id": "00000000-0000-0000-0000-000000000000",
    "opened_at": "2026-01-01T00:00:00Z",
}


@admin_bp.route("/notification-webhooks/<int:webhook_id>/test-send", methods=["POST"])
@require_auth
@require_admin_or_page_permission("notifications")
@with_db
def test_send_notification_webhook(webhook_id):
    """Fire a sample backorder.opened adaptive card at the configured
    URL so the operator can verify the Teams channel renders the card
    they expect. Does NOT touch integration_events; the live event-
    drain worker is a follow-up."""
    row = g.db.execute(
        text(
            "SELECT webhook_id, channel_kind, url FROM notification_webhooks "
            " WHERE webhook_id = :wid"
        ),
        {"wid": webhook_id},
    ).fetchone()
    if not row:
        return jsonify({"error": "notification webhook not found"}), 404

    if row.channel_kind != "teams":
        return jsonify({
            "error": f"test-send only supports channel_kind='teams'; got {row.channel_kind!r}",
        }), 400

    try:
        plaintext_url = decrypt_string(row.url)
    except Exception as exc:
        return jsonify({
            "error": "stored URL could not be decrypted (key rotated?). Rotate the URL or delete the row.",
            "detail": str(exc),
        }), 500

    card = build_adaptive_card("backorder.opened", _SAMPLE_OPENED_PAYLOAD)
    outcome: SendOutcome = send_to_teams(plaintext_url, card)
    return jsonify({
        "delivered": outcome.delivered,
        "status_code": outcome.status_code,
        "error_kind": outcome.error_kind,
        "error_detail": outcome.error_detail,
    })
