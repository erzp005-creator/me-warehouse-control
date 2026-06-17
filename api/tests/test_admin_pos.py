"""Smoke coverage for the POS Activity admin endpoints. These are
read-only reporting surfaces (paginated POS sales-order list + daily
KPI counters) that LEFT JOIN audit_log for POS_CHECKOUT metadata, so
the value here is confirming the routes register and the SQL runs and
returns the expected shape regardless of how much POS data exists."""


class TestPosActivityAdmin:
    def test_pos_sales_orders_list_shape(self, client, auth_headers):
        resp = client.get("/api/admin/pos/sales-orders", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data["sales_orders"], list)
        for key in ("total", "page", "per_page", "pages"):
            assert key in data

    def test_pos_summary_shape(self, client, auth_headers):
        resp = client.get("/api/admin/pos/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        today = data["today"]
        for key in (
            "sales_count", "sales_total_cents",
            "refund_count", "refund_total_cents",
        ):
            assert isinstance(today[key], int)
        assert isinstance(data["active_terminals"], list)

    def test_pos_summary_requires_auth(self, client):
        # Unauthenticated requests are rejected before reaching the query.
        assert client.get("/api/admin/pos/summary").status_code in (401, 403)
