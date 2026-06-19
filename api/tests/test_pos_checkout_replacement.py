"""POST /api/v1/pos/checkout: replacement / exchange child orders.

A replacement or exchange checkout names an original order via
parent_so_number; Sentry looks the parent up (in token scope), mints the SO
number as <parent>-REPLACEMENT / -EXCHANGE, and links parent_so_id. A plain
sale is unchanged (POS-<id>, order_type sale, no parent).
"""

import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from _wms_token_helpers import delete_token, insert_token
from db_test_context import get_raw_connection
from services import token_cache


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def pos_token(seed_data):
    plaintext = f"pos-co-{uuid.uuid4()}"
    token_id = insert_token(
        plaintext=plaintext,
        warehouse_ids=[1],
        event_types=[],
        inbound_resources=[],
        source_system=None,
        endpoints=["pos.dispatch"],
    )
    yield {"plaintext": plaintext, "token_id": token_id}
    delete_token(token_id)


def _post(client, token, body):
    return client.post(
        "/api/v1/pos/checkout",
        headers={"X-WMS-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _insert_so(so_number, *, warehouse_id=1):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, status, warehouse_id, order_type, order_source, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING so_id",
        (so_number, "Cust", "SHIPPED", warehouse_id, "sale", "web", str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


def _row(so_number):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT order_type, parent_so_id FROM sales_orders WHERE so_number = %s",
        (so_number,),
    )
    r = cur.fetchone()
    cur.close()
    return r


def _card_body(*, qty=1, order_type=None, parent_so_number=None):
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "external_txn_ref": f"WC-{uuid.uuid4().hex[:12]}",
        "cashier_id": "mike",
        "terminal_id": "reg-01",
        "completed_at": "2026-05-09T14:23:11Z",
        "payment_summary": {
            "method": "card",
            "subtotal_cents": 1999 * qty,
            "tax_cents": 162 * qty,
            "total_cents": 2161 * qty,
            "tenders": [
                {
                    "type": "card",
                    "amount_cents": 2161 * qty,
                    "card_brand": "Visa",
                    "card_last4": "1111",
                    "auth_code": "000289",
                    "external_ref": "0000005400911209",
                }
            ],
        },
        "lines": [
            {
                "sku": "TST-001",
                "warehouse_id": "APT-LAB",
                "bin_id": "A-01-01",
                "quantity": qty,
                "unit_price_cents": 1999,
                "tax_cents": 162 * qty,
                "line_total_cents": 2161 * qty,
            }
        ],
    }
    if order_type is not None:
        body["order_type"] = order_type
    if parent_so_number is not None:
        body["parent_so_number"] = parent_so_number
    return body


class TestReplacementCheckout:
    def test_replacement_mints_suffix_and_links_parent(self, client, pos_token):
        parent_no = f"POS-PARENT-{uuid.uuid4().hex[:6]}"
        parent_id = _insert_so(parent_no, warehouse_id=1)
        resp = _post(
            client,
            pos_token["plaintext"],
            _card_body(order_type="replacement", parent_so_number=parent_no),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["so_number"] == f"{parent_no}-REPLACEMENT"
        order_type, parent_so_id = _row(f"{parent_no}-REPLACEMENT")
        assert order_type == "replacement"
        assert parent_so_id == parent_id

    def test_exchange_mints_exchange_suffix(self, client, pos_token):
        parent_no = f"POS-PARENT-{uuid.uuid4().hex[:6]}"
        _insert_so(parent_no, warehouse_id=1)
        resp = _post(
            client,
            pos_token["plaintext"],
            _card_body(order_type="exchange", parent_so_number=parent_no),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["so_number"] == f"{parent_no}-EXCHANGE"

    def test_replacement_requires_parent_number(self, client, pos_token):
        resp = _post(
            client, pos_token["plaintext"], _card_body(order_type="replacement")
        )
        assert resp.status_code == 422
        assert resp.get_json()["error_kind"] == "parent_so_required"

    def test_replacement_parent_not_found(self, client, pos_token):
        resp = _post(
            client,
            pos_token["plaintext"],
            _card_body(order_type="replacement", parent_so_number="POS-NOPE-999"),
        )
        assert resp.status_code == 404
        assert resp.get_json()["error_kind"] == "parent_so_not_found"

    def test_plain_sale_unchanged(self, client, pos_token):
        resp = _post(client, pos_token["plaintext"], _card_body())
        assert resp.status_code == 200, resp.get_data(as_text=True)
        son = resp.get_json()["so_number"]
        assert son.startswith("POS-")
        order_type, parent_so_id = _row(son)
        assert order_type == "sale"
        assert parent_so_id is None
