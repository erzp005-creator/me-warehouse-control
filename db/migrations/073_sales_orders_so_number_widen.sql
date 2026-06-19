-- ============================================================
-- Migration 073: widen sales_orders.so_number
-- ============================================================
-- Post-fulfillment children carry a readable so_number suffix on the
-- original's number (<orig>-REPLACEMENT / -EXCHANGE / -RMA / -REFUND,
-- mig 070). VARCHAR(50) is too tight: a long reference / marketplace
-- original plus the 12-char '-REPLACEMENT' suffix can overflow 50 and
-- abort the child SO INSERT. Widen to VARCHAR(128) to match the existing
-- dockd_idempotency.so_number(128) (which already stores SO numbers),
-- giving comfortable headroom for any parent + suffix.
--
-- Widening a VARCHAR length limit is a metadata-only change in Postgres
-- (no table rewrite, no scan) -- safe + fast on a large table. The UNIQUE
-- constraint is unaffected by a length change.
--
-- NOTE: pair this with bumping `_SO_NUMBER_MAX` (api/constants.py, added in
-- the post-fulfillment PR) from 50 to 128 so the reference-order input
-- validation matches the wider column.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped. ALTER TYPE to a wider length is idempotent (a re-run
-- is a no-op).
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ALTER COLUMN so_number TYPE VARCHAR(128);

COMMENT ON COLUMN sales_orders.so_number IS
    'Human-readable SO number, UNIQUE. Widened to VARCHAR(128) (mig 073) to hold a post-fulfillment child (<orig>-REPLACEMENT / -EXCHANGE / -RMA / -REFUND) minted off a long reference / marketplace original. Matches dockd_idempotency.so_number(128).';

COMMIT;
