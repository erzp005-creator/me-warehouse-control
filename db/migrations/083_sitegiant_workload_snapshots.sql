-- SiteGiant hourly workload snapshots
--
-- Stores read-only package-stage totals captured from the merchant dashboard.
-- These rows inform staffing and dispatch; they never mutate SiteGiant orders.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

CREATE TABLE IF NOT EXISTS sitegiant_workload_snapshots (
    snapshot_id             BIGSERIAL PRIMARY KEY,
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    source_system           VARCHAR(64) NOT NULL DEFAULT 'sitegiant',
    captured_at             TIMESTAMPTZ NOT NULL,
    period_start            DATE,
    period_end              DATE,
    period_label            VARCHAR(128),
    pending_packages        INT NOT NULL CHECK (pending_packages >= 0),
    to_process_packages     INT NOT NULL CHECK (to_process_packages >= 0),
    printed_packages        INT NOT NULL CHECK (printed_packages >= 0),
    pending_pickup_packages INT NOT NULL CHECK (pending_pickup_packages >= 0),
    dashboard_order_count   INT CHECK (dashboard_order_count >= 0),
    source_url              VARCHAR(512) NOT NULL DEFAULT 'https://sitegiant.co/dashboard',
    idempotency_key         VARCHAR(128) NOT NULL,
    captured_by_token_id    BIGINT REFERENCES wms_tokens(token_id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sitegiant_workload_period_order
        CHECK (period_start IS NULL OR period_end IS NULL OR period_end >= period_start),
    UNIQUE (warehouse_id, source_system, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_sitegiant_workload_warehouse_captured
    ON sitegiant_workload_snapshots (warehouse_id, captured_at DESC);

COMMIT;
