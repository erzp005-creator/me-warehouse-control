-- 078: sales_orders.customer_email - customer email for display on the SO.
--
-- The POS checkout already collects customer_email (api/schemas/pos.py, a
-- receipt / loyalty capture field), but there was no SO column for it to
-- land in, so it was dropped. This adds the column so POS-captured email
-- persists and shows on the order. The inbound SO mapping can also populate
-- it when the upstream payload carries an email; that path stays NULL until
-- the sending system includes the field.
--
-- Free-text, nullable, no FK / CHECK -- matches the other customer_* fields
-- and customers.email (VARCHAR(255)). Display only; PII masking / access
-- gating is handled in a separate follow-up.
--
-- Migration discipline (V-213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped. ADD COLUMN nullable is metadata-only in
-- PostgreSQL 11+ -- no table rewrite, no full-table scan.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE sales_orders
    ADD COLUMN IF NOT EXISTS customer_email VARCHAR(255);

COMMENT ON COLUMN sales_orders.customer_email IS
    'Customer email for display on the SO. Supplied by the POS checkout and, when present, the inbound mapping. Free-text, nullable. (mig 078)';

COMMIT;
