"""End-to-end cycle test for Pipe C: the daemon's own _cycle() wired through the
real recompute service and the real publish path, with only the sink HTTP call
faked. Proves the headline acceptance criterion -- reserving all of a SKU drops
its published availability to 0 on the next cycle -- through the actual code the
container runs, not a reconstruction of it.
"""

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text as sa_text  # noqa: E402

import models.database as md  # noqa: E402
from db_test_context import get_raw_connection  # noqa: E402
from services.connector_publisher import ConnectorPublisher  # noqa: E402
from services.connector_publisher import publish as publish_module  # noqa: E402


# ---------------------------------------------------------------- helpers

def _mk_item_with_stock(qty):
    conn = get_raw_connection()
    cur = conn.cursor()
    sku = f"PC-{uuid.uuid4().hex[:10]}"
    cur.execute(
        "INSERT INTO items (sku, item_name, is_active, external_id) "
        "VALUES (%s, %s, TRUE, %s) RETURNING item_id",
        (sku, sku, str(uuid.uuid4())),
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
    return item_id, sku, bin_id


def _mk_channel(channel_id, sku, *, debounce=0, transform="{}"):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO channels (channel_id, display_name, delivery_url, sku_scope, "
        "transform, debounce_seconds, created_by, external_id) "
        "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
        (channel_id, channel_id, "https://sink.example.com/availability",
         '{"skus": ["' + sku + '"]}', transform, debounce, "tester", str(uuid.uuid4())),
    )
    cur.close()


def _reserve(item_id, bin_id, allocated):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("UPDATE inventory SET quantity_allocated=%s WHERE item_id=%s AND bin_id=%s",
                (allocated, item_id, bin_id))
    cur.close()


def _make_publisher(monkeypatch, sent):
    # Bypass SSRF (fake sink hostname) and fake the sink POST so _cycle exercises
    # the real recompute + publish path without touching the network.
    monkeypatch.setenv("SENTRY_ALLOW_INTERNAL_WEBHOOKS", "true")
    monkeypatch.setattr(
        publish_module, "_default_http_post",
        lambda url, payload, **kw: (sent.append(payload) or (200, "ok")),
    )
    pub = ConnectorPublisher()
    pub._Session = md.SessionLocal
    return pub


# ---------------------------------------------------------------- tests

class TestFullCycle:
    def test_cycle_publishes_live_availability(self, _db_transaction, monkeypatch):
        item_id, sku, _bin = _mk_item_with_stock(10)
        _mk_channel("amz", sku)
        sent = []
        pub = _make_publisher(monkeypatch, sent)

        pub._cycle()

        assert len(sent) == 1
        assert sent[0]["channel_id"] == "amz"
        assert sent[0]["items"] == [{"sku": sku, "available": 10}]
        # Row is now clean.
        clean = md.SessionLocal().execute(
            sa_text("SELECT current_version = last_version AS clean "
                    "FROM channel_availability WHERE channel_id='amz' AND item_id=:i"),
            {"i": item_id},
        ).fetchone().clean
        assert clean is True

    def test_reserve_drops_published_to_zero(self, _db_transaction, monkeypatch):
        item_id, sku, bin_id = _mk_item_with_stock(10)
        _mk_channel("amz", sku, debounce=0)
        sent = []
        pub = _make_publisher(monkeypatch, sent)

        pub._cycle()
        assert sent[-1]["items"][0]["available"] == 10

        # An order reserves the whole quantity (reserve-at-creation).
        _reserve(item_id, bin_id, allocated=10)

        pub._cycle()
        # Next cycle reconciles from live inventory and publishes the truth: 0.
        assert sent[-1]["items"][0]["available"] == 0

    def test_quiet_cycle_publishes_nothing(self, _db_transaction, monkeypatch):
        item_id, sku, _bin = _mk_item_with_stock(5)
        _mk_channel("amz", sku, debounce=0)
        sent = []
        pub = _make_publisher(monkeypatch, sent)

        pub._cycle()             # initial publish
        assert len(sent) == 1
        pub._cycle()             # nothing changed -> no version bump -> no publish
        assert len(sent) == 1

    def test_transform_applies_on_the_wire(self, _db_transaction, monkeypatch):
        item_id, sku, _bin = _mk_item_with_stock(4)
        _mk_channel(
            "amz", sku, debounce=0,
            transform='{"rename": {"sku": "seller_sku", "available": "quantity"}, '
                      '"constants": {"fulfillment_channel": "AMAZON_NA"}}',
        )
        sent = []
        pub = _make_publisher(monkeypatch, sent)

        pub._cycle()
        assert sent[0]["items"] == [
            {"seller_sku": sku, "quantity": 4, "fulfillment_channel": "AMAZON_NA"}
        ]

    def test_paused_channel_is_not_published(self, _db_transaction, monkeypatch):
        item_id, sku, _bin = _mk_item_with_stock(6)
        _mk_channel("amz", sku, debounce=0)
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("UPDATE channels SET status='paused' WHERE channel_id='amz'")
        cur.close()
        sent = []
        pub = _make_publisher(monkeypatch, sent)

        pub._cycle()
        # recompute_active_channels and the publish pass both skip non-active
        # channels, so nothing is materialized or sent.
        assert sent == []
