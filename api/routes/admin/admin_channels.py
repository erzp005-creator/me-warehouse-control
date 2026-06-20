"""Admin CRUD for Pipe C availability channels (v1.30.0).

A channel owns an HTTP sink (delivery_url), a SKU scope, a declarative transform,
and rate / batch / debounce knobs. The connector-publisher daemon reconciles and
debounce-publishes against these rows. Every mutation writes an audit_log row;
the create and any sku_scope change re-materialize the channel's availability so
the operator sees it populate immediately.

Mirrors the v1.6 webhook-subscription CRUD: strict-typed bodies, https/SSRF
guards on the sink, soft-delete to 'revoked'.
"""

import os
import uuid
from urllib.parse import urlparse

from flask import g, jsonify
from sqlalchemy import text

from constants import (
    ACTION_CHANNEL_CREATE,
    ACTION_CHANNEL_DELETE,
    ACTION_CHANNEL_UPDATE,
)
from middleware.auth_middleware import require_admin_or_page_permission, require_auth
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.channels import CreateChannelRequest, UpdateChannelRequest
from services.audit_service import write_audit_log
from services.channel_availability_service import recompute_channel
from services.rate_limit import limiter
from services.webhook_dispatcher import ssrf_guard
from utils.validation import validate_body

_PAGE = "channels"


def _http_sink_allowed() -> bool:
    # Reuse the dispatcher's HTTPS-only opt-out: only the literal 'true' relaxes
    # it, and the dispatcher env validator refuses that in production.
    return os.environ.get("SENTRY_ALLOW_HTTP_WEBHOOKS", "").lower() == "true"


def _validate_sink(url: str):
    """Return an (error_json, status) tuple on rejection, else None."""
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        return {"error": "delivery_url must be http or https"}, 400
    if scheme == "http" and not _http_sink_allowed():
        return {
            "error": "https_required",
            "detail": (
                "delivery_url must use https. Set SENTRY_ALLOW_HTTP_WEBHOOKS=true "
                "to relax this in dev / CI; production refuses the opt-out."
            ),
        }, 400
    try:
        ssrf_guard.assert_url_safe(url)
    except ssrf_guard.SsrfRejected as exc:
        return {"error": "private_destination", "detail": str(exc)}, 400
    return None


def _scope_dump(scope_model):
    return scope_model.model_dump(exclude_none=True) if scope_model is not None else {}


def _row_to_listing(row):
    return {
        "channel_id": row.channel_id,
        "display_name": row.display_name,
        "delivery_url": row.delivery_url,
        "sku_scope": row.sku_scope,
        "transform": row.transform,
        "status": row.status,
        "pause_reason": row.pause_reason,
        "rate_limit_per_second": row.rate_limit_per_second,
        "batch_size": row.batch_size,
        "debounce_seconds": row.debounce_seconds,
        "pending_ceiling": row.pending_ceiling,
        "dlq_ceiling": row.dlq_ceiling,
        "last_published_at": (
            row.last_published_at.isoformat() if row.last_published_at else None
        ),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "item_count": int(getattr(row, "item_count", 0) or 0),
        "dirty_count": int(getattr(row, "dirty_count", 0) or 0),
        "dlq_count": int(getattr(row, "dlq_count", 0) or 0),
    }


_LIST_SELECT = """
    SELECT c.*,
           COALESCE(s.total, 0) AS item_count,
           COALESCE(s.dirty, 0) AS dirty_count,
           COALESCE(s.dlq,   0) AS dlq_count
      FROM channels c
      LEFT JOIN (
            SELECT channel_id,
                   COUNT(*)                                            AS total,
                   COUNT(*) FILTER (WHERE current_version > last_version
                                      AND dlq = FALSE)                 AS dirty,
                   COUNT(*) FILTER (WHERE dlq = TRUE)                  AS dlq
              FROM channel_availability
             GROUP BY channel_id
      ) s ON s.channel_id = c.channel_id
"""


