"""POS admin endpoints -- feed admin/src/pages/POSActivity.jsx, the retail
revenue dashboard: today's KPIs, a today-vs-yesterday cumulative pace
curve, a this-week-vs-last-week daily trend, channel + tender splits, and
the filterable order list.

Data model:
- Amount: ``sales_orders.order_total`` -- signed dollars (sales positive,
  refunds negative), written by the POS checkout/refund path. Every
  figure sums this one column.
- Channel: ``sales_orders.order_origin`` -- the POS client writes "POS"
  for a counter sale and "Phone Order" for a phone-order checkout (first
  class column, no join). Matched case-insensitively on "phone".
- Tender: ``audit_log.details->>'payment_method'`` on the POS_CHECKOUT
  row (card / cash / split); the only figure that needs the audit join.
- "Today" is the store-local (America/Denver) day off ``created_at``;
  POS orders do not populate ``order_date``.

Endpoints:
    GET /api/admin/pos/sales-orders   -- paginated POS SO list w/ filters
    GET /api/admin/pos/summary        -- KPIs + pace + weekly + splits

Both gated by the pos-activity page permission.
"""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import g, jsonify, request
from sqlalchemy import text

from constants import ACTION_POS_CHECKOUT, ACTION_POS_REFUND
from middleware.auth_middleware import require_admin_or_page_permission, require_auth
from middleware.db import with_db
from routes.admin import admin_bp

STORE_TZ = "America/Denver"
_TZ = ZoneInfo(STORE_TZ)

# A store-local calendar date -> the UTC instant of its local midnight,
# used as a half-open [start, end) bound against created_at (timestamptz).
_BOUND = "(CAST(:%s AS timestamp) AT TIME ZONE '" + STORE_TZ + "')"
_PHONE = "lower(coalesce(so.order_origin, '')) LIKE '%%phone%%'"


def _cents(value) -> int:
    """Decimal dollars -> integer cents. None -> 0."""
    if value is None:
        return 0
    return int(round(float(value) * 100))


# ---------------------------------------------------------------------------
# GET /api/admin/pos/sales-orders
# ---------------------------------------------------------------------------


