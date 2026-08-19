-- 080: cycle_count_lines UNIQUE(count_id, item_id) - one line per item per count.
--
-- A cycle count is scoped to a single bin (cycle_counts.bin_id), so each
-- item should appear on exactly one cycle_count_line. Nothing enforced that.
-- A double-submit of a count (the mobile SUBMIT button could fire twice, and
-- submit_cycle_count re-checked status WITHOUT locking the count row) re-ran
-- the whole submit and re-INSERTed every "unexpected" line a second time.
-- The snapshot lines were spared (they UPDATE by count_line_id), but the
-- unexpected lines duplicated, became duplicate PENDING inventory_adjustments,
-- and approval applied each variance as a delta -- double-counting on-hand.
--
-- This constraint is the structural backstop. The submit handler now locks the
-- count row (SELECT ... FOR UPDATE) and re-checks status, the unexpected write
-- is idempotent per (count_id, item_id), and the create-time snapshot is
-- aggregated per item -- so the invariant holds for new counts.
--
-- PREREQUISITE: this fails if cycle_count_lines already holds duplicate
-- (count_id, item_id) rows. Clean them first -- find them with:
--   SELECT count_id, item_id, COUNT(*)
--     FROM cycle_count_lines GROUP BY count_id, item_id HAVING COUNT(*) > 1;
-- keep one line per (count_id, item_id), delete the surplus and any still
-- PENDING inventory_adjustments they produced. A clean database (CI / fresh
-- install) applies this immediately.
--
-- Migration discipline (V-213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped. ADD CONSTRAINT ... UNIQUE builds a unique index under
-- an ACCESS EXCLUSIVE lock; cycle_count_lines is small. Guarded by a
-- pg_constraint check so re-running the migration is a no-op.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_cycle_count_lines_count_item'
    ) THEN
        ALTER TABLE cycle_count_lines
            ADD CONSTRAINT uq_cycle_count_lines_count_item UNIQUE (count_id, item_id);
    END IF;
END $$;

COMMIT;
