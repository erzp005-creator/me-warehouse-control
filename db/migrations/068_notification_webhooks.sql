-- ============================================================
-- Migration 068: notification_webhooks table
-- ============================================================
-- Backs the per-warehouse Teams notification surface introduced
-- alongside the partial-fulfill / backorder workflow.
--
-- Distinct from webhook_subscriptions: that table carries
-- signed-envelope HTTP subscribers with a different
-- payload shape, audit surface, and rate-limit ceiling. This table
-- carries chat-channel destinations (Teams adaptive cards in v1).
-- Both are routed through services/webhook_dispatcher; the
-- dispatcher selects an adapter by channel_kind.
--
-- url is Fernet-encrypted at rest via the existing
-- SENTRY_ENCRYPTION_KEY wrapper that token storage uses today
-- (services/credential_vault.py). Plaintext URL never appears in
-- the DB.
--
-- v1.8.0 mig discipline (#213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

CREATE TABLE IF NOT EXISTS notification_webhooks (
    webhook_id    SERIAL PRIMARY KEY,
    warehouse_id  INT NOT NULL REFERENCES warehouses(warehouse_id),
    channel_kind  VARCHAR(20) NOT NULL,
    url           TEXT NOT NULL,
    event_filter  TEXT[] NOT NULL DEFAULT
                  ARRAY['backorder.opened','backorder.fulfillable'],
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    secret        VARCHAR(128),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    VARCHAR(100) NOT NULL,
    external_id   UUID UNIQUE NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_notification_webhooks_active
    ON notification_webhooks (warehouse_id, channel_kind)
    WHERE enabled = TRUE;

COMMENT ON TABLE notification_webhooks IS
    'Chat-channel notification destinations (Teams adaptive cards in v1). Distinct from webhook_subscriptions, which carries signed-envelope HTTP subscribers with a different payload shape and audit surface. Folded into services/webhook_dispatcher via a channel_kind adapter; routing reads this table for events in the backorder.* family.';

COMMENT ON COLUMN notification_webhooks.url IS
    'Fernet-encrypted at rest via SENTRY_ENCRYPTION_KEY. Plaintext URL never appears in the DB; the admin-facing CRUD surface accepts the URL on create and exposes a "rotate URL" flow that swaps the ciphertext.';

COMMENT ON COLUMN notification_webhooks.channel_kind IS
    'Adapter selector. v1 only ships ''teams''; the column is sized for future ''slack'', ''pagerduty'', etc.';

COMMENT ON COLUMN notification_webhooks.event_filter IS
    'Allowlist of event_type values this webhook receives. Default carries the partial-fulfill events. An empty array means "receive nothing" (explicit opt-out without disabling the row).';

COMMENT ON COLUMN notification_webhooks.secret IS
    'Optional HMAC key for sign-verify on the receiving end. Teams adaptive cards do not require this; Slack and self-hosted webhooks may. NULL on rows that do not need it.';

COMMIT;
