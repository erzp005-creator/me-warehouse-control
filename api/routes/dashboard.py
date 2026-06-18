"""Operations dashboard API (v1.8.0 #297).

Endpoints:

- GET  /api/v1/dashboard/productivity     per-user metrics for a date range
- GET  /api/v1/dashboard/preferences      user's chart_order + defaults
- PUT  /api/v1/dashboard/preferences      upsert preferences row
- GET  /api/v1/dashboard/received         per-PO receipts for a date range
- GET  /api/v1/dashboard/shipping-health  per-channel outbound health

Auth: cookie + ADMIN role for productivity (operators with admin
visibility); preferences are per-user with the user_id derived
from g.current_user (never from the request body). received and
shipping-health require a logged-in admin session.
"""

import json
from datetime import date, timedelta
from typing import List, Optional

from flask import Blueprint, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text

from middleware.auth_middleware import require_auth, require_role
from middleware.db import with_db
from services.productivity_service import (
    DASHBOARD_EVENTS,
    get_productivity,
)


dashboard_bp = Blueprint("dashboard", __name__)


_VALID_CHART_ORDER_KEYS = {slug for (slug, _, _) in DASHBOARD_EVENTS}
_VALID_DEFAULT_RANGES = {
    "today", "yesterday", "last_7d", "last_30d", "custom",
}
_VALID_DEFAULT_VIEWS = {"charts", "table"}

_MAX_RANGE_DAYS = 90


# ============================================================
# Productivity
# ============================================================


class _ProductivityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    warehouse_id: int = Field(..., gt=0)

    @field_validator("end")
    @classmethod
    def _end_not_before_start(cls, v, info):
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError("end must be on or after start")
        return v


@dashboard_bp.route("/productivity", methods=["GET"])
@require_auth
@require_role("ADMIN")
@with_db
def productivity():
    try:
        params = _ProductivityQuery.model_validate({
            "start": request.args.get("start"),
            "end": request.args.get("end"),
            "warehouse_id": request.args.get("warehouse_id", type=int),
        })
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    span_days = (params.end - params.start).days + 1
    if span_days > _MAX_RANGE_DAYS:
        return jsonify({
            "error": "range_too_large",
            "max_range_days": _MAX_RANGE_DAYS,
            "requested_days": span_days,
        }), 422

    # The endpoint accepts inclusive [start, end] dates; the SQL uses
    # half-open [start, end+1day) so the index range scan is clean.
    start_dt = params.start
    end_dt_exclusive = params.end + timedelta(days=1)
    payload = get_productivity(
        g.db, params.warehouse_id, start_dt, end_dt_exclusive,
    )
    # Fix up the response range to mirror what the operator asked for
    # (the cache key uses the SQL-level half-open form).
    payload["range"] = {
        "start": params.start.isoformat(),
        "end": params.end.isoformat(),
    }
    return jsonify(payload)


# ============================================================
# Preferences
# ============================================================


class _PreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_order: Optional[List[str]] = None
    default_range: Optional[str] = None
    default_view: Optional[str] = None

    @field_validator("chart_order")
    @classmethod
    def _check_chart_order(cls, v):
        if v is None:
            return v
        unknown = [k for k in v if k not in _VALID_CHART_ORDER_KEYS]
        if unknown:
            raise ValueError(
                f"chart_order has unknown keys: {unknown!r}. "
                f"Valid: {sorted(_VALID_CHART_ORDER_KEYS)}"
            )
        if len(set(v)) != len(v):
            raise ValueError("chart_order must not contain duplicates")
        return v

    @field_validator("default_range")
    @classmethod
    def _check_default_range(cls, v):
        if v is not None and v not in _VALID_DEFAULT_RANGES:
            raise ValueError(
                f"default_range must be one of {sorted(_VALID_DEFAULT_RANGES)}"
            )
        return v

    @field_validator("default_view")
    @classmethod
    def _check_default_view(cls, v):
        if v is not None and v not in _VALID_DEFAULT_VIEWS:
            raise ValueError(
                f"default_view must be one of {sorted(_VALID_DEFAULT_VIEWS)}"
            )
        return v


