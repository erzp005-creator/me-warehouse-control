-- Work Control automatic dispatch and daily employee availability.
--
-- The dispatcher schedules execution work only. It does not change inventory,
-- SiteGiant orders, commissions or KPI scores.

ALTER TABLE work_tasks
    ADD COLUMN IF NOT EXISTS complexity_level SMALLINT NOT NULL DEFAULT 2;

ALTER TABLE work_tasks
    DROP CONSTRAINT IF EXISTS work_tasks_complexity_level_check;
ALTER TABLE work_tasks
    ADD CONSTRAINT work_tasks_complexity_level_check
    CHECK (complexity_level BETWEEN 1 AND 5);

ALTER TABLE work_tasks
    ADD COLUMN IF NOT EXISTS estimated_minutes NUMERIC(8, 2);
ALTER TABLE work_tasks
    DROP CONSTRAINT IF EXISTS work_tasks_estimated_minutes_check;
ALTER TABLE work_tasks
    ADD CONSTRAINT work_tasks_estimated_minutes_check
    CHECK (estimated_minutes IS NULL OR estimated_minutes > 0);

ALTER TABLE work_tasks
    ADD COLUMN IF NOT EXISTS assignment_reason VARCHAR(500),
    ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS assigned_by VARCHAR(100);

CREATE INDEX IF NOT EXISTS ix_work_tasks_assignment
    ON work_tasks(warehouse_id, assigned_to, status, available_at);

CREATE TABLE IF NOT EXISTS work_worker_status (
    warehouse_id          INT NOT NULL REFERENCES warehouses(warehouse_id),
    user_id               INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    work_date             DATE NOT NULL DEFAULT CURRENT_DATE,
    availability_status   VARCHAR(16) NOT NULL DEFAULT 'AVAILABLE'
                              CHECK (availability_status IN ('AVAILABLE','BREAK','OFF_DUTY')),
    daily_capacity_minutes INT NOT NULL DEFAULT 480
                              CHECK (daily_capacity_minutes BETWEEN 60 AND 720),
    status_note           VARCHAR(500),
    updated_by            VARCHAR(100) NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (warehouse_id, user_id, work_date)
);

CREATE INDEX IF NOT EXISTS ix_work_worker_status_today
    ON work_worker_status(warehouse_id, work_date, availability_status);
