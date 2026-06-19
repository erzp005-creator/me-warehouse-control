"""POST /api/v1/pos/reference-orders contract.

A reference SO is historical scaffolding ingested from a source-system-only
original so a post-fulfillment child (replacement / exchange / RMA) can link
parent_so_id.

Coverage:
- Ingest creates a SHIPPED, order_source='reference' SO whose lines are fully
  shipped, WITHOUT touching inventory.
- Idempotent on so_number.
- SKUs not in the catalog are skipped, not fatal.
- A replacement checkout against the ingested reference resolves the parent
  (no parent_so_not_found) and links parent_so_id.
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
    plaintext = f"pos-ref-{uuid.uuid4()}"
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


def _post_reference(client, token, body):
    return client.post(
        "/api/v1/pos/reference-orders",
        headers={"X-WMS-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _post_checkout(client, token, body):
    return client.post(
        "/api/v1/pos/checkout",
        headers={"X-WMS-Token": token, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


def _so_row(so_number):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT so_id, status, order_source, source_system, order_type "
        "  FROM sales_orders WHERE so_number = %s",
        (so_number,),
    )
    r = cur.fetchone()
    cur.close()
    return r


def _so_lines(so_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT item_id, quantity_ordered, quantity_shipped, status "
        "  FROM sales_order_lines WHERE so_id = %s ORDER BY line_number",
        (so_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def _on_hand(item_id, bin_id, warehouse_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand FROM inventory "
        " WHERE item_id = %s AND bin_id = %s AND warehouse_id = %s",
        (item_id, bin_id, warehouse_id),
    )
    r = cur.fetchone()
    cur.close()
    return int(r[0]) if r else None


def _new_so_number():
    return f"AMZ-{uuid.uuid4().hex[:10]}"


def test_ingest_creates_reference_so_without_touching_inventory(client, pos_token):
    before = _on_hand(1, 3, 1)  # TST-001 @ A-01-01
    so_number = _new_so_number()
    resp = _post_reference(client, pos_token["plaintext"], {
        "so_number": so_number,
        "customer_name": "Historical Buyer",
        "lines": [{"sku": "TST-001", "quantity": 3}],
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["created"] is True
    assert body["so_number"] == so_number
    assert body["skipped_skus"] == []

    row = _so_row(so_number)
    assert row[1] == "SHIPPED"        # status
    assert row[2] == "reference"      # order_source (the marker)
    assert row[3] is None             # source_system left NULL (FK-gated allowlist)
    assert row[4] == "sale"           # order_type

    lines = _so_lines(row[0])
    assert len(lines) == 1
    assert lines[0][1] == 3           # quantity_ordered
    assert lines[0][2] == 3           # quantity_shipped (fully shipped)
    assert lines[0][3] == "SHIPPED"

    # The historical reference must NOT move on-hand inventory.
    assert _on_hand(1, 3, 1) == before


def test_ingest_is_idempotent_on_so_number(client, pos_token):
    so_number = _new_so_number()
    payload = {"so_number": so_number, "lines": [{"sku": "TST-001", "quantity": 1}]}
    first = _post_reference(client, pos_token["plaintext"], payload)
    assert first.status_code == 201
    first_so_id = first.get_json()["so_id"]

    second = _post_reference(client, pos_token["plaintext"], payload)
    assert second.status_code == 200
    assert second.get_json()["created"] is False
    assert second.get_json()["so_id"] == first_so_id


def test_ingest_skips_unknown_skus(client, pos_token):
    so_number = _new_so_number()
    resp = _post_reference(client, pos_token["plaintext"], {
        "so_number": so_number,
        "lines": [
            {"sku": "TST-001", "quantity": 2},
            {"sku": "NOPE-DISCO-999", "quantity": 1},
        ],
    })
    assert resp.status_code == 201
    assert resp.get_json()["skipped_skus"] == ["NOPE-DISCO-999"]
    # Only the known line was inserted.
    row = _so_row(so_number)
    assert len(_so_lines(row[0])) == 1


def test_replacement_against_ingested_reference_links_parent(client, pos_token):
    # Ingest a source-system-only original, then a replacement checkout against it must
    # resolve the parent (previously parent_so_not_found) and link it.
    parent_no = _new_so_number()
    ingest = _post_reference(client, pos_token["plaintext"], {
        "so_number": parent_no,
        "lines": [{"sku": "TST-001", "quantity": 1}],
    })
    assert ingest.status_code == 201

    body = {
        "idempotency_key": str(uuid.uuid4()),
        "external_txn_ref": f"WC-{uuid.uuid4().hex[:12]}",
        "cashier_id": "mike",
        "terminal_id": "reg-01",
        "completed_at": "2026-05-09T14:23:11Z",
        "order_type": "replacement",
        "parent_so_number": parent_no,
        "payment_summary": {
            "method": "card", "subtotal_cents": 0, "tax_cents": 0, "total_cents": 0,
            "tenders": [{
                "type": "card", "amount_cents": 0, "card_brand": "Visa",
                "card_last4": "1111", "auth_code": "000289",
                "external_ref": "0000005400911209",
            }],
        },
        "lines": [{
            "sku": "TST-001", "warehouse_id": "APT-LAB", "bin_id": "A-01-01",
            "quantity": 1, "unit_price_cents": 0, "tax_cents": 0, "line_total_cents": 0,
        }],
    }
    resp = _post_checkout(client, pos_token["plaintext"], body)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    child_no = resp.get_json()["so_number"]
    assert child_no == f"{parent_no}-REPLACEMENT"

    parent = _so_row(parent_no)
    child = _so_row(child_no)
    # The child links the ingested reference as its parent.
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT parent_so_id FROM sales_orders WHERE so_number = %s", (child_no,))
    parent_so_id = cur.fetchone()[0]
    cur.close()
    assert parent_so_id == parent[0]
    assert child[4] == "replacement"
