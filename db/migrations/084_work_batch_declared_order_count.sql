-- Pack Note sheets can state the real number of orders while exposing only
-- representative order numbers (for example, the first and last order).

ALTER TABLE work_batches
    ADD COLUMN IF NOT EXISTS declared_order_count INT;

UPDATE work_batches wb
   SET declared_order_count = counts.order_count
  FROM (
        SELECT batch_id, COUNT(*)::int AS order_count
          FROM work_batch_orders
         GROUP BY batch_id
       ) counts
 WHERE counts.batch_id = wb.batch_id
   AND wb.declared_order_count IS NULL;

ALTER TABLE work_batches
    ALTER COLUMN declared_order_count SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'work_batches_declared_order_count_check'
           AND conrelid = 'work_batches'::regclass
    ) THEN
        ALTER TABLE work_batches
            ADD CONSTRAINT work_batches_declared_order_count_check
            CHECK (declared_order_count BETWEEN 1 AND 50);
    END IF;
END;
$$;