@admin_bp.route("/pos/sales-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("pos-activity")
@with_db
def list_pos_sales_orders():
    """Paginated POS sales orders. Amount + date off sales_orders;
    terminal / cashier / tender from the POS audit row (checkout for
    sales, refund for refunds)."""
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    terminal_id = request.args.get("terminal_id")
    cashier_id = request.args.get("cashier_id")
    so_status = request.args.get("status")
    order_type = request.args.get("order_type", "sale").lower()
    channel = request.args.get("channel")  # 'counter' | 'phone' | None

    where = ["so.order_source = :pos_source"]
    params = {
        "pos_source": "pos",
        "checkout_action": ACTION_POS_CHECKOUT,
        "refund_action": ACTION_POS_REFUND,
    }
    if order_type in ("sale", "refund"):
        where.append("so.order_type = :order_type")
        params["order_type"] = order_type
    elif order_type != "all":
        where.append("so.order_type = :order_type")
        params["order_type"] = "sale"
    if channel == "phone":
        where.append(_PHONE)
    elif channel == "counter":
        where.append("NOT " + _PHONE)
    date_arg = request.args.get("date")
    if date_arg:
        try:
            datetime.strptime(date_arg, "%Y-%m-%d")
            where.append(
                f"so.created_at >= {_BOUND % 'pos_date'} "
                f"AND so.created_at < ((CAST(:pos_date AS timestamp) "
                f"+ interval '1 day') AT TIME ZONE '{STORE_TZ}')"
            )
            params["pos_date"] = date_arg
        except ValueError:
            pass
    if so_status:
        where.append("so.status = :so_status")
        params["so_status"] = so_status
    if terminal_id:
        where.append("al.details->>'terminal_id' = :terminal_id")
        params["terminal_id"] = terminal_id
    if cashier_id:
        where.append("al.user_id = :cashier_id")
        params["cashier_id"] = cashier_id

    where_sql = "WHERE " + " AND ".join(where)
    join_sql = (
        "LEFT JOIN audit_log al ON al.entity_type = 'SO' "
        "AND al.entity_id = so.so_id "
        "AND al.action_type IN (:checkout_action, :refund_action)"
    )

    total = g.db.execute(
        text(f"SELECT COUNT(DISTINCT so.so_id) FROM sales_orders so {join_sql} {where_sql}"),
        params,
    ).scalar()
    pages = max(1, math.ceil((total or 0) / per_page))
    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page

    rows = g.db.execute(
        text(
            f"""
            SELECT DISTINCT ON (so.so_id)
                   so.so_id, so.so_number, so.status, so.order_type,
                   so.created_at, so.customer_name, so.customer_phone,
                   so.external_txn_ref, so.warehouse_id, so.order_total,
                   so.order_origin,
                   al.user_id AS cashier_id,
                   al.details->>'terminal_id' AS terminal_id,
                   al.details->>'payment_method' AS payment_method
              FROM sales_orders so {join_sql}
              {where_sql}
             ORDER BY so.so_id DESC, al.created_at DESC
             LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).fetchall()

    return jsonify({
        "sales_orders": [
            {
                "so_id": r.so_id,
                "so_number": r.so_number,
                "status": r.status,
                "order_type": r.order_type,
                "channel": "Phone Order" if (r.order_origin or "").lower().find("phone") >= 0 else "POS",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "customer_name": r.customer_name,
                "customer_phone": r.customer_phone,
                "external_txn_ref": r.external_txn_ref,
                "warehouse_id": r.warehouse_id,
                "total_cents": _cents(r.order_total),
                "cashier_id": r.cashier_id,
                "terminal_id": r.terminal_id,
                "payment_method": r.payment_method,
            }
            for r in rows
        ],
        "total": total or 0,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })


# ---------------------------------------------------------------------------
# GET /api/admin/pos/summary  -- KPIs + pace + weekly + splits
# ---------------------------------------------------------------------------


def _period_kpis(db, start, end):
    """Counts + totals for POS orders in [start, end) store-local dates,
    including the counter/phone channel split (order_origin)."""
    r = db.execute(
        text(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE so.order_type = 'sale')   AS sales_count,
              COUNT(*) FILTER (WHERE so.order_type = 'refund') AS refund_count,
              COALESCE(SUM(so.order_total) FILTER (WHERE so.order_type='sale'),   0) AS sales_total,
              COALESCE(SUM(so.order_total) FILTER (WHERE so.order_type='refund'), 0) AS refund_total,
              COALESCE(SUM(so.order_total), 0) AS net_total,
              COALESCE(SUM(so.order_total) FILTER (WHERE so.order_type='sale' AND {_PHONE}), 0)       AS phone_total,
              COUNT(*) FILTER (WHERE so.order_type='sale' AND {_PHONE})                                AS phone_count,
              COALESCE(SUM(so.order_total) FILTER (WHERE so.order_type='sale' AND NOT {_PHONE}), 0)    AS counter_total,
              COUNT(*) FILTER (WHERE so.order_type='sale' AND NOT {_PHONE})                            AS counter_count
            FROM sales_orders so
            WHERE so.order_source = 'pos'
              AND so.created_at >= {_BOUND % 's'}
              AND so.created_at <  {_BOUND % 'e'}
            """
        ),
        {"s": str(start), "e": str(end)},
    ).fetchone()
    sc = r.sales_count or 0
    sales_total = _cents(r.sales_total)
    return {
        "sales_count": sc,
        "sales_total_cents": sales_total,
        "refund_count": r.refund_count or 0,
        "refund_total_cents": -_cents(r.refund_total),
        "net_cents": _cents(r.net_total),
        "avg_sale_cents": round(sales_total / sc) if sc else 0,
        "counter_cents": _cents(r.counter_total),
        "counter_count": r.counter_count or 0,
        "phone_cents": _cents(r.phone_total),
        "phone_count": r.phone_count or 0,
    }


def _hourly_cumulative(db, start, end):
    """24 cumulative net-revenue cents by store-local hour for [start, end)."""
    rows = db.execute(
        text(
            f"""
            WITH agg AS (
              SELECT EXTRACT(HOUR FROM so.created_at AT TIME ZONE '{STORE_TZ}')::int AS hr,
                     COALESCE(SUM(so.order_total), 0) AS net
                FROM sales_orders so
               WHERE so.order_source = 'pos'
                 AND so.created_at >= {_BOUND % 's'}
                 AND so.created_at <  {_BOUND % 'e'}
               GROUP BY hr
            )
            SELECT h.hr, COALESCE(agg.net, 0) AS net
              FROM generate_series(0, 23) h(hr)
              LEFT JOIN agg ON agg.hr = h.hr
             ORDER BY h.hr
            """
        ),
        {"s": str(start), "e": str(end)},
    ).fetchall()
    out, run = [], 0
    for row in rows:
        run += _cents(row.net)
        out.append({"hour": row.hr, "cents": run})
    return out


def _daily_net(db, start, end):
    """Per-day net cents (store-local date) across [start, end), with
    zero-filled gaps."""
    rows = db.execute(
        text(
            f"""
            WITH agg AS (
              SELECT (so.created_at AT TIME ZONE '{STORE_TZ}')::date AS d,
                     COALESCE(SUM(so.order_total), 0) AS net
                FROM sales_orders so
               WHERE so.order_source = 'pos'
                 AND so.created_at >= {_BOUND % 's'}
                 AND so.created_at <  {_BOUND % 'e'}
               GROUP BY d
            )
            SELECT g::date AS d, COALESCE(agg.net, 0) AS net
              FROM generate_series(CAST(:s AS date), CAST(:e AS date) - 1, interval '1 day') g
              LEFT JOIN agg ON agg.d = g::date
             ORDER BY d
            """
        ),
        {"s": str(start), "e": str(end)},
    ).fetchall()
    return [(row.d, _cents(row.net)) for row in rows]


def _tender_split(db, start, end):
    """Sales revenue grouped by tender (payment_method) for [start, end)."""
    rows = db.execute(
        text(
            f"""
            SELECT COALESCE(al.details->>'payment_method', 'unknown') AS method,
                   COALESCE(SUM(so.order_total), 0) AS total,
                   COUNT(*) AS cnt
              FROM sales_orders so
              JOIN audit_log al
                ON al.entity_type = 'SO' AND al.entity_id = so.so_id
               AND al.action_type = :checkout_action
             WHERE so.order_source = 'pos' AND so.order_type = 'sale'
               AND so.created_at >= {_BOUND % 's'}
               AND so.created_at <  {_BOUND % 'e'}
             GROUP BY method
             ORDER BY total DESC
            """
        ),
        {"s": str(start), "e": str(end), "checkout_action": ACTION_POS_CHECKOUT},
    ).fetchall()
    return [{"method": r.method, "cents": _cents(r.total), "count": r.cnt} for r in rows]


def _anchor_date():
    """The store-local day the dashboard is viewing: ?date=YYYY-MM-DD,
    defaulting to (and clamped at) today. Returns (anchor, today, is_today)."""
    now_local = datetime.now(_TZ)
    today = now_local.date()
    anchor = today
    arg = request.args.get("date")
    if arg:
        try:
            anchor = datetime.strptime(arg, "%Y-%m-%d").date()
        except ValueError:
            anchor = today
    if anchor > today:
        anchor = today
    return anchor, today, anchor == today


@admin_bp.route("/pos/summary", methods=["GET"])
@require_auth
@require_admin_or_page_permission("pos-activity")
@with_db
def pos_summary():
    anchor, today_actual, is_today = _anchor_date()
    # A past day is complete (cap the pace curve at midnight); today caps at now.
    cur_hour = datetime.now(_TZ).hour if is_today else 23

    today = anchor                                          # the selected day
    yesterday = anchor - timedelta(days=1)
    week_start = anchor - timedelta(days=anchor.weekday())  # Monday of its week
    last_week_start = week_start - timedelta(days=7)
    next_week_start = week_start + timedelta(days=7)

    today_kpis = _period_kpis(g.db, today, today + timedelta(days=1))
    yest_kpis = _period_kpis(g.db, yesterday, today)

    pace_today = _hourly_cumulative(g.db, today, today + timedelta(days=1))
    pace_yest = _hourly_cumulative(g.db, yesterday, today)
    # Apples-to-apples: today's net so far vs yesterday by the same hour.
    yest_by_now = pace_yest[cur_hour]["cents"] if cur_hour < 24 else pace_yest[-1]["cents"]
    vs_yesterday_cents = today_kpis["net_cents"] - yest_by_now

    daily = _daily_net(g.db, last_week_start, next_week_start)  # 14 rows
    by_date = {d: c for d, c in daily}

    def _weekrow(monday):
        days = []
        total = 0
        for i in range(7):
            d = monday + timedelta(days=i)
            cents = by_date.get(d, 0)
            total += cents
            days.append({
                "date": d.isoformat(),
                "dow": i,  # 0=Mon
                "net_cents": cents,
                "is_today": d == today,
                "is_future": d > today,
            })
        return days, total

    this_days, this_total = _weekrow(week_start)
    last_days, last_total = _weekrow(last_week_start)

    tenders = _tender_split(g.db, today, today + timedelta(days=1))

    terminals = g.db.execute(
        text(
            """
            SELECT DISTINCT al.details->>'terminal_id' AS terminal_id
              FROM audit_log al
             WHERE al.action_type IN (:checkout_action, :refund_action)
               AND al.created_at >= (now() - INTERVAL '24 hours')
               AND al.details->>'terminal_id' IS NOT NULL
             ORDER BY terminal_id
            """
        ),
        {"checkout_action": ACTION_POS_CHECKOUT, "refund_action": ACTION_POS_REFUND},
    ).fetchall()

    return jsonify({
        "tz": STORE_TZ,
        "date": anchor.isoformat(),
        "is_today": is_today,
        "current_hour": cur_hour,
        "today": today_kpis,
        "yesterday": yest_kpis,
        "vs_yesterday_cents": vs_yesterday_cents,
        "pace": {"today": pace_today, "yesterday": pace_yest},
        "week": {
            "this": this_days,
            "last": last_days,
            "this_total_cents": this_total,
            "last_total_cents": last_total,
        },
        "tenders": tenders,
        "active_terminals": [t.terminal_id for t in terminals if t.terminal_id],
    })
