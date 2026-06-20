"""Per-batch publish contract for Pipe C (services.connector_publisher.publish).

Covers the transform, the success advance (last_version -> current_version, and
the row goes clean), failure backoff, DLQ on attempt exhaustion, and the
dispatch-time SSRF pause. http_post is injected so no real network is touched.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json  # noqa: E402

from sqlalchemy import text as sa_text  # noqa: E402

from db_test_context import get_raw_connection  # noqa: E402
from services.channel_availability_service import recompute_channel  # noqa: E402
from services.connector_publisher.publish import (  # noqa: E402
    MAX_ATTEMPTS,
    apply_transform,
    publish_channel,
)
from services.webhook_dispatcher.ssrf_guard import SsrfRejected  # noqa: E402

# Most tests inject a no-op SSRF check: the real guard does live DNS resolution,
# which a fake sink hostname would fail. The guard's private-IP detection is
# covered by the webhook-dispatcher SSRF tests; here we drive the pause branch
# explicitly with a raising stub.
_SSRF_OK = lambda url: None  # noqa: E731


# ---------------------------------------------------------------- helpers

def _mk_item(sku=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = sku or f"PC-{uuid.uuid4().hex[:10]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, is_active, external_id) "
        "VALUES (%s, %s, TRUE, %s) RETURNING item_id",
        (sku, sku, str(uuid.uuid4())),
    )
    item_id = cur.fetchone()[0]
    cur.close()
    return item_id, sku


def _pick_bin():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT bin_id, warehouse_id FROM bins WHERE bin_type='Pickable' "
                "ORDER BY bin_id LIMIT 1")
    row = cur.fetchone()
    cur.close()
    return row[0], row[1]


def _set_inv(item_id, bin_id, wh, on_hand, allocated=0):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO inventory (item_id, bin_id, warehouse_id, quantity_on_hand, "
        "quantity_allocated) VALUES (%s, %s, %s, %s, %s)",
        (item_id, bin_id, wh, on_hand, allocated),
    )
    cur.close()


def _mk_channel(channel_id, *, delivery_url="https://sink.example.com/availability",
                transform=None, batch_size=100):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO channels (channel_id, display_name, delivery_url, batch_size, "
        "transform, created_by, external_id) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
        (channel_id, channel_id, delivery_url, batch_size,
         json.dumps(transform or {}), "tester", str(uuid.uuid4())),
    )
    cur.close()


def _channel_row(db, channel_id):
    return db.execute(
        sa_text("SELECT * FROM channels WHERE channel_id = :c"),
        {"c": channel_id},
    ).fetchone()


def _avail(db, channel_id, item_id):
    return db.execute(
        sa_text("SELECT available_qty, current_version, last_version, attempt_count, "
                "dlq, last_error FROM channel_availability "
                "WHERE channel_id = :c AND item_id = :i"),
        {"c": channel_id, "i": item_id},
    ).fetchone()


# ---------------------------------------------------------------- transform

class TestTransform:
    def test_rename_and_constants(self):
        out = apply_transform(
            {"sku": "TST-1", "available": 5},
            {"rename": {"sku": "seller_sku", "available": "quantity"},
             "constants": {"fulfillment_channel": "AMAZON_NA"}},
        )
        assert out == {"seller_sku": "TST-1", "quantity": 5,
                       "fulfillment_channel": "AMAZON_NA"}

    def test_empty_transform_is_identity(self):
        out = apply_transform({"sku": "X", "available": 2}, {})
        assert out == {"sku": "X", "available": 2}


# ---------------------------------------------------------------- publish

class TestPublishSuccess:
    def test_success_advances_and_cleans_row(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pb, wh = _pick_bin()
        _set_inv(item_id, pb, wh, on_hand=7)
        recompute_channel(db, "amz", {"skus": [sku]})

        sent = []

        def fake_post(url, payload):
            sent.append((url, payload))
            return 200, "ok"

        result = publish_channel(db, _channel_row(db, "amz"), http_post=fake_post,
                                 ssrf_check=_SSRF_OK)
        assert result == {"published": 1}

        # The sink received the sellable number.
        assert sent[0][0] == "https://sink.example.com/availability"
        assert sent[0][1]["items"] == [{"sku": sku, "available": 7}]

        # Row is now clean (current_version == last_version).
        row = _avail(db, "amz", item_id)
        assert row.last_version == row.current_version
        assert row.attempt_count == 0 and row.dlq is False

    def test_no_dirty_rows_is_a_noop(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        result = publish_channel(db, _channel_row(db, "amz"),
                                 http_post=lambda u, p: (200, "ok"))
        assert result == {"skipped": "no_dirty"}


class TestPublishFailure:
    def test_http_error_backs_off_and_keeps_row_dirty(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pb, wh = _pick_bin()
        _set_inv(item_id, pb, wh, on_hand=3)
        recompute_channel(db, "amz", {"skus": [sku]})

        result = publish_channel(db, _channel_row(db, "amz"),
                                 http_post=lambda u, p: (500, "boom"),
                                 ssrf_check=_SSRF_OK)
        assert result == {"failed": 1}
        row = _avail(db, "amz", item_id)
        assert row.attempt_count == 1
        assert row.dlq is False
        assert row.last_version < row.current_version  # still dirty -> will retry
        assert "500" in row.last_error

    def test_network_raise_is_a_failed_batch(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pb, wh = _pick_bin()
        _set_inv(item_id, pb, wh, on_hand=1)
        recompute_channel(db, "amz", {"skus": [sku]})

        def boom(url, payload):
            raise RuntimeError("connection refused")

        result = publish_channel(db, _channel_row(db, "amz"), http_post=boom,
                                 ssrf_check=_SSRF_OK)
        assert result == {"failed": 1}
        assert _avail(db, "amz", item_id).attempt_count == 1

    def test_dlq_after_attempts_exhausted(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pb, wh = _pick_bin()
        _set_inv(item_id, pb, wh, on_hand=1)
        recompute_channel(db, "amz", {"skus": [sku]})

        # Pre-age the row to one failure short of the DLQ threshold, and clear
        # next_attempt_at so it is eligible to claim.
        db.execute(
            sa_text("UPDATE channel_availability SET attempt_count = :a, "
                    "next_attempt_at = NULL WHERE channel_id='amz' AND item_id=:i"),
            {"a": MAX_ATTEMPTS - 1, "i": item_id},
        )
        result = publish_channel(db, _channel_row(db, "amz"),
                                 http_post=lambda u, p: (503, "down"),
                                 ssrf_check=_SSRF_OK)
        assert result == {"failed": 1}
        row = _avail(db, "amz", item_id)
        assert row.attempt_count == MAX_ATTEMPTS
        assert row.dlq is True  # parked


class TestSsrfPause:
    def test_private_sink_pauses_channel(self, _db_transaction):
        db = _db_transaction
        _mk_channel("amz")
        item_id, sku = _mk_item()
        pb, wh = _pick_bin()
        _set_inv(item_id, pb, wh, on_hand=4)
        recompute_channel(db, "amz", {"skus": [sku]})

        def reject(url):
            raise SsrfRejected(f"refusing {url}: loopback")

        posted = []
        result = publish_channel(
            db, _channel_row(db, "amz"),
            http_post=lambda u, p: posted.append(1) or (200, "ok"),
            ssrf_check=reject,
        )
        assert result == {"paused": "malformed_config"}
        assert posted == []  # never POSTed to the rejected address
        ch = _channel_row(db, "amz")
        assert ch.status == "paused" and ch.pause_reason == "malformed_config"