@admin_bp.route("/channels", methods=["POST"])
@require_auth
@require_admin_or_page_permission(_PAGE)
@limiter.limit("60 per minute")
@validate_body(CreateChannelRequest)
@with_db
def create_channel(validated):
    sink_error = _validate_sink(validated.delivery_url)
    if sink_error is not None:
        return jsonify(sink_error[0]), sink_error[1]

    exists = g.db.execute(
        text("SELECT 1 FROM channels WHERE channel_id = :c"),
        {"c": validated.channel_id},
    ).fetchone()
    if exists:
        return jsonify({"error": "channel_id_exists"}), 409

    scope_dump = _scope_dump(validated.sku_scope)
    transform_dump = _scope_dump(validated.transform)
    g.db.execute(
        text(
            """
            INSERT INTO channels (
                channel_id, display_name, delivery_url, sku_scope, transform,
                rate_limit_per_second, batch_size, debounce_seconds,
                pending_ceiling, dlq_ceiling, created_by, external_id
            ) VALUES (
                :channel_id, :display_name, :delivery_url,
                CAST(:sku_scope AS jsonb), CAST(:transform AS jsonb),
                :rate, :batch, :debounce, :pending_ceiling, :dlq_ceiling,
                :created_by, :external_id
            )
            """
        ),
        {
            "channel_id": validated.channel_id,
            "display_name": validated.display_name,
            "delivery_url": validated.delivery_url,
            "sku_scope": validated.sku_scope.model_dump_json(exclude_none=True),
            "transform": validated.transform.model_dump_json(exclude_none=True),
            "rate": validated.rate_limit_per_second,
            "batch": validated.batch_size,
            "debounce": validated.debounce_seconds,
            "pending_ceiling": validated.pending_ceiling,
            "dlq_ceiling": validated.dlq_ceiling,
            "created_by": g.current_user["username"],
            "external_id": str(uuid.uuid4()),
        },
    )

    # Materialize the initial snapshot so the operator sees availability populate
    # and the daemon has dirty rows to publish on its next cycle.
    recompute_channel(g.db, validated.channel_id, scope_dump)

    write_audit_log(
        g.db,
        action_type=ACTION_CHANNEL_CREATE,
        entity_type="CHANNEL",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={
            "channel_id": validated.channel_id,
            "display_name": validated.display_name,
            "delivery_url": validated.delivery_url,
            "sku_scope": scope_dump,
            "transform": transform_dump,
            "rate_limit_per_second": validated.rate_limit_per_second,
            "batch_size": validated.batch_size,
            "debounce_seconds": validated.debounce_seconds,
        },
    )
    g.db.commit()
    return jsonify({"channel_id": validated.channel_id, "status": "active"}), 201


@admin_bp.route("/channels", methods=["GET"])
@require_auth
@require_admin_or_page_permission(_PAGE)
@with_db
def list_channels():
    rows = g.db.execute(
        text(_LIST_SELECT + " WHERE c.status != 'revoked' ORDER BY c.created_at DESC")
    ).fetchall()
    return jsonify({"channels": [_row_to_listing(r) for r in rows]})


@admin_bp.route("/channels/<channel_id>", methods=["GET"])
@require_auth
@require_admin_or_page_permission(_PAGE)
@with_db
def get_channel(channel_id):
    row = g.db.execute(
        text(_LIST_SELECT + " WHERE c.channel_id = :c"), {"c": channel_id}
    ).fetchone()
    if row is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_row_to_listing(row))


