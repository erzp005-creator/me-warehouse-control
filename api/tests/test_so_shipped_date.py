"""Shipped Date on the admin SO modal.

shipped_at is stored as a UTC TIMESTAMPTZ (dockd ship writes NOW()).
The admin modal surfaces it as a single company-local calendar date so
the read-only view and the editable date picker can never disagree:

- GET detail returns shipped_date_local: (shipped_at AT TIME ZONE
  COMPANY_TIMEZONE)::date as YYYY-MM-DD, independent of the server's
  UTC offset.
- PUT shipped_at takes a calendar date and anchors it at noon in
  COMPANY_TIMEZONE, so the stored instant always reads back as that
  same date through DST. Empty string clears to NULL. A no-op re-save
  writes no audit row.

COMPANY_TIMEZONE is env-driven (default UTC); these tests pin
America/Denver to assert across both
the MDT (UTC-6, summer) and MST (UTC-7, winter) offsets.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import text as sa_text


@pytest.fixture(autouse=True)
def _pin_company_timezone(monkeypatch):
    """COMPANY_TIMEZONE defaults to UTC and is deployment-configurable.
    These tests assert DST behaviour, so pin a DST-observing zone on
    every module that imported the constant at boot."""
    import constants as _c
    import routes.admin.admin_orders as _ao
    monkeypatch.setattr(_c, "COMPANY_TIMEZONE", "America/Denver")
    monkeypatch.setattr(_ao, "COMPANY_TIMEZONE", "America/Denver")



def _create_so(client, auth_headers):
    sn = f"SO-SHIP-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/admin/sales-orders",
        json={
            "so_number": sn,
            "warehouse_id": 1,
            "lines": [{"item_id": 1, "quantity_ordered": 1}],
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()["sales_order"]["so_id"]


# ----------------------------------------------------------------------
# Pydantic schema surface
# ----------------------------------------------------------------------


class TestPydanticSchemas:
    def test_update_request_accepts_shipped_at_date(self):
        from schemas.sales_orders import UpdateSalesOrderRequest
        req = UpdateSalesOrderRequest(shipped_at="2025-07-14")
        assert req.shipped_at == "2025-07-14"

    def test_update_request_accepts_empty_string_for_clear(self):
        from schemas.sales_orders import UpdateSalesOrderRequest
        req = UpdateSalesOrderRequest(shipped_at="")
        assert req.shipped_at == ""

    def test_update_request_rejects_over_64_chars(self):
        from pydantic import ValidationError
        from schemas.sales_orders import UpdateSalesOrderRequest
        with pytest.raises(ValidationError) as exc:
            UpdateSalesOrderRequest(shipped_at="x" * 65)
        errors = exc.value.errors()
        assert any(e["loc"] == ("shipped_at",) for e in errors)


# ----------------------------------------------------------------------
# GET detail surface
# ----------------------------------------------------------------------


class TestGetDetail:
    def test_detail_returns_shipped_date_local_key(self, client, auth_headers):
        """The key must always be present so the controlled date input
        never sees undefined."""
        get_resp = client.get("/api/admin/sales-orders/1", headers=auth_headers)
        assert get_resp.status_code == 200
        assert "shipped_date_local" in get_resp.get_json()["sales_order"]

    def test_unshipped_order_has_null_shipped_date_local(self, client, auth_headers):
        so_id = _create_so(client, auth_headers)
        body = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        ).get_json()["sales_order"]
        assert body["shipped_date_local"] is None


# ----------------------------------------------------------------------
# Timezone correctness: UTC instant -> company-local date
# ----------------------------------------------------------------------


class TestUtcToLocalDate:
    def test_utc_instant_maps_to_local_date_summer(
        self, client, auth_headers, _db_transaction,
    ):
        """An order shipped 21:00 Mountain on 2025-07-14 is the instant
        2025-07-15T03:00Z. A naive UTC slice would show 2025-07-15; the
        company-local date is 2025-07-14."""
        so_id = _create_so(client, auth_headers)
        _db_transaction.execute(
            sa_text(
                "UPDATE sales_orders SET shipped_at = "
                "'2025-07-15T03:00:00+00'::timestamptz WHERE so_id = :s"
            ),
            {"s": so_id},
        )
        body = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        ).get_json()["sales_order"]
        assert body["shipped_date_local"] == "2025-07-14"

    def test_utc_instant_maps_to_local_date_winter(
        self, client, auth_headers, _db_transaction,
    ):
        """Same boundary in January (MST, UTC-7): 20:00 Mountain on
        2025-01-14 is 2025-01-15T03:00Z; local date is 2025-01-14."""
        so_id = _create_so(client, auth_headers)
        _db_transaction.execute(
            sa_text(
                "UPDATE sales_orders SET shipped_at = "
                "'2025-01-15T03:00:00+00'::timestamptz WHERE so_id = :s"
            ),
            {"s": so_id},
        )
        body = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        ).get_json()["sales_order"]
        assert body["shipped_date_local"] == "2025-01-14"


# ----------------------------------------------------------------------
# PUT round-trip: pick a date, read back the same date; noon anchor
# ----------------------------------------------------------------------


class TestPutRoundTrip:
    def _local_ts(self, db, so_id):
        return db.execute(
            sa_text(
                "SELECT (shipped_at AT TIME ZONE 'America/Denver') AS lts "
                "  FROM sales_orders WHERE so_id = :s"
            ),
            {"s": so_id},
        ).fetchone().lts

    def test_put_date_round_trips_summer(
        self, client, auth_headers, _db_transaction,
    ):
        so_id = _create_so(client, auth_headers)
        put = client.put(
            f"/api/admin/sales-orders/{so_id}",
            json={"shipped_at": "2025-07-14"},
            headers=auth_headers,
        )
        assert put.status_code == 200, put.get_json()
        assert "shipped_at" in put.get_json()["edited_fields"]
        # Reads back as the same calendar date.
        body = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        ).get_json()["sales_order"]
        assert body["shipped_date_local"] == "2025-07-14"
        # Stored anchored at noon company-local (boundary-proof).
        lts = self._local_ts(_db_transaction, so_id)
        assert lts.date().isoformat() == "2025-07-14"
        assert lts.hour == 12

    def test_put_date_round_trips_winter(
        self, client, auth_headers, _db_transaction,
    ):
        so_id = _create_so(client, auth_headers)
        client.put(
            f"/api/admin/sales-orders/{so_id}",
            json={"shipped_at": "2025-01-14"},
            headers=auth_headers,
        )
        body = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        ).get_json()["sales_order"]
        assert body["shipped_date_local"] == "2025-01-14"
        lts = self._local_ts(_db_transaction, so_id)
        assert lts.date().isoformat() == "2025-01-14"
        assert lts.hour == 12

    def test_put_empty_string_clears_to_null(
        self, client, auth_headers, _db_transaction,
    ):
        so_id = _create_so(client, auth_headers)
        client.put(
            f"/api/admin/sales-orders/{so_id}",
            json={"shipped_at": "2025-07-14"},
            headers=auth_headers,
        )
        client.put(
            f"/api/admin/sales-orders/{so_id}",
            json={"shipped_at": ""},
            headers=auth_headers,
        )
        val = _db_transaction.execute(
            sa_text("SELECT shipped_at FROM sales_orders WHERE so_id = :s"),
            {"s": so_id},
        ).fetchone().shipped_at
        assert val is None
        body = client.get(
            f"/api/admin/sales-orders/{so_id}", headers=auth_headers,
        ).get_json()["sales_order"]
        assert body["shipped_date_local"] is None


# ----------------------------------------------------------------------
# Audit / idempotency
# ----------------------------------------------------------------------


class TestAudit:
    def test_put_writes_audit_row_when_changed(
        self, client, auth_headers, _db_transaction,
    ):
        """A real date change writes one SO_HEADER_EDITED audit row
        carrying the old/new values."""
        so_id = _create_so(client, auth_headers)
        resp = client.put(
            f"/api/admin/sales-orders/{so_id}",
            headers=auth_headers,
            json={"shipped_at": "2025-07-14"},
        )
        assert resp.status_code == 200
        rows = _db_transaction.execute(
            sa_text(
                "SELECT details FROM audit_log WHERE entity_type = 'SO' "
                "AND entity_id = :s AND action_type = 'SO_HEADER_EDITED'"
            ),
            {"s": so_id},
        ).fetchall()
        assert any(r.details.get("field_changed") == "shipped_at" for r in rows)

    def test_noop_resave_writes_no_audit(
        self, client, auth_headers, _db_transaction,
    ):
        """Re-sending the same company-local date is change-detected
        server-side: no UPDATE fragment, no audit row, 200 unchanged."""
        so_id = _create_so(client, auth_headers)
        resp = client.put(
            f"/api/admin/sales-orders/{so_id}",
            headers=auth_headers,
            json={"shipped_at": "2025-07-14"},
        )
        assert resp.status_code == 200
        before = _db_transaction.execute(
            sa_text(
                "SELECT count(*) AS n FROM audit_log WHERE entity_type = 'SO' "
                "AND entity_id = :s"
            ),
            {"s": so_id},
        ).fetchone().n
        resp2 = client.put(
            f"/api/admin/sales-orders/{so_id}",
            headers=auth_headers,
            json={"shipped_at": "2025-07-14"},
        )
        assert resp2.status_code == 200
        assert resp2.get_json().get("unchanged") is True
        after = _db_transaction.execute(
            sa_text(
                "SELECT count(*) AS n FROM audit_log WHERE entity_type = 'SO' "
                "AND entity_id = :s"
            ),
            {"s": so_id},
        ).fetchone().n
        assert after == before
