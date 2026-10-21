CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS bronze.raw_records (
    bronze_record_id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    source_format TEXT NOT NULL
        CHECK (source_format IN ('json', 'csv')),
    source_file_checksum TEXT NOT NULL,
    source_record_number BIGINT NOT NULL
        CHECK (source_record_number > 0),
    source_record_id TEXT,
    raw_record_text TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    record_checksum TEXT NOT NULL,
    first_ingestion_run_id TEXT NOT NULL
        REFERENCES ops.pipeline_runs(pipeline_run_id),
    ingested_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_bronze_source_record
        UNIQUE (
            source_file,
            source_file_checksum,
            source_record_number
        ),

    CONSTRAINT ck_bronze_file_checksum
        CHECK (source_file_checksum ~ '^[0-9a-f]{64}$'),

    CONSTRAINT ck_bronze_record_checksum
        CHECK (record_checksum ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_bronze_source_file
    ON bronze.raw_records(source_file);

CREATE INDEX IF NOT EXISTS idx_bronze_source_record_id
    ON bronze.raw_records(source_record_id);

CREATE INDEX IF NOT EXISTS idx_bronze_ingestion_run
    ON bronze.raw_records(first_ingestion_run_id);

CREATE INDEX IF NOT EXISTS idx_bronze_raw_payload_gin
    ON bronze.raw_records
    USING GIN(raw_payload);
