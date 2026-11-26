-- migrate:up
ALTER TABLE job_records
    DROP CONSTRAINT job_records_source_kind_check,
    ALTER COLUMN employer DROP NOT NULL,
    ALTER COLUMN description DROP NOT NULL,
    ADD COLUMN source_original_url VARCHAR(2000),
    ADD COLUMN source_retrieval_provider VARCHAR(60),
    ADD CONSTRAINT job_records_source_kind_check
        CHECK (source_kind IN ('manual', 'greenhouse', 'open_web'));

-- migrate:down
ALTER TABLE job_records
    DROP CONSTRAINT job_records_source_kind_check,
    ALTER COLUMN employer SET NOT NULL,
    ALTER COLUMN description SET NOT NULL,
    DROP COLUMN source_original_url,
    DROP COLUMN source_retrieval_provider,
    ADD CONSTRAINT job_records_source_kind_check CHECK (source_kind = 'manual');
