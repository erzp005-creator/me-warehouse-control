"""POS Activity dashboard endpoints: /api/admin/pos/summary + /sales-orders.

Covers the wired-up KPIs and the revamp's splits/trends:
- "today" scoped to the store-local (America/Denver) day off created_at
  (POS orders never populate order_date).
- net/sales/refund summed from the signed sales_orders.order_total column.
- channel split (counter vs phone) off sales_orders.order_origin
  ("POS" vs "Phone Order").
- tender split off the POS_CHECKOUT audit row's payment_method.
- cumulative pace, vs-yesterday, and this-week/last-week totals.

Deltas are measured around the seeded rows so seed-data POS orders don't
make the assertions brittle.
"""

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db_test_context import get_raw_connection

_TZ = ZoneInfo("America/Denver")

_SO_SQL = """
    INSERT INTO sales_orders
      (so_number, warehouse_id, status, external_id, order_source,
       order_type, order_total, order_origin, created_at)
    VALUES (%s, 1, 'SHIPPED', gen_random_uuid(), 'pos', %s, %s, %s,
            ((date_trunc('day', now() AT TIME ZONE 'America/Denver')
              + INTERVAL '13 hours') AT TIME ZONE 'America/Denver')
            - make_interval(days => %s))
    RETURNING so_id
"""

_AUDIT_SQL = """
    INSERT INTO audit_log
      (action_type, entity_type, entity_id, user_id, warehouse_id, details)
    VALUES (%s, 'SO', %s, 'admin', 1, %s::jsonb)
"""


def _seed(so_number, order_type, total, *, day_offset=0, origin="POS", tender=None, terminal="REG-01"):
    conn = get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(_SO_SQL, (so_number, order_type, total, origin, day_offset))
        so_id = cur.fetchone()[0]
        if tender:
            action = "POS_CHECKOUT" if order_type == "sale" else "POS_REFUND"
            cur.execute(_AUDIT_SQL, (
                action, so_id,
                json.dumps({"terminal_id": terminal, "payment_method": tender,
                            "total_cents": int(round(abs(total) * 100))}),
            ))
        return so_id
    finally:
        cur.close()


def _summary(client, auth_headers):
    return client.get("/api/admin/pos/summary", headers=auth_headers).get_json()


def _tag():
    return uuid.uuid4().hex[:8].upper()


class TestPosSummary:
    def test_today_kpis_and_channel_split(self, client, auth_headers):
        b = _summary(client, auth_headers)["today"]
        tag = _tag()
        _seed(f"POS-{tag}-1", "sale", 10.00, origin="POS")
        _seed(f"POS-{tag}-2", "sale", 25.50, origin="POS")
        _seed(f"POS-{tag}-P", "sale", 60.00, origin="Phone Order")
        _seed(f"POS-{tag}-R", "refund", -5.00, origin="POS")
        a = _summary(client, auth_headers)["today"]

        assert a["sales_count"] - b["sales_count"] == 3
        assert a["sales_total_cents"] - b["sales_total_cents"] == 9550
        assert a["refund_count"] - b["refund_count"] == 1
        assert a["refund_total_cents"] - b["refund_total_cents"] == 500
        assert a["net_cents"] - b["net_cents"] == 9050
        # Channel split: 2 counter ($35.50), 1 phone ($60).
        assert a["counter_count"] - b["counter_count"] == 2
        assert a["counter_cents"] - b["counter_cents"] == 3550
        assert a["phone_count"] - b["phone_count"] == 1
        assert a["phone_cents"] - b["phone_cents"] == 6000

    def test_pace_is_cumulative_and_full_day(self, client, auth_headers):
        d = _summary(client, auth_headers)
        assert len(d["pace"]["today"]) == 24
        assert len(d["pace"]["yesterday"]) == 24
        # Cumulative: non-decreasing, and the last point equals today's net.
        cum = [p["cents"] for p in d["pace"]["today"]]
        assert cum == sorted(cum)
        assert cum[-1] == d["today"]["net_cents"]

    def test_vs_yesterday_uses_same_hour(self, client, auth_headers):
        b = _summary(client, auth_headers)
        tag = _tag()
        _seed(f"POS-{tag}-T", "sale", 40.00)              # today
        _seed(f"POS-{tag}-Y", "sale", 15.00, day_offset=1)  # yesterday
        a = _summary(client, auth_headers)
        # today moved +$40; yesterday-by-same-hour moved +$15 (1pm <= now
        # is assumed during a normal test run) -> vs_yesterday delta +$25.
        assert a["today"]["net_cents"] - b["today"]["net_cents"] == 4000
        assert a["yesterday"]["net_cents"] - b["yesterday"]["net_cents"] == 1500

    def test_weekly_totals(self, client, auth_headers):
        b = _summary(client, auth_headers)["week"]
        wd = datetime.now(_TZ).weekday()
        tag = _tag()
        _seed(f"POS-{tag}-TW", "sale", 50.00, day_offset=0)            # this week
        _seed(f"POS-{tag}-LW", "sale", 80.00, day_offset=wd + 3)        # last week
        a = _summary(client, auth_headers)["week"]
        assert a["this_total_cents"] - b["this_total_cents"] == 5000
        assert a["last_total_cents"] - b["last_total_cents"] == 8000
        assert len(a["this"]) == 7 and len(a["last"]) == 7

    def test_tender_split(self, client, auth_headers):
        before = {t["method"]: t["cents"] for t in _summary(client, auth_headers)["tenders"]}
        tag = _tag()
        _seed(f"POS-{tag}-C", "sale", 30.00, tender="card")
        _seed(f"POS-{tag}-K", "sale", 12.00, tender="cash")
        after = {t["method"]: t["cents"] for t in _summary(client, auth_headers)["tenders"]}
        assert after.get("card", 0) - before.get("card", 0) == 3000
        assert after.get("cash", 0) - before.get("cash", 0) == 1200


