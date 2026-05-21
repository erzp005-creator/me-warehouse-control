# Stuck-pick-after-revert recovery

Symptom: an operator reverts an SO from PICKED back to OPEN with
release-all, then re-scans to pick. The handheld presents only a
subset of lines and auto-completes early; the home screen (pre-
hotfix mobile) shows the batch as "Resume"-able but tapping Resume
jumps straight to a Batch Complete screen that never actually
completes. The SO ends up with some lines stuck at `quantity_picked = 0`
that the picker was never prompted for.

Root cause is fixed by the code change in
`api/services/sales_order_service.py` (`revert_sales_order_status`
now decrements `sales_order_lines.quantity_allocated` alongside
`quantity_picked`). This runbook covers cleanup of SOs that were
already in the stuck state before the fix deployed.

## When to run this

Run when you find an SO that:
- Has status OPEN (or any non-terminal status), AND
- Has one or more lines with `quantity_picked < quantity_ordered`, AND
- Has no live `pick_tasks` (PENDING or PICKED) covering the remaining
  `quantity_ordered - quantity_picked` units, AND
- Has `quantity_allocated > 0` on those same lines (the stale residue).

Step 1's diagnostic query enumerates every affected SO; run it first to
build the list of so_ids you will feed into Steps 2 and 3.

## Step 1: Diagnose the residue

Run against the prod database (read-only):

```sql
-- All lines where allocation appears stale (allocated > 0 but no
-- live pick_task accounts for the units). Returns the per-line
-- delta that needs to be zeroed.
SELECT
    so.so_id,
    so.so_number,
    so.status                                            AS so_status,
    sol.so_line_id,
    i.sku,
    sol.quantity_ordered,
    sol.quantity_allocated                               AS sol_allocated,
    sol.quantity_picked,
    COALESCE(live.live_alloc, 0)                         AS live_alloc,
    sol.quantity_allocated - COALESCE(live.live_alloc, 0)
                                                          AS stale_residue
FROM sales_order_lines sol
JOIN sales_orders so       ON so.so_id   = sol.so_id
JOIN items i               ON i.item_id  = sol.item_id
LEFT JOIN LATERAL (
    SELECT SUM(pt.quantity_to_pick) AS live_alloc
      FROM pick_tasks pt
     WHERE pt.so_line_id = sol.so_line_id
       AND pt.status IN ('PENDING', 'PICKED')
) live ON TRUE
WHERE sol.quantity_allocated > COALESCE(live.live_alloc, 0)
  AND so.status NOT IN ('CANCELLED', 'SHIPPED')
ORDER BY so.so_id, sol.so_line_id;
```

Lines with `stale_residue > 0` are candidates for cleanup.

## Step 2: Cancel any in-progress batches the affected SOs sit in

Some affected SOs may also be linked to a `pick_batches` row in
`IN_PROGRESS` or `OPEN` status from the failed pick attempt. Those
batches must be cancelled first via the API (NOT direct SQL) so the
new `full_revert_batch` helper restores inventory and writes the
correct audit rows.

```bash
# 1. Identify any active batches containing the affected SOs.
psql -c "
  SELECT DISTINCT pb.batch_id, pb.batch_number, pb.status, pbo.so_id
    FROM pick_batches pb
    JOIN pick_batch_orders pbo USING (batch_id)
   WHERE pbo.so_id IN (<so_id>, ...)   -- the so_ids from Step 1
     AND pb.status IN ('OPEN', 'IN_PROGRESS');
"

# 2. For each batch_id returned, POST cancel-batch:
curl -X POST "$SENTRY_PROD_BASE_URL/api/picking/cancel-batch" \
  -H "Authorization: Bearer $SENTRY_PROD_JWT" \
  -H "Content-Type: application/json" \
  -d '{"batch_id": <BATCH_ID>}'
```

The cancel-batch endpoint now (post-hotfix) does the full revert:
inventory restored, `sol.quantity_picked` and `quantity_allocated`
both reset, picked tasks flipped to RELEASED.

