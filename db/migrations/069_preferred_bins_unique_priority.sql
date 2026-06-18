-- ============================================================
-- Migration 069: preferred_bins strict priority hierarchy
-- ============================================================
-- preferred_bins enforced UNIQUE(item_id, bin_id) but nothing on
-- (item_id, priority), so an item could carry two bins at the SAME
-- priority. When the admin search / putaway JOINs preferred_bins the
-- SKU then renders twice at the same priority -> ambiguous putaway,
-- which can block receiving when items accumulate two or more bins at
-- the same priority. A preferred bin list must be a strict hierarchy:
-- exactly one bin per priority.
--
-- Add UNIQUE(item_id, priority). DEFERRABLE INITIALLY DEFERRED so the
-- write paths can renumber an item's bins within a transaction (the
-- "set as primary" bump and the admin auto-resequence pass through
-- transient duplicate states that only need to be unique at COMMIT).
-- The non-unique ix_preferred_bins_item_priority is now redundant --
-- the unique constraint's index serves the same (item_id, priority)
-- and (item_id) prefix lookups -- so drop it.
--
-- PRECONDITION: existing duplicate (item_id, priority) rows must be
-- deduped BEFORE this runs, or ADD CONSTRAINT aborts building the
-- unique index. That cleanup is operator-run; this migration fails
-- fast (correctly) if duplicates remain.
--
-- v1.8.0 migration discipline (#213): lock/statement timeouts,
-- BEGIN/COMMIT-wrapped. DROP CONSTRAINT IF EXISTS makes a re-run a
-- no-op.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE preferred_bins
    DROP CONSTRAINT IF EXISTS preferred_bins_item_priority_key;

ALTER TABLE preferred_bins
    ADD CONSTRAINT preferred_bins_item_priority_key
        UNIQUE (item_id, priority) DEFERRABLE INITIALLY DEFERRED;

DROP INDEX IF EXISTS ix_preferred_bins_item_priority;

COMMIT;