class TestDateSelector:
    def test_summary_anchors_on_selected_date(self, client, auth_headers):
        yday = (datetime.now(_TZ).date() - timedelta(days=1)).isoformat()
        before = client.get(f"/api/admin/pos/summary?date={yday}", headers=auth_headers).get_json()
        assert before["date"] == yday
        assert before["is_today"] is False
        b_net = before["today"]["net_cents"]
        _seed(f"POS-{_tag()}-Y", "sale", 33.00, day_offset=1)  # a sale yesterday
        after = client.get(f"/api/admin/pos/summary?date={yday}", headers=auth_headers).get_json()
        assert after["today"]["net_cents"] - b_net == 3300  # shows under the selected day

    def test_list_filters_by_date(self, client, auth_headers):
        yday = (datetime.now(_TZ).date() - timedelta(days=1)).isoformat()
        tag = _tag()
        _seed(f"POS-{tag}-Y", "sale", 5.00, day_offset=1)
        _seed(f"POS-{tag}-T", "sale", 5.00, day_offset=0)
        rows = client.get(
            f"/api/admin/pos/sales-orders?date={yday}&per_page=200", headers=auth_headers,
        ).get_json()["sales_orders"]
        nums = [r["so_number"] for r in rows]
        assert f"POS-{tag}-Y" in nums
        assert f"POS-{tag}-T" not in nums


class TestPosList:
    def test_total_from_order_total_and_channel(self, client, auth_headers):
        tag = _tag()
        _seed(f"POS-{tag}-LIST", "sale", 41.07, origin="Phone Order")
        rows = client.get(
            "/api/admin/pos/sales-orders?order_type=sale&per_page=200",
            headers=auth_headers,
        ).get_json()["sales_orders"]
        mine = next(r for r in rows if r["so_number"] == f"POS-{tag}-LIST")
        assert mine["total_cents"] == 4107
        assert mine["created_at"] is not None
        assert mine["channel"] == "Phone Order"

    def test_channel_filter(self, client, auth_headers):
        tag = _tag()
        _seed(f"POS-{tag}-PH", "sale", 9.00, origin="Phone Order")
        _seed(f"POS-{tag}-CT", "sale", 9.00, origin="POS")
        phone = client.get(
            "/api/admin/pos/sales-orders?order_type=sale&channel=phone&per_page=200",
            headers=auth_headers,
        ).get_json()["sales_orders"]
        nums = [r["so_number"] for r in phone]
        assert f"POS-{tag}-PH" in nums
        assert f"POS-{tag}-CT" not in nums
