"""Admin CRUD contract for /api/admin/channels (v1.30.0, Pipe C).

Exercises the full lifecycle through the Flask test client: create (with the
https / SSRF guards), list with stats, detail, per-field PATCH, pause, the
scope-change re-materialize, soft-delete, the DLQ view, and that every mutation
writes an audit_log row. SSRF is bypassed via the documented dev/CI flag so the
fake sink hostname does not need DNS.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from db_test_context import get_raw_connection  # noqa: E402

_SINK = "https://sink.example.com/availability"


@pytest.fixture(autouse=True)
def _allow_internal_sink(monkeypatch):
    # The create/update endpoints run the dispatch-time SSRF guard; a fake sink
    # hostname would fail DNS. Bypass via the documented dev/CI flag (the guard's
    # real behavior is covered in the publish tests).
    monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")


# ---------------------------------------------------------------- helpers

def _mk_item_with_stock(qty, category=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = f"PC-{uuid.uuid4().hex[:10]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, category, is_active, external_id) "
        "VALUES (%s, %s, %s, TRUE, %s) RETURNING item_id",
        (sku, sku, category, str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.execute("SELECT bin_id, warehouse_id FROM bins WHERE bin_type='Pickable' "
                "ORDER BY bin_id LIMIT 1")
    bin_id, wh = cur.fetchone()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand) "
        "VALUES (%s, %s, %s, %s)",
        (item_id, bin_id, wh, qty),
    )
    cur.close()
    return item_id, sku


def _count_avail(channel_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM channel_availability WHERE channel_id=%s",
                (channel_id,))
    n = cur.fetchone()[0]
    cur.close()
    return n


def _avail_skus(channel_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT i.sku FROM channel_availability ca JOIN items i ON i.item_id=ca.item_id "
        "WHERE ca.channel_id=%s",
        (channel_id,),
    )
    skus = {r[0] for r in cur.fetchall()}
    cur.close()
    return skus


def _audit_count(action):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM audit_log WHERE action_type=%s", (action,))
    n = cur.fetchone()[0]
    cur.close()
    return n


def _create(client, auth_headers, **overrides):
    payload = {
        "channel_id": overrides.pop("channel_id", "amz"),
        "display_name": "Amazon",
        "delivery_url": _SINK,
    }
    payload.update(overrides)
    return client.post("/api/admin/channels", json=payload, headers=auth_headers)


# ---------------------------------------------------------------- create

class TestCreate:
    def test_create_materializes_and_audits(self, client, auth_headers):
        _, sku = _mk_item_with_stock(7)
        before = _audit_count("CHANNEL_CREATE")
        resp = _create(client, auth_headers, sku_scope={"skus": [sku]})
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["channel_id"] == "amz"
        # Initial snapshot materialized for the in-scope sku.
        assert _avail_skus("amz") == {sku}
        assert _audit_count("CHANNEL_CREATE") == before + 1

    def test_duplicate_channel_id_409(self, client, auth_headers):
        _create(client, auth_headers, channel_id="dup")
        resp = _create(client, auth_headers, channel_id="dup")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "channel_id_exists"

    def test_invalid_channel_id_400(self, client, auth_headers):
        resp = _create(client, auth_headers, channel_id="Not A Slug")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_unknown_field_rejected_400(self, client, auth_headers):
        resp = _create(client, auth_headers, surprise="x")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "validation_error"

    def test_http_sink_rejected(self, client, auth_headers):
        resp = _create(client, auth_headers,
                       delivery_url="http://sink.example.com/x")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "https_required"


# ---------------------------------------------------------------- read

class TestRead:
    def test_list_includes_stats(self, client, auth_headers):
        _, sku = _mk_item_with_stock(3)
        _create(client, auth_headers, channel_id="lst", sku_scope={"skus": [sku]})
        body = client.get("/api/admin/channels", headers=auth_headers).get_json()
        row = next(c for c in body["channels"] if c["channel_id"] == "lst")
        assert row["item_count"] == 1
        assert row["dirty_count"] == 1  # freshly materialized, unpublished

    def test_detail_and_404(self, client, auth_headers):
        _create(client, auth_headers, channel_id="det")
        assert client.get("/api/admin/channels/det",
                          headers=auth_headers).status_code == 200
        assert client.get("/api/admin/channels/nope",
                          headers=auth_headers).status_code == 404


# ---------------------------------------------------------------- update

class TestUpdate:
    def test_patch_scalar_field(self, client, auth_headers):
        _create(client, auth_headers, channel_id="upd")
        resp = client.patch("/api/admin/channels/upd",
                            json={"rate_limit_per_second": 25}, headers=auth_headers)
        assert resp.status_code == 200
        assert "rate_limit_per_second" in resp.get_json()["updated_fields"]
        assert _audit_count("CHANNEL_UPDATE") >= 1

    def test_pause_sets_reason(self, client, auth_headers):
        _create(client, auth_headers, channel_id="pse")
        client.patch("/api/admin/channels/pse",
                     json={"status": "paused"}, headers=auth_headers)
        detail = client.get("/api/admin/channels/pse",
                            headers=auth_headers).get_json()
        assert detail["status"] == "paused"
        assert detail["pause_reason"] == "manual"

    def test_status_revoked_rejected(self, client, auth_headers):
        _create(client, auth_headers, channel_id="rev")
        resp = client.patch("/api/admin/channels/rev",
                            json={"status": "revoked"}, headers=auth_headers)
        assert resp.status_code == 400  # schema only allows active/paused

    def test_scope_change_rematerializes(self, client, auth_headers):
        _, sku_a = _mk_item_with_stock(2)
        _, sku_b = _mk_item_with_stock(2)
        _create(client, auth_headers, channel_id="scp", sku_scope={"skus": [sku_a]})
        assert _avail_skus("scp") == {sku_a}
        # Repoint scope to sku_b: A drops out, B materializes.
        resp = client.patch("/api/admin/channels/scp",
                            json={"sku_scope": {"skus": [sku_b]}}, headers=auth_headers)
        assert resp.status_code == 200
        assert _avail_skus("scp") == {sku_b}


# ---------------------------------------------------------------- delete

class TestDelete:
    def test_soft_delete_hides_from_list(self, client, auth_headers):
        _create(client, auth_headers, channel_id="del")
        resp = client.delete("/api/admin/channels/del", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "revoked"
        body = client.get("/api/admin/channels", headers=auth_headers).get_json()
        assert all(c["channel_id"] != "del" for c in body["channels"])
        assert _audit_count("CHANNEL_DELETE") >= 1


# ---------------------------------------------------------------- dlq

class TestDlq:
    def test_dlq_empty_initially(self, client, auth_headers):
        _create(client, auth_headers, channel_id="dlqc")
        body = client.get("/api/admin/channels/dlqc/dlq",
                          headers=auth_headers).get_json()
        assert body["parked"] == []
