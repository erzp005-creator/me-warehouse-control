-- 059: wave_pick_breakdown.so_line_id FK -> ON DELETE SET NULL.
--
-- Companion to mig 058 (pick_tasks.{so_line_id,to_line_id} -> SET NULL).
-- That migration moved pick_tasks off RESTRICT so a line delete can
-- remove a sales_order_lines row without first scrubbing every
-- historical pick_task. wave_pick_breakdown has the same shape -- a
-- per-SO audit row referencing sales_order_lines(so_line_id) with the
-- default-RESTRICT FK. Real symptom observed in production:
--
--     IntegrityError ... constraint=wave_pick_breakdown_so_line_id_fkey
--     detail=Key (so_line_id)=(4871) is still referenced from table
--     "wave_pick_breakdown"
--
-- Reproduction: admin Edit modal -> Release picked quantities (clears
-- pending pick_tasks via _release_so_line_allocation) -> click "X" to
-- delete the line. The DELETE on sales_order_lines fires inside the
-- update_sales_order_line / delete_sales_order_line handler and is
-- blocked by the wave_pick_breakdown FK.
--
-- Why SET NULL (not CASCADE): wave_pick_breakdown is an in-progress
-- pick-wave breakdown that doubles as audit. Keeping the row with
-- quantity / quantity_picked / short_quantity intact (with so_line_id
-- now NULL) preserves the wave-side audit trail when the SO line is
-- deleted under it -- same trade-off mig 058 makes for pick_tasks.
--
-- The NOT NULL on so_line_id must be dropped before SET NULL can fire;
-- DROP NOT NULL is metadata-only in PG (no table rewrite).
--
-- Data safety: this migration touches NO wave_pick_breakdown rows.
-- It only changes what the DB does on a future sales_order_lines DELETE.
-- Existing rows with valid so_line_id stay exactly as they are.
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped. The DROP+ADD on a single
-- FK + DROP NOT NULL on one column are fast: ACCESS EXCLUSIVE on
-- wave_pick_breakdown briefly, no scan, no row rewrite.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE wave_pick_breakdown
    ALTER COLUMN so_line_id DROP NOT NULL;

ALTER TABLE wave_pick_breakdown
    DROP CONSTRAINT IF EXISTS wave_pick_breakdown_so_line_id_fkey,
    ADD CONSTRAINT wave_pick_breakdown_so_line_id_fkey
        FOREIGN KEY (so_line_id) REFERENCES sales_order_lines(so_line_id)
        ON DELETE SET NULL;

COMMIT;
