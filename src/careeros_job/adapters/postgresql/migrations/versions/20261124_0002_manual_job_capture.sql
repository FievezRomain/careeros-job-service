-- migrate:up
ALTER TABLE job_records
    ADD COLUMN tenant_id VARCHAR(36) NOT NULL,
    ADD COLUMN title VARCHAR(200) NOT NULL,
    ADD COLUMN employer VARCHAR(200) NOT NULL,
    ADD COLUMN description VARCHAR(10000) NOT NULL,
    ADD COLUMN source_kind VARCHAR(32) NOT NULL CHECK (source_kind = 'manual'),
    ADD COLUMN source_reference VARCHAR(2000) NOT NULL,
    ADD COLUMN raw_source TEXT NOT NULL,
    ADD COLUMN captured_at TIMESTAMPTZ NOT NULL,
    ADD COLUMN idempotency_key VARCHAR(128) NOT NULL,
    ADD COLUMN request_fingerprint VARCHAR(64) NOT NULL,
    ADD CONSTRAINT uq_job_owner_idempotency UNIQUE (tenant_id, owner_id, idempotency_key);
-- migrate:down
ALTER TABLE job_records
    DROP CONSTRAINT uq_job_owner_idempotency,
    DROP COLUMN request_fingerprint,
    DROP COLUMN idempotency_key,
    DROP COLUMN captured_at,
    DROP COLUMN raw_source,
    DROP COLUMN source_reference,
    DROP COLUMN source_kind,
    DROP COLUMN description,
    DROP COLUMN employer,
    DROP COLUMN title,
    DROP COLUMN tenant_id;
