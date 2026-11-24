-- migrate:up
CREATE TABLE job_records (
    id UUID PRIMARY KEY,
    owner_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- migrate:down
DROP TABLE job_records;
