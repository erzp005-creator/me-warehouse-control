-- ============================================================
-- Migration 077: composite (po_id, item_id) index for receiving
-- ============================================================
-- receive_items() resolves the matching PO line for each received item
-- with WHERE po_id = :po_id AND item_id = :item_id (api/routes/
-- receiving.py). The only index was ix_purchase_order_lines_po(po_id),
-- so each lookup scanned every line on the PO and filtered by item_id:
-- O(lines) per item, ~O(lines^2) across receiving a whole PO. 700+ line
-- POs were slow on the floor.
--
-- A composite (po_id, item_id) index turns each lookup into a direct
-- probe. Its leading po_id column also serves the plain WHERE po_id
-- scans (lookup_po, PO-status recompute), so the single-column
-- ix_purchase_order_lines_po is now redundant -- drop it, matching mig
-- 074's drop-the-now-covered-index rule. One index to maintain on write
-- instead of two.
--
-- Migration discipline: lock/statement timeouts, BEGIN/COMMIT-wrapped.
-- CREATE INDEX IF NOT EXISTS / DROP INDEX IF EXISTS make a re-run a
-- no-op.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

CREATE INDEX IF NOT EXISTS ix_purchase_order_lines_po_item
    ON purchase_order_lines (po_id, item_id);

DROP INDEX IF EXISTS ix_purchase_order_lines_po;

COMMIT;
