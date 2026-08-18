-- 076: sales_orders.voided_at / voided_by - soft-delete for return SOs (RMAs).
--
-- Operators occasionally create a return SO (order_type='return', the
-- <orig>-RMA goods-in record) by mistake and need to remove it from the
-- RMA page. Sentry has no order-level delete; the generic cancel
-- (sales_order_service.cancel_sales_order) unwinds outbound
-- allocation / picking, which is wrong for a goods-in return (see the
-- standing note at the top of admin/src/pages/RMA.jsx). This adds a
-- reversible soft-delete instead of a destructive row removal:
--
--   voided_at  -- when the return was voided; NULL = live. The
--                 sales-order list endpoint hides voided rows so they
--                 drop off the RMA page while the row (and its full
--                 audit trail) persists.
--   voided_by  -- actor username, denormalised alongside the audit_log
--                 RETURN_VOID entry for at-a-glance display.
--
-- Void is gated in the service to OPEN, un-received returns with no
-- linked refund, so a voided row can never strand received inventory.
-- A voided return is restorable by clearing voided_at.
--
-- Migration discipline (V-213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped. ADD COLUMN nullable is metadata-only in
-- PostgreSQL 11+ -- no table rewrite, no full-table scan.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS voided_by VARCHAR(255);

COMMENT ON COLUMN sales_orders.voided_at IS
    'Soft-delete timestamp for a return SO (RMA) voided by an operator. NULL = live. The sales-order list endpoint excludes rows where this is set. (mig 076)';
COMMENT ON COLUMN sales_orders.voided_by IS
    'Username that voided the return SO; paired with the RETURN_VOID audit_log entry. (mig 076)';

COMMIT;
