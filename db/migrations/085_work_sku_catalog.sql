-- Local SKU reference catalog for Warehouse Control.
-- SiteGiant remains the product source of truth; this table stores only the
-- fields warehouse staff need to identify goods during arrival counting.

CREATE TABLE IF NOT EXISTS work_sku_catalog (
    sku_catalog_id  BIGSERIAL PRIMARY KEY,
    warehouse_id    INT NOT NULL REFERENCES warehouses(warehouse_id),
    sku             VARCHAR(128) NOT NULL,
    sku_normalized  VARCHAR(128) GENERATED ALWAYS AS (UPPER(BTRIM(sku))) STORED,
    item_name       VARCHAR(500) NOT NULL,
    source_system   VARCHAR(64) NOT NULL DEFAULT 'manual',
    source_item_id  VARCHAR(64),
    source_item_url VARCHAR(512),
    image_url       VARCHAR(1024),
    needs_review    BOOLEAN NOT NULL DEFAULT TRUE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_sync_run   UUID,
    synced_at       TIMESTAMPTZ,
    created_by      VARCHAR(100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_work_sku_catalog_warehouse_sku
        UNIQUE (warehouse_id, sku_normalized),
    CONSTRAINT ck_work_sku_catalog_sku_not_blank
        CHECK (BTRIM(sku) <> ''),
    CONSTRAINT ck_work_sku_catalog_name_not_blank
        CHECK (BTRIM(item_name) <> '')
);

CREATE INDEX IF NOT EXISTS ix_work_sku_catalog_search
    ON work_sku_catalog (warehouse_id, is_active, sku_normalized);
CREATE INDEX IF NOT EXISTS ix_work_sku_catalog_sync
    ON work_sku_catalog (warehouse_id, source_system, last_sync_run);

ALTER TABLE receiving_draft_lines
    ADD COLUMN IF NOT EXISTS sku_catalog_id BIGINT
        REFERENCES work_sku_catalog(sku_catalog_id) ON DELETE SET NULL;
ALTER TABLE receiving_draft_lines
    ADD COLUMN IF NOT EXISTS item_name VARCHAR(500);

CREATE INDEX IF NOT EXISTS ix_receiving_draft_lines_sku_catalog
    ON receiving_draft_lines(sku_catalog_id);
CREATE INDEX IF NOT EXISTS ix_work_evidence_receiving_line
    ON work_evidence(receiving_line_id);