## Step 3: Clear the residue on lines that have NO active batch

For lines where Step 1 reported `stale_residue > 0` AND Step 2
found no batches (or you have already cancelled them), the residue
is pure left-over state from before the fix. Clear it in a
transaction:

```sql
BEGIN;

-- Audit trail: log what we are about to do before changing it.
INSERT INTO audit_log (
    action_type, entity_type, entity_id, user_id, warehouse_id, details
)
SELECT
    'SO_ALLOCATION_RELEASED',
    'SO',
    sol.so_id,
    'system:recovery',
    so.warehouse_id,
    jsonb_build_object(
        'so_line_id', sol.so_line_id,
        'released_quantity', sol.quantity_allocated - COALESCE(live.live_alloc, 0),
        'reason', 'pre_hotfix_revert_residue_cleanup',
        'runbook', 'stuck-pick-after-revert-recovery'
    )
FROM sales_order_lines sol
JOIN sales_orders so ON so.so_id = sol.so_id
LEFT JOIN LATERAL (
    SELECT SUM(pt.quantity_to_pick) AS live_alloc
      FROM pick_tasks pt
     WHERE pt.so_line_id = sol.so_line_id
       AND pt.status IN ('PENDING', 'PICKED')
) live ON TRUE
WHERE so.so_id IN (<so_id>, ...)       -- the so_ids from Step 1
  AND sol.quantity_allocated > COALESCE(live.live_alloc, 0);

-- Apply the residue cleanup: clamp sol.quantity_allocated down to
-- whatever live pick_tasks (PENDING + PICKED) actually account
-- for. GREATEST keeps zero from going negative on edge cases.
UPDATE sales_order_lines sol
   SET quantity_allocated = GREATEST(
           0,
           COALESCE((
               SELECT SUM(pt.quantity_to_pick)
                 FROM pick_tasks pt
                WHERE pt.so_line_id = sol.so_line_id
                  AND pt.status IN ('PENDING', 'PICKED')
           ), 0)
       )
 WHERE sol.so_id IN (<so_id>, ...)     -- the so_ids from Step 1
   AND sol.quantity_allocated > COALESCE((
           SELECT SUM(pt.quantity_to_pick)
             FROM pick_tasks pt
            WHERE pt.so_line_id = sol.so_line_id
              AND pt.status IN ('PENDING', 'PICKED')
       ), 0);

-- Verify before commit -- the SELECT below should now return zero
-- rows for the same so_ids. If it still returns rows, ROLLBACK
-- and investigate before retrying.
SELECT sol.so_line_id, sol.quantity_allocated, sol.quantity_picked,
       COALESCE(live.live_alloc, 0) AS live_alloc
  FROM sales_order_lines sol
  LEFT JOIN LATERAL (
      SELECT SUM(pt.quantity_to_pick) AS live_alloc
        FROM pick_tasks pt
       WHERE pt.so_line_id = sol.so_line_id
         AND pt.status IN ('PENDING', 'PICKED')
  ) live ON TRUE
 WHERE sol.so_id IN (<so_id>, ...)     -- the so_ids from Step 1
   AND sol.quantity_allocated > COALESCE(live.live_alloc, 0);

COMMIT;
```

## Step 4: Validate

After cleanup, the picker can re-scan the affected SOs and the
fresh batch will include every un-picked line. Verify by either:

- Asking the operator to re-scan and confirm the line count matches
  the SO's open lines.
- Running create_pick_batch dry against the SO via the admin SO
  detail endpoint -- the un-allocated lines should now appear with
  `quantity_ordered > quantity_allocated`.

## Why not script this end-to-end

The diagnose + cancel-batches + clear-residue dance is intentionally
manual: each step has a different blast radius, and the cancel-batch
step routes through the API (which writes proper audit rows). The
residue cleanup is the only direct SQL, and it is written so the
verification query before COMMIT catches any unexpected state.
