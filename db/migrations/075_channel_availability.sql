-- ============================================================
-- Migration 075: channel availability (Pipe C)
-- ============================================================
-- Pipe C materializes per-channel sellable availability and
-- debounce-publishes it to a per-channel HTTP sink. It is separate
-- from Pipe A (integration_events / webhooks) on purpose: fanning out
-- every raw inventory change to every marketplace would blow past
-- their rate limits within minutes. Instead Pipe C collapses many
-- inventory changes into a current-state number per (channel, item)
-- and the connector-publisher daemon sends the latest debounced
-- snapshot.
--
-- Three tables + one sequence:
--
--   * channels                  -- per-channel config: delivery_url,
--                                  SKU scope, transform map, rate /
--                                  batch / debounce, status lifecycle.
--   * channel_availability      -- the materialization, keyed
--                                  (channel_id, item_id), carrying the
--                                  current_version / last_version
--                                  pattern. A row is "dirty" (awaiting
--                                  publish) iff current_version >
--                                  last_version.
--   * channel_recompute_state   -- singleton cursor into
--                                  integration_events for the recompute
--                                  consumer.
--   * channel_availability_version_seq -- global monotonic source for
--                                  current_version bumps.
--
-- available_qty is sellable stock: SUM(on_hand - allocated) over the
-- channel's in-scope warehouses, counting only Pickable /
-- PickableStaging bins. The recompute service reads CURRENT inventory
-- state (events are triggers, not deltas), so replaying an event is
-- idempotent. current_version bumps only when the recomputed number
-- actually differs from the stored one, so a no-op inventory churn
-- does not enqueue a publish.
--
-- The publisher reuses the dispatcher's dispatch-time SSRF guard on
-- delivery_url. Real Amazon / BigCommerce / Shopify API clients are
-- deferred (v2.1+); the sink here is a generic HTTP receiver.
--
-- Additive only: no existing inventory or write path is touched, and
-- no new trigger lands on a hot path. The recompute consumer wakes on
-- the existing integration_events_visible NOTIFY (migration 031).
--
-- v1.8.0 mig discipline (#213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

-- Global monotonic version source. A single sequence across all
-- channels/items is fine: per-row monotonicity is all the
-- current_version > last_version comparison needs, and a global
-- sequence trivially satisfies it.
CREATE SEQUENCE IF NOT EXISTS channel_availability_version_seq AS BIGINT;

CREATE TABLE IF NOT EXISTS channels (
    channel_id            VARCHAR(64)  PRIMARY KEY,            -- operator slug, e.g. 'amazon-fba'
    display_name          VARCHAR(128) NOT NULL,
    delivery_url          TEXT         NOT NULL,
    -- SKU scope: which items this channel publishes. An absent or
    -- empty dimension means "no restriction on that dimension".
    -- Shape (validated app-side): {"skus": [...], "categories": [...],
    -- "warehouse_ids": [...]}.
    sku_scope             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- Declarative transform map applied to the outbound payload.
    -- Shape (validated app-side): {"rename": {"sku": "seller_sku"},
    -- "constants": {"fulfillment_channel": "AMAZON_NA"}}. No code
    -- execution -- field rename + constant injection only.
    transform             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    status                VARCHAR(16)  NOT NULL DEFAULT 'active',
    pause_reason          VARCHAR(32),
    rate_limit_per_second INTEGER      NOT NULL DEFAULT 10,
    batch_size            INTEGER      NOT NULL DEFAULT 100,
    debounce_seconds      INTEGER      NOT NULL DEFAULT 30,
    pending_ceiling       INTEGER      NOT NULL DEFAULT 100000,
    dlq_ceiling           INTEGER      NOT NULL DEFAULT 1000,
    last_published_at     TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by            VARCHAR(100) NOT NULL,
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    external_id           UUID         UNIQUE NOT NULL,
    CONSTRAINT channels_delivery_url_scheme
        CHECK (delivery_url ~ '^https?://'),
    CONSTRAINT channels_rate_limit_range
        CHECK (rate_limit_per_second BETWEEN 1 AND 1000),
    CONSTRAINT channels_batch_size_range
        CHECK (batch_size BETWEEN 1 AND 1000),
    CONSTRAINT channels_debounce_range
        CHECK (debounce_seconds BETWEEN 0 AND 3600),
    CONSTRAINT channels_pending_ceiling_range
        CHECK (pending_ceiling BETWEEN 100 AND 1000000),
    CONSTRAINT channels_dlq_ceiling_range
        CHECK (dlq_ceiling BETWEEN 10 AND 100000),
    -- Column-level enforcement, mirroring webhook_subscriptions #236:
    -- a privileged-role error or malicious migration cannot write an
    -- out-of-band status. malformed_config is the publisher's
    -- auto-pause value when a channel's transform/scope fails to load.
    CONSTRAINT channels_status_enum
        CHECK (status IN ('active', 'paused', 'revoked')),
    CONSTRAINT channels_pause_reason_enum
        CHECK (
            pause_reason IS NULL
            OR pause_reason IN (
                'manual',
                'pending_ceiling',
                'dlq_ceiling',
                'malformed_config'
            )
        )
);

CREATE INDEX IF NOT EXISTS ix_channels_active
    ON channels (status)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS channel_availability (
    channel_id        VARCHAR(64)  NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
    item_id           INT          NOT NULL REFERENCES items(item_id),
    available_qty     INTEGER      NOT NULL DEFAULT 0,   -- SUM(on_hand - allocated), scoped bins
    current_version   BIGINT       NOT NULL DEFAULT 0,   -- bumped when available_qty changes
    last_version      BIGINT       NOT NULL DEFAULT 0,   -- last version published to the sink
    attempt_count     INTEGER      NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ,                       -- backoff gate; NULL = eligible now
    dlq               BOOLEAN      NOT NULL DEFAULT FALSE,
    last_error        TEXT,
    last_published_at TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (channel_id, item_id)
);

-- Hot-query partial index. The publisher claim is
-- "WHERE channel_id = :c AND current_version > last_version"; index
-- only the dirty rows so a channel with millions of clean SKUs still
-- claims in time proportional to its backlog, not its catalog.
CREATE INDEX IF NOT EXISTS ix_channel_availability_dirty
    ON channel_availability (channel_id, current_version)
    WHERE current_version > last_version;

-- DLQ view support: list parked rows per channel cheaply.
CREATE INDEX IF NOT EXISTS ix_channel_availability_dlq
    ON channel_availability (channel_id)
    WHERE dlq = TRUE;

-- Singleton cursor for the recompute consumer's position in
-- integration_events. One consumer feeds every channel, so one
-- global cursor (not per-channel).
CREATE TABLE IF NOT EXISTS channel_recompute_state (
    only_row     BOOLEAN     PRIMARY KEY DEFAULT TRUE,
    last_cursor  BIGINT      NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT channel_recompute_state_singleton CHECK (only_row = TRUE)
);

-- Seed the singleton at cursor 0. The daemon fast-forwards a fresh
-- (cursor = 0) install to MAX(event_id) on first boot so it does not
-- replay the whole event history; events are triggers, and the
-- channel seed materializes from a live inventory scan instead.
INSERT INTO channel_recompute_state (only_row, last_cursor)
VALUES (TRUE, 0)
ON CONFLICT (only_row) DO NOTHING;

COMMENT ON TABLE channels IS
    'Pipe C per-channel availability config. Standalone (not linked to the v1.3 connectors stub): a channel owns its delivery_url HTTP sink, SKU scope, declarative transform map, and rate/batch/debounce knobs. status lifecycle mirrors webhook_subscriptions (active/paused/revoked).';

COMMENT ON COLUMN channels.sku_scope IS
    'Which items publish on this channel. {"skus": [...], "categories": [...], "warehouse_ids": [...]}. An absent or empty dimension imposes no restriction on that dimension; {} publishes every active item across every warehouse.';

COMMENT ON COLUMN channels.transform IS
    'Declarative outbound payload transform. {"rename": {"sku": "seller_sku"}, "constants": {"fulfillment_channel": "AMAZON_NA"}}. Field rename + constant injection only -- no expression evaluation (deferred). Covers the Amazon FBA vs MFN distinction.';

COMMENT ON COLUMN channels.debounce_seconds IS
    'Minimum seconds between consecutive publishes for this channel. The publisher collapses all dirty rows accumulated during the window into one batch on the next tick. 0 disables debounce (publish on every wake).';

COMMENT ON TABLE channel_availability IS
    'Materialized sellable availability per (channel, item). available_qty = SUM(on_hand - allocated) over the channel''s in-scope warehouses, Pickable/PickableStaging bins only. A row is dirty (awaiting publish) iff current_version > last_version; the publisher advances last_version to the value it published.';

COMMENT ON COLUMN channel_availability.current_version IS
    'nextval(channel_availability_version_seq) stamped each time the recompute service observes available_qty actually change. A no-op recompute does not bump it, so an inventory event that nets to the same sellable number enqueues no publish.';

COMMENT ON COLUMN channel_availability.dlq IS
    'TRUE once attempt_count exhausts the retry schedule. A parked row is skipped by the claim until a NEWER current_version arrives (a fresh inventory change), which resets dlq/attempt_count/next_attempt_at so current state always gets another chance.';

COMMIT;
