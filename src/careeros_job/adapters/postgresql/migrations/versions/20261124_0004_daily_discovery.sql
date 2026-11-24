-- migrate:up
ALTER TABLE job_records
    ADD COLUMN source_id VARCHAR(200),
    ADD COLUMN source_external_id VARCHAR(500),
    ADD COLUMN source_location VARCHAR(500),
    ADD COLUMN last_seen_at TIMESTAMPTZ,
    ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active',
    ADD CONSTRAINT ck_job_status
        CHECK (status IN ('active', 'saved', 'ignored', 'expired', 'applied'));

CREATE TABLE discovery_search_projections (
    tenant_id VARCHAR(36) NOT NULL,
    account_id VARCHAR(36) NOT NULL,
    event_id UUID NOT NULL UNIQUE,
    occurred_at TIMESTAMPTZ NOT NULL,
    criteria_version_id UUID NOT NULL,
    state VARCHAR(16) NOT NULL CHECK (state IN ('active', 'paused')),
    schedule_json JSONB NOT NULL,
    signature_json JSONB NOT NULL,
    last_run_local_date DATE,
    PRIMARY KEY (tenant_id, account_id)
);

CREATE TABLE discovery_runs (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    request_fingerprint VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL CHECK (state IN ('started', 'completed', 'failed')),
    result_json JSONB,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE discovery_source_checkpoints (
    source_id VARCHAR(200) NOT NULL,
    signature_id VARCHAR(64) NOT NULL,
    etag VARCHAR(500),
    last_modified VARCHAR(500),
    cursor VARCHAR(2000),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_id, signature_id)
);

CREATE TABLE discovery_observations (
    tenant_id VARCHAR(36) NOT NULL,
    account_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(200) NOT NULL,
    external_id VARCHAR(500) NOT NULL,
    material_hash VARCHAR(64) NOT NULL,
    current_opportunity_id UUID NOT NULL REFERENCES job_records(id),
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, account_id, source_id, external_id)
);

CREATE TABLE discovery_outbox (
    event_id UUID PRIMARY KEY,
    event_type VARCHAR(120) NOT NULL,
    schema_version VARCHAR(16) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ
);

CREATE INDEX ix_discovery_outbox_unpublished
    ON discovery_outbox (occurred_at) WHERE published_at IS NULL;
CREATE INDEX ix_discovery_projection_state
    ON discovery_search_projections (state, last_run_local_date);
-- migrate:down
DROP INDEX ix_discovery_projection_state;
DROP INDEX ix_discovery_outbox_unpublished;
DROP TABLE discovery_outbox;
DROP TABLE discovery_observations;
DROP TABLE discovery_source_checkpoints;
DROP TABLE discovery_runs;
DROP TABLE discovery_search_projections;
ALTER TABLE job_records
    DROP CONSTRAINT ck_job_status,
    DROP COLUMN status,
    DROP COLUMN last_seen_at,
    DROP COLUMN source_location,
    DROP COLUMN source_external_id,
    DROP COLUMN source_id;
