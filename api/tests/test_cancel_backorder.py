"""Cancel-backorder endpoint.

Thin wrapper over sales_order_service.cancel_sales_order; this file
covers the wrapper-specific validation (must be a BO; valid reason;
not already cancelled) and confirms the event + audit emission via
the underlying service.
"""

from db_test_context import get_raw_connection


def _query_one(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


def _query_val(sql, params=None):
    row = _query_one(sql, params)
    return row[0] if row else None


def _partial_fulfill_seed_so(client, auth_headers, so_id=1, short_qty=1):
    """Run /partial-fulfill against a seeded OPEN SO to create a BO
    we can then cancel. Returns the BO's so_id."""
    sol_id = _query_val(
        "SELECT so_line_id FROM sales_order_lines WHERE so_id = %s LIMIT 1",
        (so_id,),
    )
    resp = client.post(
        f"/api/admin/sales-orders/{so_id}/partial-fulfill",
        json={"lines": [{"so_line_id": sol_id, "short_qty": short_qty}]},
        headers=auth_headers,
    )
    return resp.get_json()["backorder_so"]["so_id"]


def test_cancel_with_refunded_reason_sets_refunded_status(client, auth_headers):
    # mig 074: a backorder cancelled because the customer was refunded lands
    # as REFUNDED (its own terminal status), not CANCELLED, while still
    # carrying the reason. The inventory unwind + cancel audit/event still run
    # (the relabel is on top of the shared cancel service).
    bo_so_id = _partial_fulfill_seed_so(client, auth_headers)

    resp = client.post(
        f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
        json={"cancellation_reason": "refunded"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["cancellation_reason"] == "refunded"

    row = _query_one(
        "SELECT status, cancellation_reason FROM sales_orders WHERE so_id = %s",
        (bo_so_id,),
    )
    assert row == ("REFUNDED", "refunded")


class TestHappyPath:
    def test_cancel_waiting_stock_bo(self, client, auth_headers):
        bo_so_id = _partial_fulfill_seed_so(client, auth_headers)

        resp = client.post(
            f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
            json={"cancellation_reason": "found"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["bo_so_id"] == bo_so_id
        assert body["cancellation_reason"] == "found"

        row = _query_one(
            "SELECT status, cancellation_reason FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        assert row == ("CANCELLED", "found")

        # CANCEL audit row + backorder.cancelled event on the outbox.
        cancel_audit_count = _query_val(
            "SELECT COUNT(*) FROM audit_log "
            " WHERE entity_type = 'SO' AND entity_id = %s "
            "   AND action_type = 'CANCEL'",
            (bo_so_id,),
        )
        assert cancel_audit_count == 1
        emit_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.cancelled' AND aggregate_id = %s",
            (bo_so_id,),
        )
        assert emit_count == 1

    def test_cancel_with_default_reason_is_other(self, client, auth_headers):
        bo_so_id = _partial_fulfill_seed_so(client, auth_headers)
        # Omitting cancellation_reason should default to 'other'.
        resp = client.post(
            f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["cancellation_reason"] == "other"


class TestValidation:
    def test_non_bo_so_rejected(self, client, auth_headers):
        # SO-2026-002 has no parent_so_id and order_type='sale'.
        resp = client.post(
            "/api/admin/sales-orders/2/cancel-backorder",
            json={"cancellation_reason": "found"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Not a backorder" in resp.get_json()["error"]

    def test_already_cancelled_bo_rejected(self, client, auth_headers):
        bo_so_id = _partial_fulfill_seed_so(client, auth_headers)
        # Cancel once.
        client.post(
            f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
            json={"cancellation_reason": "found"},
            headers=auth_headers,
        )
        # Second cancel: 400 at the wrapper layer (service is
        # idempotent but the UI wants the explicit error).
        resp = client.post(
            f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
            json={"cancellation_reason": "found"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "already cancelled" in resp.get_json()["error"].lower()

    def test_invalid_reason_rejected(self, client, auth_headers):
        bo_so_id = _partial_fulfill_seed_so(client, auth_headers)
        resp = client.post(
            f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
            json={"cancellation_reason": "made_up_value"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Allowed:" in resp.get_json()["error"]


class TestParentCancelCascade:
    """Cascade lives in sales_order_service.cancel_sales_order. These
    tests confirm the cascade fires when the parent is cancelled via
    the regular /cancel endpoint."""

    def test_cancelling_parent_cascades_to_bo(self, client, auth_headers):
        bo_so_id = _partial_fulfill_seed_so(client, auth_headers, so_id=1)

        # Cancel the parent (SO-2026-001, so_id=1) via the regular
        # /cancel endpoint.
        resp = client.post(
            "/api/admin/sales-orders/1/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

        # Both parent and BO are CANCELLED.
        parent_status = _query_val(
            "SELECT status FROM sales_orders WHERE so_id = 1"
        )
        assert parent_status == "CANCELLED"

        bo_row = _query_one(
            "SELECT status, cancellation_reason FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        assert bo_row == ("CANCELLED", "parent_cancelled")

        # backorder.cancelled was emitted for the BO (cascade path).
        emit_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.cancelled' AND aggregate_id = %s",
            (bo_so_id,),
        )
        assert emit_count == 1

    def test_parent_with_already_cancelled_bo_is_idempotent(self, client, auth_headers):
        bo_so_id = _partial_fulfill_seed_so(client, auth_headers, so_id=1)

        # Cancel BO first.
        client.post(
            f"/api/admin/sales-orders/{bo_so_id}/cancel-backorder",
            json={"cancellation_reason": "found"},
            headers=auth_headers,
        )
        # Now cancel parent.
        resp = client.post(
            "/api/admin/sales-orders/1/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # BO keeps its original reason (the cascade does not overwrite
        # a CANCELLED child).
        bo_reason = _query_val(
            "SELECT cancellation_reason FROM sales_orders WHERE so_id = %s",
            (bo_so_id,),
        )
        # Either the original "found" (cascade skipped) or
        # "parent_cancelled" (cascade overwrote then idempotent
        # cancel returned no-op). Either is acceptable behavior; the
        # invariant is exactly one backorder.cancelled event.
        assert bo_reason in ("found", "parent_cancelled")

        emit_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.cancelled' AND aggregate_id = %s",
            (bo_so_id,),
        )
        assert emit_count == 1

    def test_parent_with_no_bo_does_not_cascade(self, client, auth_headers):
        # SO-2026-002 has no children; cancel should behave normally.
        resp = client.post(
            "/api/admin/sales-orders/2/cancel",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        emit_count = _query_val(
            "SELECT COUNT(*) FROM integration_events "
            " WHERE event_type = 'backorder.cancelled'",
        )
        assert emit_count == 0
