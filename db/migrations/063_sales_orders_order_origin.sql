-- 063: sales_orders.order_origin - free-text source-of-order label.
--
-- The inbound payload from connectors carries an upstream origin /
-- channel label per order ("amazon", "shopify-store-1", "phone-order",
-- "in-store-walkin", etc.). Distinct from:
--
--   * source_system (FK-gated allowlist of connectors that push to
--     Sentry; the value is "which connector pushed this")
--   * order_source (small enum 'web' / 'pos' added by mig 056 for the
--     POS endpoint surface)
--
-- order_origin is intentionally free-text with no FK / CHECK / enum,
-- so any value the connector hands us via the canonical-mapping
-- contract lands without a deploy. Length cap matches source_system
-- for symmetry; a 64-char cap keeps the column index-friendly and
-- rules out unbounded growth from a misconfigured mapping doc.
--
-- The admin Edit modal exposes the column as a free-text input;
-- ADMIN can edit at any status, base sales-orders USER at OPEN only
-- (same gate as the other PUT header fields).
--
-- No mapping yaml change in this migration. Operators wire up the
-- connector mapping doc to populate order_origin separately; until
-- then the column stays NULL on inbound writes and renders blank in
-- the admin UI.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped. ADD COLUMN nullable is
-- a metadata-only operation in PostgreSQL 11+ -- no table rewrite,
-- no full-table scan.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS order_origin VARCHAR(64);

COMMIT;