def _resolve_user_id():
    """user_id derived from g.current_user (CSRF / IDOR protection per
    plan section 5.2). Returns None when unauthenticated."""
    user = getattr(g, "current_user", None) or {}
    return user.get("user_id")


def _row_to_dict(row) -> dict:
    return {
        "chart_order": (
            list(row.chart_order) if row.chart_order is not None
            else [slug for (slug, _, _) in DASHBOARD_EVENTS]
        ),
        "default_range": row.default_range,
        "default_view": row.default_view,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@dashboard_bp.route("/preferences", methods=["GET"])
@require_auth
@with_db
def get_preferences():
    user_id = _resolve_user_id()
    if user_id is None:
        return jsonify({"error": "unauthenticated"}), 401
    row = g.db.execute(
        text(
            "SELECT chart_order, default_range, default_view, updated_at "
            "  FROM user_dashboard_preferences WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()
    if row is None:
        return jsonify({
            "chart_order": [slug for (slug, _, _) in DASHBOARD_EVENTS],
            "default_range": "today",
            "default_view": "charts",
            "updated_at": None,
        })
    return jsonify(_row_to_dict(row))


@dashboard_bp.route("/preferences", methods=["PUT"])
@require_auth
@with_db
def put_preferences():
    user_id = _resolve_user_id()
    if user_id is None:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        body = _PreferencesBody.model_validate(request.get_json() or {})
    except ValidationError as exc:
        return jsonify({
            "error": "validation_error",
            "details": exc.errors(include_url=False, include_context=False),
        }), 422

    # Build the UPSERT dynamically so unset fields keep their existing
    # value (rather than reverting to defaults).
    fields = {}
    if body.chart_order is not None:
        fields["chart_order"] = body.chart_order
    if body.default_range is not None:
        fields["default_range"] = body.default_range
    if body.default_view is not None:
        fields["default_view"] = body.default_view
    if not fields:
        return jsonify({"error": "no_fields_to_update"}), 422

    # Build the INSERT column list (always includes user_id) + the
    # ON CONFLICT updates for the fields the body actually set. Empty
    # column case (only user_id) is handled by the no_fields_to_update
    # guard above.
    insert_cols = ["user_id"] + list(fields.keys())
    insert_placeholders = ["(:uid)"] + [f"(:{c})" for c in fields.keys()]

    # Use psycopg2.extras.Json wrapping for chart_order JSONB binding.
    from psycopg2.extras import Json
    params = {"uid": user_id}
    for col, val in fields.items():
        params[col] = Json(val) if col == "chart_order" else val

    # Construct the SQL: INSERT ... ON CONFLICT (user_id) DO UPDATE SET ...
    set_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in fields.keys()
    ) + ", updated_at = NOW()"
    cols_sql = ", ".join(insert_cols)
    vals_sql = ", ".join(f":{c}" if c != "user_id" else ":uid"
                         for c in insert_cols)
    g.db.execute(
        text(
            f"INSERT INTO user_dashboard_preferences ({cols_sql}) "
            f"VALUES ({vals_sql}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {set_clause}"
        ),
        params,
    )
    g.db.commit()

    row = g.db.execute(
        text(
            "SELECT chart_order, default_range, default_view, updated_at "
            "  FROM user_dashboard_preferences WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).fetchone()
    return jsonify(_row_to_dict(row))


# ============================================================
# Shared range parsing
# ============================================================


def _parse_dashboard_range():
    """Parse the shared start/end query params (inclusive YYYY-MM-DD dates,
    defaulting to today). Returns (start_date, end_date, error_response);
    on success error_response is None. Callers add one day to end for the
    half-open SQL window."""
    start_str = request.args.get("start") or date.today().isoformat()
    end_str = request.args.get("end") or date.today().isoformat()
    try:
        start_dt = date.fromisoformat(start_str)
        end_dt = date.fromisoformat(end_str)
    except ValueError:
        return None, None, (jsonify({"error": "start/end must be YYYY-MM-DD"}), 422)
    if end_dt < start_dt:
        return None, None, (jsonify({"error": "end must be on or after start"}), 422)
    return start_dt, end_dt, None


# ============================================================
# Received (per-PO receipts for a date range)
# ============================================================


@dashboard_bp.route("/received", methods=["GET"])
@require_auth
@with_db
def received():
    """Per-PO receipts for a date range.

    Aggregates audit_log RECEIVE rows in [start, end] (inclusive dates,
    treated as half-open [start, end+1day) in SQL for clean index scans).
    Per PO the response carries the PO header, what was received in range
    (units + distinct line items + receivers), and when the most recent
    line landed. start / end default to CURRENT_DATE; warehouse_id is
    required.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    start_dt, end_dt, err = _parse_dashboard_range()
    if err is not None:
        return err
    end_exclusive = end_dt + timedelta(days=1)

    rows = g.db.execute(
        text(
            """
            SELECT al.entity_id AS po_id,
                   po.po_number,
                   po.vendor_name,
                   po.status,
                   SUM(COALESCE((al.details->>'quantity')::int, 0)) AS units_received,
                   COUNT(DISTINCT (al.details->>'item_id')::int) AS lines_received,
                   COUNT(*) AS receive_events,
                   ARRAY_AGG(DISTINCT al.user_id) AS receivers,
                   MAX(al.created_at) AS last_received_at
              FROM audit_log al
              JOIN purchase_orders po ON po.po_id = al.entity_id
             WHERE al.action_type = 'RECEIVE'
               AND al.entity_type = 'PO'
               AND al.warehouse_id = :wid
               AND al.created_at >= :start
               AND al.created_at < :end
             GROUP BY al.entity_id, po.po_number, po.vendor_name, po.status
             ORDER BY MAX(al.created_at) DESC
            """
        ),
        {"wid": warehouse_id, "start": start_dt, "end": end_exclusive},
    ).fetchall()

    return jsonify({
        "warehouse_id": warehouse_id,
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "pos": [
            {
                "po_id": r.po_id,
                "po_number": r.po_number,
                "vendor_name": r.vendor_name,
                "status": r.status,
                "units_received": int(r.units_received or 0),
                "lines_received": int(r.lines_received or 0),
                "receive_events": int(r.receive_events or 0),
                "receivers": list(r.receivers or []),
                "last_received_at": (
                    r.last_received_at.isoformat() if r.last_received_at else None
                ),
            }
            for r in rows
        ],
    })


# ============================================================
# Shipping Health (Marketplace Health)
# ============================================================


_DASHBOARD_BUBBLE_ORIGINS_KEY = "dashboard_bubble_origins"


def _bubble_origins(db):
    """The admin-defined Marketplace Health bubble set.

    order_origin is a free-text sales_orders column: the inbound mapping
    writes whatever label the upstream channel uses, and the POS
    phone-order flow writes the literal "Phone Order". Rather than hardcode
    a channel list, the bubble set is configured per deployment in
    app_settings.dashboard_bubble_origins -- a JSON array of
    {"origin": <order_origin value>, "label": <display name>}. An admin
    adds one entry per channel they want a health bubble for. Unset, empty,
    or malformed yields no bubbles: this dashboard never surfaces channels
    implicitly from the data, so a fresh install shows nothing until an
    operator opts a channel in. origin values are matched verbatim.
    """
    row = db.execute(
        text("SELECT value FROM app_settings WHERE key = :k"),
        {"k": _DASHBOARD_BUBBLE_ORIGINS_KEY},
    ).fetchone()
    if not row or not row.value:
        return []
    try:
        parsed = json.loads(row.value)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    seen = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin")
        if not isinstance(origin, str) or not origin or origin in seen:
            continue
        seen.add(origin)
        label = entry.get("label")
        out.append({
            "origin": origin,
            "label": label if isinstance(label, str) and label else origin,
        })
    return out


@dashboard_bp.route("/shipping-health", methods=["GET"])
@require_auth
@with_db
def shipping_health():
    """Per-order-origin outbound health for the Marketplace Health view.

    Returns one row per configured channel carrying:
      * orders_received    (created_at within [start, end])
      * orders_shipped     (status SHIPPED, shipped_at within range)
      * need_to_ship_today (ship_by_date <= today, still open) -- always
                           current-state, since the desk only acts on
                           today's backlog
      * orders             up to 200 SOs behind the ship-today count, so a
                           click on the bubble renders the drill-down
                           without a second round-trip

    The configured channel set is materialised as a CTE and LEFT JOINed so
    every bubble renders even when its count is zero (a green check is
    information, not an empty grid). With no channels configured the
    response is ``by_source: []``. start / end default to CURRENT_DATE;
    warehouse_id is required; received / shipped honour the range. A
    SHIPPED, CANCELLED (refunds cancel the original), or FRAUD_REVIEW order
    is out of the ship-today pool.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    if not warehouse_id:
        return jsonify({"error": "warehouse_id is required"}), 422
    start_dt, end_dt, err = _parse_dashboard_range()
    if err is not None:
        return err
    end_exclusive = end_dt + timedelta(days=1)

    bubbles = _bubble_origins(g.db)
    if not bubbles:
        return jsonify({
            "warehouse_id": warehouse_id,
            "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "by_source": [],
        })
    origins = [b["origin"] for b in bubbles]
    label_by_origin = {b["origin"]: b["label"] for b in bubbles}

    rows = g.db.execute(
        text(
            """
            WITH origins AS (
                SELECT UNNEST(CAST(:origins AS text[])) AS order_origin
            )
            SELECT o.order_origin,
                   COUNT(so.so_id) FILTER (
                       WHERE so.created_at >= :start
                         AND so.created_at < :end
                   ) AS orders_received,
                   COUNT(so.so_id) FILTER (
                       WHERE so.status = 'SHIPPED'
                         AND so.shipped_at >= :start
                         AND so.shipped_at < :end
                   ) AS orders_shipped,
                   COUNT(so.so_id) FILTER (
                       WHERE so.ship_by_date IS NOT NULL
                         AND so.ship_by_date <= CURRENT_DATE
                         AND so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
                   ) AS need_to_ship_today
              FROM origins o
              LEFT JOIN sales_orders so
                ON so.order_origin = o.order_origin
               AND so.warehouse_id = :wid
             GROUP BY o.order_origin
             ORDER BY o.order_origin
            """
        ),
        {
            "wid": warehouse_id,
            "start": start_dt,
            "end": end_exclusive,
            "origins": origins,
        },
    ).fetchall()

    # Drill-down: the SO list behind every non-zero need_to_ship_today
    # bubble. Capped per-origin in Python (the count is bounded to
    # len(origins) x 200, so a plain order-and-truncate beats ROW_NUMBER()
    # partition work). Earliest ship_by_date first so the desk sees the
    # oldest debt at the top of the modal.
    detail_rows = g.db.execute(
        text(
            """
            SELECT so.order_origin, so.so_id, so.so_number,
                   so.customer_name, so.status, so.ship_by_date
              FROM sales_orders so
             WHERE so.warehouse_id = :wid
               AND so.order_origin = ANY(CAST(:origins AS text[]))
               AND so.ship_by_date IS NOT NULL
               AND so.ship_by_date <= CURRENT_DATE
               AND so.status NOT IN ('SHIPPED', 'CANCELLED', 'FRAUD_REVIEW')
             ORDER BY so.ship_by_date ASC, so.so_id ASC
            """
        ),
        {"wid": warehouse_id, "origins": origins},
    ).fetchall()
    orders_by_origin = {o: [] for o in origins}
    for r in detail_rows:
        bucket = orders_by_origin.get(r.order_origin)
        if bucket is None or len(bucket) >= 200:
            continue
        bucket.append({
            "so_id": r.so_id,
            "so_number": r.so_number,
            "customer_name": r.customer_name,
            "status": r.status,
            "ship_by_date": r.ship_by_date.isoformat() if r.ship_by_date else None,
        })

    return jsonify({
        "warehouse_id": warehouse_id,
        "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "by_source": [
            {
                "order_origin": r.order_origin,
                "label": label_by_origin.get(r.order_origin, r.order_origin),
                "orders_received": int(r.orders_received or 0),
                "orders_shipped": int(r.orders_shipped or 0),
                "need_to_ship_today": int(r.need_to_ship_today or 0),
                "orders": orders_by_origin.get(r.order_origin, []),
            }
            for r in rows
        ],
    })
