-- ============================================================
-- Migration 070: post-fulfillment order types
-- ============================================================
-- Adds the order_type values that back the POS-created
-- post-fulfillment flows and the RMA page:
--
--   'replacement' -- a -REPLACEMENT outbound SO (same SKU, $0),
--                    created by the POS Replacement mode.
--   'exchange'    -- an -EXCHANGE outbound SO (different item +
--                    price delta), created by the POS Exchange mode.
--   'return'      -- a -RMA goods-in SO (the universal return
--                    record). Receiving books a `return` movement
--                    routed by destination (a sellable bin restocks,
--                    a defective / open-box bin quarantines).
--
-- All three reuse the existing parent_so_id FK and the so_number
-- suffix convention (<orig>-REPLACEMENT / -EXCHANGE / -RMA, alongside
-- the existing -REFUND), exactly as mig 067 added 'backorder'.
-- order_type stays VARCHAR(20); the longest new value ('replacement')
-- is 11 chars.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped. Constraint swap wrapped in
-- a DO $$ block so a partially-applied re-run does not abort.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

-- order_type CHECK expansion. Drop-then-add inside a DO block so a
-- partially-applied state (e.g. constraint already dropped by a prior
-- aborted run) does not abort the migration. Keeps the existing
-- ('sale','refund','backorder') values and adds the three
-- post-fulfillment types.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_orders_order_type_check'
    ) THEN
        ALTER TABLE sales_orders DROP CONSTRAINT sales_orders_order_type_check;
    END IF;

    ALTER TABLE sales_orders
        ADD CONSTRAINT sales_orders_order_type_check
        CHECK (order_type IN (
            'sale','refund','backorder','replacement','exchange','return'
        ));
END $$;

COMMENT ON COLUMN sales_orders.order_type IS
    'Journey type. App-enforced via api/constants.py plus a DB CHECK. Values: sale (default), refund (credit-memo, mig 056), backorder (mig 067), and the post-fulfillment types (mig 070) replacement, exchange, return. Post-fulfillment children link to the original via parent_so_id and carry a readable so_number suffix (-REPLACEMENT / -EXCHANGE / -RMA / -REFUND).';

COMMIT;
