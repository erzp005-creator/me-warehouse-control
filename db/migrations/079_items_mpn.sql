-- 079: items.mpn - manufacturer part number on the SKU master.
--
-- Items carry a manufacturer part number (MPN) from the upstream item
-- master alongside the SKU and UPC. It is a buyer-/receiver-facing
-- identifier: the manufacturer's own catalogue number, used to match an
-- arriving shipment line back to what was ordered when the SKU and UPC are
-- not enough on their own. It is added as a sibling of upc:
--
--   mpn  -- nullable manufacturer part number. NULL = unknown / not yet
--           supplied. Not DB-unique: a single MPN can legitimately map to
--           multiple SKUs (kits, re-packs, reseller renames) and is shared
--           across manufacturers, so uniqueness is neither true nor enforced.
--
-- ix_items_mpn supports lookup/search by MPN in the admin item and
-- inventory search paths (mirrors ix_items_upc).
--
-- Inbound sync of MPN is optional: a deployment maps the field in its
-- inbound mapping doc when the sending system carries an MPN, and leaves
-- it unmapped otherwise. This column must exist either way, or the
-- inbound mapping boot guard (_validate_canonical_columns) would refuse
-- to start once the field is mapped.
--
-- Migration discipline (V-213): SET lock_timeout / statement_timeout,
-- BEGIN/COMMIT-wrapped. ADD COLUMN nullable is metadata-only in
-- PostgreSQL 11+ -- no table rewrite, no full-table scan. CREATE INDEX is
-- not CONCURRENT (it takes a brief ACCESS SHARE-blocking lock); items is
-- small and this runs inside the maintenance window with the other DDL.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

ALTER TABLE items
    ADD COLUMN IF NOT EXISTS mpn VARCHAR(64);

COMMENT ON COLUMN items.mpn IS
    'Manufacturer part number, sourced from the item master feed. Nullable, not unique; a sibling of upc used for buyer-/receiver-facing identification. (mig 079)';

CREATE INDEX IF NOT EXISTS ix_items_mpn ON items(mpn);

COMMIT;
