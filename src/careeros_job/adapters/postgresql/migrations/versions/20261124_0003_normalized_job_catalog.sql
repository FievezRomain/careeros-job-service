-- migrate:up
ALTER TABLE job_records
    ADD COLUMN group_id UUID,
    ADD COLUMN version INTEGER,
    ADD COLUMN grouping_key VARCHAR(64),
    ADD COLUMN content_fingerprint VARCHAR(64);

UPDATE job_records
SET group_id = id,
    version = 1,
    grouping_key = encode(sha256(convert_to(id::text, 'UTF8')), 'hex'),
    content_fingerprint = encode(sha256(convert_to(id::text, 'UTF8')), 'hex');

ALTER TABLE job_records
    ALTER COLUMN group_id SET NOT NULL,
    ALTER COLUMN version SET NOT NULL,
    ALTER COLUMN grouping_key SET NOT NULL,
    ALTER COLUMN content_fingerprint SET NOT NULL,
    ADD CONSTRAINT ck_job_version_positive CHECK (version >= 1),
    ADD CONSTRAINT uq_job_group_content
        UNIQUE (tenant_id, owner_id, grouping_key, content_fingerprint),
    ADD CONSTRAINT uq_job_group_version
        UNIQUE (tenant_id, owner_id, group_id, version);

CREATE TABLE job_capture_commands (
    tenant_id VARCHAR(36) NOT NULL,
    owner_id VARCHAR(36) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    opportunity_id UUID NOT NULL REFERENCES job_records(id),
    PRIMARY KEY (tenant_id, owner_id, idempotency_key)
);

INSERT INTO job_capture_commands (
    tenant_id, owner_id, idempotency_key, request_fingerprint, opportunity_id
)
SELECT tenant_id, owner_id, idempotency_key, request_fingerprint, id
FROM job_records;

CREATE INDEX ix_job_records_owner_current
    ON job_records (tenant_id, owner_id, group_id, version DESC);
-- migrate:down
DROP INDEX ix_job_records_owner_current;
DROP TABLE job_capture_commands;
ALTER TABLE job_records
    DROP CONSTRAINT uq_job_group_version,
    DROP CONSTRAINT uq_job_group_content,
    DROP CONSTRAINT ck_job_version_positive,
    DROP COLUMN content_fingerprint,
    DROP COLUMN grouping_key,
    DROP COLUMN version,
    DROP COLUMN group_id;
