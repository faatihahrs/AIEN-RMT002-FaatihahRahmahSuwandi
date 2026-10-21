CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
    pipeline_run_id TEXT PRIMARY KEY,
    dag_run_id TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'HOLD')),
    raw_root TEXT NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at_utc TIMESTAMPTZ,
    error_stage TEXT,
    error_message TEXT,
    run_metadata JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE TABLE IF NOT EXISTS ops.pipeline_stages (
    pipeline_run_id TEXT NOT NULL
        REFERENCES ops.pipeline_runs(pipeline_run_id),
    stage_name TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED')),
    records_read BIGINT NOT NULL DEFAULT 0,
    records_accepted BIGINT NOT NULL DEFAULT 0,
    records_rejected BIGINT NOT NULL DEFAULT 0,
    started_at_utc TIMESTAMPTZ,
    finished_at_utc TIMESTAMPTZ,
    error_message TEXT,
    stage_metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (pipeline_run_id, stage_name)
);

CREATE TABLE IF NOT EXISTS ops.ingested_files (
    ingestion_id BIGSERIAL PRIMARY KEY,
    pipeline_run_id TEXT NOT NULL
        REFERENCES ops.pipeline_runs(pipeline_run_id),
    source_file TEXT NOT NULL,
    source_format TEXT NOT NULL
        CHECK (source_format IN ('json', 'csv')),
    sha256_checksum TEXT NOT NULL,
    expected_record_count BIGINT,
    actual_record_count BIGINT NOT NULL,
    ingestion_status TEXT NOT NULL
        CHECK (ingestion_status IN ('SUCCESS', 'FAILED', 'SKIPPED')),
    ingested_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_message TEXT,
    UNIQUE (pipeline_run_id, source_file)
);

CREATE TABLE IF NOT EXISTS ops.quality (
    quality_id BIGSERIAL PRIMARY KEY,
    pipeline_run_id TEXT NOT NULL
        REFERENCES ops.pipeline_runs(pipeline_run_id),
    layer_name TEXT NOT NULL
        CHECK (layer_name IN ('raw', 'bronze', 'silver', 'gold')),
    check_name TEXT NOT NULL,
    check_status TEXT NOT NULL
        CHECK (check_status IN ('PASS', 'FAIL', 'WARN')),
    actual_value TEXT,
    expected_value TEXT,
    failure_count BIGINT NOT NULL DEFAULT 0,
    checked_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details JSONB NOT NULL DEFAULT '{}'::JSONB,
    UNIQUE (pipeline_run_id, layer_name, check_name)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stages_status
    ON ops.pipeline_stages(status);

CREATE INDEX IF NOT EXISTS idx_ingested_files_checksum
    ON ops.ingested_files(sha256_checksum);

CREATE INDEX IF NOT EXISTS idx_quality_status
    ON ops.quality(check_status);
