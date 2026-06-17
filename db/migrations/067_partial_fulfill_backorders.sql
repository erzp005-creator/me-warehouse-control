-- ============================================================
-- Migration 067: partial-fulfill + backorders
-- ============================================================
-- Backs the new "Partially Fulfill" admin workflow plus the
-- /backorders Outbound surface.
--
-- Additive shape:
--
--   backorder_opened_at        -- set on a BO SO when it is created
--                                 via /partial-fulfill. NULL on every
--                                 non-BO SO.
--   backorder_fulfillable_at   -- set on a BO SO when receipt.completed
--                                 makes all its lines satisfiable and
--                                 the BO flips WAITING_STOCK -> OPEN.
--   cancellation_reason        -- on a CANCELLED SO, free-form-ish
--                                 enum (app-enforced, see
--                                 api/constants.py). Used by
--                                 /cancel-backorder and the parent
--                                 cancellation cascade.
--
--   order_type CHECK gains 'backorder'. The column was added in
--   mig 056 for POS refunds; backorder SOs reuse the existing
--   parent_so_id FK and disambiguate via order_type='backorder'.
--
--   sales_orders.status comment gains 'WAITING_STOCK'. The status
--   column is plain VARCHAR (no DB CHECK; matches mig 054 + mig 058
--   pattern). The new lifecycle slot is a backorder-only off-ramp:
--   a BO flips to OPEN when receipt.completed makes its lines
--   satisfiable, picker-side workflow then runs unchanged.
--
--   inventory_adjustments.reason_code comment gains 'SHORT'. Same
--   app-enforced enum pattern as the SO status; no DB CHECK.
--
--   ix_sales_orders_waiting_stock partial index covers the receipt
--   hook's "find WAITING_STOCK BOs in this warehouse, oldest first"
--   query.
--
-- v1.8.0 migration discipline (#213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped. Constraint adds wrapped
-- in DO $$ blocks so partial-apply re-runs do not abort.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS backorder_opened_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS backorder_fulfillable_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancellation_reason      VARCHAR(50);

-- order_type CHECK expansion. Drop-then-add inside a DO block so a
-- partially-applied state (e.g. constraint already dropped from a
-- prior aborted run) does not abort the migration. The replacement
-- constraint keeps the existing ('sale','refund') values and adds
-- ('backorder') for SOs created via the partial-fulfill endpoint.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_order_type_check'
    ) THEN
        ALTER TABLE sales_orders DROP CONSTRAINT sales_orders_order_type_check;
    END IF;

    ALTER TABLE sales_orders
        ADD CONSTRAINT sales_orders_order_type_check
        CHECK (order_type IN ('sale','refund','backorder'));
END $$;

COMMENT ON COLUMN sales_orders.status IS
    'Lifecycle: OPEN, PICKED, PACKED, SHIPPED, CANCELLED, FRAUD_REVIEW, WAITING_STOCK. Enforced by api/constants.py, not by a DB CHECK. WAITING_STOCK (mig 067) is a backorder-only off-ramp; a BO flips to OPEN when receipt.completed makes all its lines satisfiable.';

COMMENT ON COLUMN sales_order_lines.status IS
    'Lifecycle: PENDING, PICKED, PACKED, SHIPPED. ALLOCATED retired in mig 058 (never written by app code).';

COMMENT ON COLUMN inventory_adjustments.reason_code IS
    'Reason for the adjustment. App-enforced enum (no DB CHECK): CYCLE_COUNT, DAMAGE, FOUND, LOST, CORRECTION, SHORT. SHORT (mig 067) is written by /partial-fulfill when on-hand stock exists for a shorted line.';

COMMENT ON COLUMN sales_orders.cancellation_reason IS
    'On a CANCELLED SO, the reason the cancel was issued. App-enforced enum (no DB CHECK): found, customer_asked, refunded, parent_cancelled, other. Set by /cancel-backorder (operator-supplied) or by the parent-cancel cascade in sales_order_service.cancel_sales_order (value: parent_cancelled). NULL on non-cancelled SOs and on cancels that predate mig 067.';

CREATE INDEX IF NOT EXISTS ix_sales_orders_waiting_stock
    ON sales_orders (warehouse_id, backorder_opened_at)
    WHERE status = 'WAITING_STOCK';

COMMIT;
