-- ME Warehouse Control
--
-- Adds a warehouse-execution layer that records assignment, time, evidence,
-- receiving drafts and error investigations without owning stock or order
-- accounting. The upstream ERP/WMS remains the source of truth.

CREATE TABLE IF NOT EXISTS work_batches (
    batch_id        BIGSERIAL PRIMARY KEY,
    warehouse_id    INT NOT NULL REFERENCES warehouses(warehouse_id),
    source_system   VARCHAR(64) NOT NULL DEFAULT 'manual',
    pack_note_ref   VARCHAR(128) NOT NULL,
    platform        VARCHAR(32),
    priority        SMALLINT NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    status          VARCHAR(20) NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','IN_PROGRESS','COMPLETED','CANCELLED')),
    available_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (warehouse_id, source_system, pack_note_ref)
);

CREATE TABLE IF NOT EXISTS work_batch_orders (
    batch_order_id  BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES work_batches(batch_id) ON DELETE CASCADE,
    order_number    VARCHAR(128) NOT NULL,
    courier_barcode VARCHAR(128),
    platform        VARCHAR(32),
    sku_count       INT NOT NULL DEFAULT 0 CHECK (sku_count >= 0),
    unit_count      INT NOT NULL DEFAULT 0 CHECK (unit_count >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, order_number),
    UNIQUE (batch_id, courier_barcode)
);

CREATE INDEX IF NOT EXISTS ix_work_batch_orders_barcode
    ON work_batch_orders(courier_barcode) WHERE courier_barcode IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_work_batch_orders_batch ON work_batch_orders(batch_id);

CREATE OR REPLACE FUNCTION work_batch_enforce_order_limit() RETURNS TRIGGER AS $$
BEGIN
    IF (SELECT COUNT(*) FROM work_batch_orders WHERE batch_id = NEW.batch_id) >= 50 THEN
        RAISE EXCEPTION 'A work batch cannot contain more than 50 orders';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS work_batch_order_limit ON work_batch_orders;
CREATE TRIGGER work_batch_order_limit
    BEFORE INSERT ON work_batch_orders
    FOR EACH ROW EXECUTE FUNCTION work_batch_enforce_order_limit();

