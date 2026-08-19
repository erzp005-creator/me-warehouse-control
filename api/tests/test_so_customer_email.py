"""sales_orders.customer_email: inbound mapping + admin read/edit (mig 078).

customer_email is a new SO column shown on the order-detail modal. It is
populated three ways: the POS checkout (already on the wire), an inbound
connector that declares `canonical: "customer_email"`, and the admin
header-edit PUT. This file pins the inbound round-trip and the admin
read / edit so a future change to the inbound service, the canonical-column
validator, or the SO PUT handler does not silently break the email path.
"""

import json
import os
import sys
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

import db_test_context
from services import token_cache
from services.mapping_loader import (
    LoadedMappingFile,
    MappingDocument,
    MappingRegistry,
)


_MAPPING_WITH_EMAIL = """\
mapping_version: "1.0"
source_system: "{ss}"
version_compare: "iso_timestamp"
resources:
  sales_orders:
    canonical_type: "sales_order"
    fields:
      - canonical: "so_number"
        source_path: "$.orderNumber"
        type: "string"
        required: true
      - canonical: "warehouse_id"
        source_path: "$.warehouseId"
        type: "integer"
        required: true
      - canonical: "customer_name"
        source_path: "$.customer.name"
        type: "string"
      - canonical: "customer_email"
        source_path: "$.customer.email"
        type: "string"
"""


def _query(sql, params=()):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchall()
    finally:
        cur.close()


def _build_registry(app, ss: str) -> MappingDocument:
    parsed = yaml.safe_load(_MAPPING_WITH_EMAIL.format(ss=ss))
    doc = MappingDocument.model_validate(parsed)
    registry = MappingRegistry()
    registry.register(LoadedMappingFile(
        document=doc, path=f"<test:{ss}>", sha256="0" * 64,
    ))
    app.config["MAPPING_REGISTRY"] = registry
    return doc


def _insert_token_and_allowlist(ss: str, plaintext: str) -> int:
    import hashlib
    import json as _json
    from _wms_token_helpers import PEPPER

    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inbound_source_systems_allowlist (source_system, kind) "
        "VALUES (%s, 'internal_tool') ON CONFLICT DO NOTHING",
        (ss,),
    )
    token_hash = hashlib.sha256((PEPPER + plaintext).encode()).hexdigest()
    cur.execute(
        "INSERT INTO wms_tokens "
        "(token_name, token_hash, status, warehouse_ids, event_types, "
        " endpoints, source_system, inbound_resources, mapping_override, "
        " mapping_overrides) "
        "VALUES (%s, %s, 'active', %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "RETURNING token_id",
        (
            f"inbound-email-{uuid.uuid4().hex[:6]}",
            token_hash,
            [1], [], [], ss, ["sales_orders"], False, _json.dumps({}),
        ),
    )
    token_id = cur.fetchone()[0]
    cur.close()
    return token_id


def _insert_so(*, customer_email=None, status="OPEN", warehouse_id=1):
    conn = db_test_context.get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sales_orders "
        "(so_number, customer_name, customer_email, status, warehouse_id, external_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING so_id",
        (f"SO-EMAIL-{uuid.uuid4().hex[:8]}", "Cust", customer_email, status,
         warehouse_id, str(uuid.uuid4())),
    )
    so_id = cur.fetchone()[0]
    cur.close()
    return so_id


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scenario(app):
    ss = f"email-test-{uuid.uuid4().hex[:8]}"
    _build_registry(app, ss)
    plaintext = f"email-token-{uuid.uuid4().hex[:8]}"
    _insert_token_and_allowlist(ss, plaintext)
    return {"ss": ss, "plaintext": plaintext}


def _post(client, plaintext, body):
    return client.post(
        "/api/v1/inbound/sales_orders",
        headers={"X-WMS-Token": plaintext, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


class TestInboundCustomerEmail:
    def test_email_round_trips_to_canonical(self, client, app, scenario):
        resp = _post(client, scenario["plaintext"], {
            "external_id": "SO-EMAIL-1",
            "external_version": "2026-05-08T10:00:00Z",
            "source_payload": {
                "orderNumber": "SO-EMAIL-1",
                "warehouseId": 1,
                "customer": {"name": "Acme", "email": "buyer@acme.test"},
            },
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        canonical_id = resp.get_json()["canonical_id"]
        email = _query(
            "SELECT customer_email FROM sales_orders WHERE external_id = %s",
            (canonical_id,),
        )[0][0]
        assert email == "buyer@acme.test"

    def test_email_omitted_lands_as_null(self, client, app, scenario):
        resp = _post(client, scenario["plaintext"], {
            "external_id": "SO-EMAIL-2",
            "external_version": "2026-05-08T10:00:00Z",
            "source_payload": {
                "orderNumber": "SO-EMAIL-2",
                "warehouseId": 1,
                "customer": {"name": "Acme"},  # no email
            },
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        canonical_id = resp.get_json()["canonical_id"]
        email = _query(
            "SELECT customer_email FROM sales_orders WHERE external_id = %s",
            (canonical_id,),
        )[0][0]
        assert email is None


class TestAdminCustomerEmail:
    def test_get_returns_customer_email(self, client, auth_headers, _db_transaction):
        so_id = _insert_so(customer_email="buyer@example.com")
        r = client.get(f"/api/admin/sales-orders/{so_id}", headers=auth_headers)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["sales_order"]["customer_email"] == "buyer@example.com"

    def test_put_edits_customer_email(self, client, auth_headers, _db_transaction):
        so_id = _insert_so(customer_email=None)
        r = client.put(
            f"/api/admin/sales-orders/{so_id}",
            headers=auth_headers,
            json={"customer_email": "corrected@example.com"},
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        g = client.get(f"/api/admin/sales-orders/{so_id}", headers=auth_headers)
        assert g.get_json()["sales_order"]["customer_email"] == "corrected@example.com"
