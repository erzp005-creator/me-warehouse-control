-- ============================================================
-- Migration 074: backfill REFUNDED sales-order status
-- ============================================================
-- Refunds now have their own terminal status (REFUNDED) instead of
-- being recorded as CANCELLED. Going forward the POS full-refund path
-- and the cancel-with-reason=refunded path write REFUNDED directly;
-- this one-time pass relabels the EXISTING rows that are CANCELLED but
-- are genuinely refunds, so history reads consistently and the
-- cancellation-rate counters stop double-counting refunds.
--
-- A CANCELLED row is treated as a refund when ANY of:
--   * refunded_at IS NOT NULL        -- set by the POS full-refund path
--   * refund_so_id IS NOT NULL       -- the completing credit-memo SO
--   * cancellation_reason = 'refunded' -- operator cancel-with-reason
--
-- Pure relabel: touches only sales_orders.status. No inventory, money,
-- credit-memo, or event side effects -- those were recorded when the
-- refund originally happened. refunded_at / refund_so_id /
-- cancellation_reason are left intact as the audit trail.
--
-- Idempotent: a re-run matches nothing (the rows are REFUNDED now, no
-- longer CANCELLED). status is plain VARCHAR(20) (no DB CHECK), so no
-- type/constraint change is needed for the new value.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

UPDATE sales_orders
   SET status = 'REFUNDED'
 WHERE status = 'CANCELLED'
   AND (
        refunded_at IS NOT NULL
     OR refund_so_id IS NOT NULL
     OR cancellation_reason = 'refunded'
   );

COMMIT;
