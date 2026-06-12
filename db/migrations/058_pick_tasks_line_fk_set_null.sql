-- 058: pick_tasks.{so_line_id, to_line_id} FK -> ON DELETE SET NULL.
--
-- pick_tasks are durable audit records of physical pick events
-- (item / bin / qty / who / when / status). The line-level FKs were
-- created with the PostgreSQL default ON DELETE NO ACTION (RESTRICT),
-- which meant any code path that deleted a sales_order_lines or
-- transfer_order_lines row would fail if any pick_task in any status
-- still pointed at it. Two real paths hit this:
--
--   * Partial fulfillment (upcoming) shrinks a line to zero by DELETE
--     sales_order_lines. Validation keeps the obvious case
--     (fully-picked line) from getting to the DELETE, but any
--     pick_task left behind in SHORT / SKIPPED state still blocks
--     the DELETE with an FK violation.
--   * Future delete-line code paths inherit the same footgun: forget
--     to clean up historical pick_tasks first, and the DELETE 409s.
--
-- ON DELETE SET NULL expresses the actual semantic at the schema
-- level: the pick_task survives line deletion as a free-standing
-- audit row, only the line pointer goes NULL. The pick_task keeps
-- pick_task_id, batch_id, so_id (still required by the target_xor
-- check), item_id, bin_id, quantity_to_pick, quantity_picked,
-- status, picked_by, picked_at, scan_confirmed -- which is the full
-- value of the audit record. The line pointer was only meaningful
-- while the line existed.
--
-- Scope: so_line_id and to_line_id only. batch_id / so_id / to_id /
-- bin_id / item_id stay RESTRICT because:
--   * batch_id / so_id / to_id are parent-aggregate references; the
--     parents are not hard-deleted in normal operation (cancel
--     leaves the row, only flips status).
--   * bin_id / item_id losing their references would destroy the
--     audit value (the record IS "I picked X units of item Y from
--     bin Z"; nulling either makes the row meaningless).
--
-- Data safety: this migration does not touch any pick_tasks rows.
-- It only changes what the DB does on a future sales_order_lines or
-- transfer_order_lines DELETE. Existing pick_tasks with valid line
-- pointers stay exactly as they are. Rollback (re-add the original
-- FK with default RESTRICT) is safe because any pick_tasks with
-- NULL so_line_id / to_line_id from intervening DELETEs are still
-- valid under RESTRICT (RESTRICT only blocks parent-row DELETEs
-- that would orphan a non-NULL reference).
--
-- v1.8.0 migration discipline (V-213): SET lock_timeout /
-- statement_timeout, BEGIN/COMMIT-wrapped. The DROP + ADD on a
-- single FK is fast: it acquires ACCESS EXCLUSIVE on pick_tasks
-- briefly, no full-table scan, no row rewrite.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE pick_tasks
    DROP CONSTRAINT pick_tasks_so_line_id_fkey,
    ADD CONSTRAINT pick_tasks_so_line_id_fkey
        FOREIGN KEY (so_line_id) REFERENCES sales_order_lines(so_line_id)
        ON DELETE SET NULL;

ALTER TABLE pick_tasks
    DROP CONSTRAINT pick_tasks_to_line_id_fkey,
    ADD CONSTRAINT pick_tasks_to_line_id_fkey
        FOREIGN KEY (to_line_id) REFERENCES transfer_order_lines(to_line_id)
        ON DELETE SET NULL;

COMMIT;