@admin_bp.route("/channels/<channel_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission(_PAGE)
@limiter.limit("60 per minute")
@validate_body(UpdateChannelRequest)
@with_db
def update_channel(validated, channel_id):
    current = g.db.execute(
        text("SELECT * FROM channels WHERE channel_id = :c"), {"c": channel_id}
    ).fetchone()
    if current is None:
        return jsonify({"error": "not_found"}), 404
    if current.status == "revoked":
        return jsonify({"error": "channel_revoked"}), 409

    if validated.delivery_url is not None:
        sink_error = _validate_sink(validated.delivery_url)
        if sink_error is not None:
            return jsonify(sink_error[0]), sink_error[1]

    set_clauses = []
    params = {"c": channel_id}
    diff = {}
    scope_changed = False

    def _record(column, before, after):
        diff[column] = {"before": before, "after": after}

    def _scalar(field, column=None):
        column = column or field
        new = getattr(validated, field)
        if new is not None and new != getattr(current, column):
            set_clauses.append(f"{column} = :{column}")
            params[column] = new
            _record(column, getattr(current, column), new)

    _scalar("display_name")
    _scalar("delivery_url")
    _scalar("rate_limit_per_second")
    _scalar("batch_size")
    _scalar("debounce_seconds")
    _scalar("pending_ceiling")
    _scalar("dlq_ceiling")

    # status: only active/paused reach here (schema-gated). Pausing keeps the
    # config; resuming clears any pause_reason.
    if validated.status is not None and validated.status != current.status:
        set_clauses.append("status = :status")
        params["status"] = validated.status
        if validated.status == "active":
            set_clauses.append("pause_reason = NULL")
        else:
            set_clauses.append("pause_reason = 'manual'")
        _record("status", current.status, validated.status)

    if validated.sku_scope is not None:
        new_scope = _scope_dump(validated.sku_scope)
        if new_scope != (current.sku_scope or {}):
            set_clauses.append("sku_scope = CAST(:sku_scope AS jsonb)")
            params["sku_scope"] = validated.sku_scope.model_dump_json(exclude_none=True)
            _record("sku_scope", current.sku_scope or {}, new_scope)
            scope_changed = True

    if validated.transform is not None:
        new_transform = _scope_dump(validated.transform)
        if new_transform != (current.transform or {}):
            set_clauses.append("transform = CAST(:transform AS jsonb)")
            params["transform"] = validated.transform.model_dump_json(exclude_none=True)
            _record("transform", current.transform or {}, new_transform)

    if not set_clauses:
        return jsonify({"channel_id": channel_id, "updated_fields": []})

    set_clauses.append("updated_at = NOW()")
    g.db.execute(
        text(f"UPDATE channels SET {', '.join(set_clauses)} WHERE channel_id = :c"),
        params,
    )

    # A scope change redraws the in-scope item set. Drop the materialization and
    # re-seed so de-scoped items stop tracking and newly-in-scope items publish.
    if scope_changed:
        g.db.execute(
            text("DELETE FROM channel_availability WHERE channel_id = :c"),
            {"c": channel_id},
        )
        recompute_channel(g.db, channel_id, _scope_dump(validated.sku_scope))

    write_audit_log(
        g.db,
        action_type=ACTION_CHANNEL_UPDATE,
        entity_type="CHANNEL",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={"channel_id": channel_id, "diff": diff},
    )
    g.db.commit()
    return jsonify({"channel_id": channel_id, "updated_fields": sorted(diff.keys())})


@admin_bp.route("/channels/<channel_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission(_PAGE)
@with_db
def delete_channel(channel_id):
    current = g.db.execute(
        text("SELECT status FROM channels WHERE channel_id = :c"), {"c": channel_id}
    ).fetchone()
    if current is None:
        return jsonify({"error": "not_found"}), 404
    if current.status == "revoked":
        return jsonify({"channel_id": channel_id, "status": "revoked"})

    # Soft delete: the publisher skips non-active channels, and the row + its
    # config snapshot survive for forensics. channel_availability rows stay
    # (harmless) until a hard cleanup.
    g.db.execute(
        text(
            "UPDATE channels SET status = 'revoked', pause_reason = NULL, "
            "updated_at = NOW() WHERE channel_id = :c"
        ),
        {"c": channel_id},
    )
    write_audit_log(
        g.db,
        action_type=ACTION_CHANNEL_DELETE,
        entity_type="CHANNEL",
        entity_id=0,
        user_id=g.current_user["username"],
        warehouse_id=None,
        details={"channel_id": channel_id},
    )
    g.db.commit()
    return jsonify({"channel_id": channel_id, "status": "revoked"})


@admin_bp.route("/channels/<channel_id>/dlq", methods=["GET"])
@require_auth
@require_admin_or_page_permission(_PAGE)
@with_db
def channel_dlq(channel_id):
    rows = g.db.execute(
        text(
            """
            SELECT ca.item_id, i.sku, ca.available_qty, ca.attempt_count,
                   ca.last_error,
                   ca.last_published_at, ca.updated_at
              FROM channel_availability ca
              JOIN items i ON i.item_id = ca.item_id
             WHERE ca.channel_id = :c AND ca.dlq = TRUE
             ORDER BY ca.updated_at DESC
             LIMIT 500
            """
        ),
        {"c": channel_id},
    ).fetchall()
    return jsonify(
        {
            "channel_id": channel_id,
            "parked": [
                {
                    "item_id": r.item_id,
                    "sku": r.sku,
                    "available_qty": r.available_qty,
                    "attempt_count": r.attempt_count,
                    "last_error": r.last_error,
                    "last_published_at": (
                        r.last_published_at.isoformat() if r.last_published_at else None
                    ),
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ],
        }
    )
