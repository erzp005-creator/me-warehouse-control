-- 081: correct the sales_orders.status comment. Documentation only.
--
-- Migration 067 introduced WAITING_STOCK and documented it as:
--
--   "WAITING_STOCK (mig 067) is a backorder-only off-ramp; a BO flips to
--    OPEN when receipt.completed makes all its lines satisfiable."
--
-- The second half of that is no longer true. A PO receipt was the only
-- release trigger because the matcher was called from one place, but stock
-- lands in a warehouse by several routes and a backorder waiting on a SKU
-- sitting in a pickable bin should not care which one delivered it.
-- Releases now also fire from direct adjustments, cycle-count approvals, the
-- adjustment CSV import, inter-warehouse transfers and the Pipe B inventory
-- sync.
--
-- Restock-on-revert paths deliberately do NOT release: an operator undoing a
-- pick or reverting a status has stock transiently back on the shelf, and
-- flipping a backorder open underneath them would make it pickable against
-- inventory that is about to move again.
--
-- No data or schema change. Safe to run at any time, and re-runnable.

COMMENT ON COLUMN sales_orders.status IS
    'Lifecycle: OPEN, PICKED, PACKED, SHIPPED, CANCELLED, REFUNDED, FRAUD_REVIEW, WAITING_STOCK. Enforced by api/constants.py, not by a DB CHECK. WAITING_STOCK (mig 067) is a backorder-only off-ramp; a BO flips to OPEN once any inventory increase in its warehouse makes all of its lines satisfiable (mig 081). REFUNDED added in mig 074.';