CREATE TABLE IF NOT EXISTS work_tasks (
    task_id          BIGSERIAL PRIMARY KEY,
    batch_id         BIGINT REFERENCES work_batches(batch_id) ON DELETE SET NULL,
    warehouse_id     INT NOT NULL REFERENCES warehouses(warehouse_id),
    task_type        VARCHAR(24) NOT NULL
                         CHECK (task_type IN ('PICKING','PACKING','RECEIVING','PUTAWAY','STOCK_CHECK','OTHER')),
    status           VARCHAR(20) NOT NULL DEFAULT 'QUEUED'
                         CHECK (status IN ('QUEUED','ASSIGNED','CLAIMED','IN_PROGRESS','PAUSED','COMPLETED','CANCELLED')),
    priority         SMALLINT NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    assigned_to      VARCHAR(100),
    claimed_by       VARCHAR(100),
    source_ref       VARCHAR(128),
    idempotency_key  VARCHAR(128) UNIQUE,
    order_count      INT NOT NULL DEFAULT 0 CHECK (order_count >= 0),
    sku_count        INT NOT NULL DEFAULT 0 CHECK (sku_count >= 0),
    unit_count       INT NOT NULL DEFAULT 0 CHECK (unit_count >= 0),
    complexity_note  VARCHAR(500),
    available_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at       TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    active_started_at TIMESTAMPTZ,
    pause_started_at TIMESTAMPTZ,
    active_seconds   INT NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
    paused_seconds   INT NOT NULL DEFAULT 0 CHECK (paused_seconds >= 0),
    last_event_at    TIMESTAMPTZ,
    created_by       VARCHAR(100) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_work_tasks_queue
    ON work_tasks(warehouse_id, status, priority DESC, available_at, task_id);
CREATE INDEX IF NOT EXISTS ix_work_tasks_batch ON work_tasks(batch_id, task_type);
CREATE INDEX IF NOT EXISTS ix_work_tasks_worker ON work_tasks(claimed_by, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_work_tasks_one_active_per_worker
    ON work_tasks(claimed_by)
    WHERE claimed_by IS NOT NULL
      AND status IN ('CLAIMED','IN_PROGRESS','PAUSED');

CREATE TABLE IF NOT EXISTS work_task_events (
    event_id      BIGSERIAL PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES work_tasks(task_id) ON DELETE RESTRICT,
    event_type    VARCHAR(20) NOT NULL
                      CHECK (event_type IN ('CREATED','ASSIGNED','CLAIMED','VERIFIED','STARTED','PAUSED','RESUMED','COMPLETED','EXCEPTION','CANCELLED','REOPENED')),
    user_id       VARCHAR(100) NOT NULL,
    reason_code   VARCHAR(64),
    notes         VARCHAR(1000),
    metadata      JSONB,
    device_id     VARCHAR(100),
    event_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_work_task_events_task ON work_task_events(task_id, event_at);
CREATE INDEX IF NOT EXISTS ix_work_task_events_user ON work_task_events(user_id, event_at);

CREATE OR REPLACE FUNCTION work_control_reject_event_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'work_task_events are append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS work_task_events_no_update ON work_task_events;
CREATE TRIGGER work_task_events_no_update
    BEFORE UPDATE ON work_task_events
    FOR EACH ROW EXECUTE FUNCTION work_control_reject_event_mutation();
DROP TRIGGER IF EXISTS work_task_events_no_delete ON work_task_events;
CREATE TRIGGER work_task_events_no_delete
    BEFORE DELETE ON work_task_events
    FOR EACH ROW EXECUTE FUNCTION work_control_reject_event_mutation();

CREATE TABLE IF NOT EXISTS work_errors (
    error_id             BIGSERIAL PRIMARY KEY,
    warehouse_id         INT NOT NULL REFERENCES warehouses(warehouse_id),
    task_id              BIGINT REFERENCES work_tasks(task_id) ON DELETE SET NULL,
    batch_id             BIGINT REFERENCES work_batches(batch_id) ON DELETE SET NULL,
    batch_order_id       BIGINT REFERENCES work_batch_orders(batch_order_id) ON DELETE SET NULL,
    error_type           VARCHAR(40) NOT NULL,
    severity             VARCHAR(12) NOT NULL DEFAULT 'MEDIUM'
                             CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status               VARCHAR(16) NOT NULL DEFAULT 'PENDING'
                             CHECK (status IN ('PENDING','CONFIRMED','DISMISSED')),
    responsibility       VARCHAR(20) NOT NULL DEFAULT 'UNCONFIRMED'
                             CHECK (responsibility IN ('UNCONFIRMED','PICKER','PACKER','BOTH','SUPPLIER','SOURCE_DATA','SYSTEM','UNKNOWN')),
    discovered_stage     VARCHAR(24),
    reported_by          VARCHAR(100) NOT NULL,
    picker_user_id       VARCHAR(100),
    packer_user_id       VARCHAR(100),
    courier_barcode      VARCHAR(128),
    order_number         VARCHAR(128),
    sku                   VARCHAR(128),
    quantity              INT CHECK (quantity IS NULL OR quantity > 0),
    description          VARCHAR(2000),
    resolution_notes     VARCHAR(2000),
    reviewed_by          VARCHAR(100),
    reviewed_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_work_errors_queue
    ON work_errors(warehouse_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_work_errors_batch ON work_errors(batch_id);

CREATE TABLE IF NOT EXISTS receiving_drafts (
    receiving_id    BIGSERIAL PRIMARY KEY,
    task_id         BIGINT UNIQUE REFERENCES work_tasks(task_id) ON DELETE SET NULL,
    warehouse_id    INT NOT NULL REFERENCES warehouses(warehouse_id),
    source_system   VARCHAR(64) NOT NULL DEFAULT 'manual',
    po_number       VARCHAR(128),
    supplier_ref    VARCHAR(128),
    status          VARCHAR(16) NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT','SUBMITTED','APPROVED','REJECTED','POSTED')),
    counted_by      VARCHAR(100) NOT NULL,
    notes           VARCHAR(2000),
    submitted_at    TIMESTAMPTZ,
    reviewed_by     VARCHAR(100),
    reviewed_at     TIMESTAMPTZ,
    review_notes    VARCHAR(2000),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_receiving_drafts_queue
    ON receiving_drafts(warehouse_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS receiving_draft_lines (
    receiving_line_id BIGSERIAL PRIMARY KEY,
    receiving_id      BIGINT NOT NULL REFERENCES receiving_drafts(receiving_id) ON DELETE CASCADE,
    sku               VARCHAR(128) NOT NULL,
    expected_quantity INT CHECK (expected_quantity IS NULL OR expected_quantity >= 0),
    received_quantity INT NOT NULL CHECK (received_quantity >= 0),
    good_quantity     INT NOT NULL DEFAULT 0 CHECK (good_quantity >= 0),
    damaged_quantity  INT NOT NULL DEFAULT 0 CHECK (damaged_quantity >= 0),
    short_quantity    INT NOT NULL DEFAULT 0 CHECK (short_quantity >= 0),
    over_quantity     INT NOT NULL DEFAULT 0 CHECK (over_quantity >= 0),
    notes             VARCHAR(1000),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (received_quantity = good_quantity + damaged_quantity)
);

CREATE INDEX IF NOT EXISTS ix_receiving_draft_lines_parent
    ON receiving_draft_lines(receiving_id);

CREATE TABLE IF NOT EXISTS work_evidence (
    evidence_id       BIGSERIAL PRIMARY KEY,
    warehouse_id      INT NOT NULL REFERENCES warehouses(warehouse_id),
    error_id          BIGINT REFERENCES work_errors(error_id) ON DELETE CASCADE,
    receiving_id      BIGINT REFERENCES receiving_drafts(receiving_id) ON DELETE CASCADE,
    receiving_line_id BIGINT REFERENCES receiving_draft_lines(receiving_line_id) ON DELETE CASCADE,
    storage_key       VARCHAR(255) NOT NULL UNIQUE,
    original_filename VARCHAR(255) NOT NULL,
    content_type      VARCHAR(64) NOT NULL,
    byte_size         INT NOT NULL CHECK (byte_size > 0 AND byte_size <= 10485760),
    sha256            CHAR(64) NOT NULL,
    note              VARCHAR(500),
    uploaded_by       VARCHAR(100) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (num_nonnulls(error_id, receiving_id, receiving_line_id) = 1)
);

CREATE INDEX IF NOT EXISTS ix_work_evidence_error ON work_evidence(error_id);
CREATE INDEX IF NOT EXISTS ix_work_evidence_receiving ON work_evidence(receiving_id);
