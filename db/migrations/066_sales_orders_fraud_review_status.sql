-- ============================================================
-- Migration 066: sales_orders FRAUD_REVIEW status
-- ============================================================
-- Backs the new Outbound > Fraud queue. A sales order can carry
-- status='FRAUD_REVIEW' to hold it out of the picking queue until a
-- CSR clears it (auto-flagged at ingest when the billing/shipping
-- heuristic is enabled, or set manually).
--
-- No DDL is required:
--   * status is plain VARCHAR(20) with no DB CHECK, so the new value
--     needs no column change; api/constants.py is the source of truth
--     for the allowed set (SO_FRAUD_REVIEW).
--   * the CSR memo field (sales_orders.memo) already shipped in
--     migration 055.
--
-- This migration only refreshes the inline column comment so schema
-- readers see the full status enum. Forward-only: it does not scan or
-- re-evaluate existing orders.
--
-- Migration discipline (V-213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

COMMENT ON COLUMN sales_orders.status IS
    'Lifecycle: OPEN, PICKED, PACKED, SHIPPED, CANCELLED, FRAUD_REVIEW. PICKING/PACKING retired in mig 060. Enforced by api/constants.py, not by a DB CHECK.';

COMMIT;
