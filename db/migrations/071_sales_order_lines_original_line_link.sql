-- ============================================================
-- Migration 071: post-fulfillment child-line linkage
-- ============================================================
-- A post-fulfillment child SO's lines (return / replacement / exchange)
-- point back to the original order line they derive from, so the grouped
-- case view and per-line return tracking can tie a child line to its
-- source. Nullable self-FK; NULL on every ordinary sale / backorder line.
--
-- v1.8.0 migration discipline: SET timeouts, BEGIN/COMMIT, ADD COLUMN
-- IF NOT EXISTS so a partially-applied re-run does not abort.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_order_lines
    ADD COLUMN IF NOT EXISTS original_so_line_id INT
        REFERENCES sales_order_lines(so_line_id);

COMMENT ON COLUMN sales_order_lines.original_so_line_id IS
    'For a post-fulfillment child line (return / replacement / exchange), the original sales_order_lines.so_line_id it derives from. NULL on ordinary sale / backorder lines. Drives the grouped case view and per-line return tracking (mig 071).';

COMMIT;
