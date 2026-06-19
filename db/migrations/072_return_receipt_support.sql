-- ============================================================
-- Migration 072: return-receipt support
-- ============================================================
-- Goods coming back against a return SO (the <orig>-RMA goods-in) are
-- received the same way a PO is received: an item_receipts row + an
-- inventory bump + a completion event. Two additive shapes make that
-- possible:
--
--   item_receipts.so_id / so_line_id -- a receipt booked against a
--     return SO instead of a PO. po_id / po_line_id are NULL on a
--     return receipt; so_id / so_line_id are NULL on a PO receipt.
--     The existing bin_id / warehouse_id carry the disposition (a
--     sellable bin restocks, a defective / open-box bin quarantines);
--     a downstream ledger maps the destination to its GL treatment.
--
--   sales_order_lines.quantity_received -- denormalised per-line
--     received count for a return SO's lines, mirroring
--     purchase_order_lines.quantity_received. 0 on ordinary sale lines.
--
-- v1.8.0 migration discipline: SET timeouts, BEGIN/COMMIT, ADD COLUMN
-- IF NOT EXISTS so a partially-applied re-run does not abort.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE item_receipts
    ADD COLUMN IF NOT EXISTS so_id      INT REFERENCES sales_orders(so_id),
    ADD COLUMN IF NOT EXISTS so_line_id INT REFERENCES sales_order_lines(so_line_id);

ALTER TABLE sales_order_lines
    ADD COLUMN IF NOT EXISTS quantity_received INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN item_receipts.so_id IS
    'For a return receipt, the return SO (order_type=return, the <orig>-RMA) the goods came back against. NULL on a PO receipt (which sets po_id). bin_id/warehouse_id carry the disposition. (mig 072)';
COMMENT ON COLUMN item_receipts.so_line_id IS
    'For a return receipt, the return SO line received against. NULL on a PO receipt. (mig 072)';
COMMENT ON COLUMN sales_order_lines.quantity_received IS
    'Denormalised received count for a return SO line (goods back against the <orig>-RMA). 0 on ordinary sale lines. Mirrors purchase_order_lines.quantity_received. (mig 072)';

COMMIT;
