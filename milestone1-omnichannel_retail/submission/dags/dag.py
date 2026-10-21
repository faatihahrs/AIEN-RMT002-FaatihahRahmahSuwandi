from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import pg8000.dbapi
from airflow import DAG
from airflow.operators.python import PythonOperator

PROJECT_ROOT = Path(
    os.getenv("PROJECT_ROOT", "/opt/airflow/project")
).resolve()

RAW_ROOT = PROJECT_ROOT / "data/raw"

SCHEMA_FILES = (
    "ops.sql",
    "bronze.sql",
    "silver.sql",
    "gold.sql",
)


def connect_local():
    dsn = os.getenv(
        "LOCAL_DATABASE_URL",
        "postgresql://retail:retail@postgres:5432/retail_analytics",
    )
    parsed = urlparse(dsn)

    return pg8000.dbapi.connect(
        host=parsed.hostname or "postgres",
        port=parsed.port or 5432,
        database=(parsed.path or "/retail_analytics").lstrip("/"),
        user=unquote(parsed.username or "retail"),
        password=unquote(parsed.password or "retail"),
    )


def execute_sql_file(cursor, path: Path) -> None:
    sql_text = path.read_text(encoding="utf-8-sig")

    for statement_number, statement in enumerate(
        sql_text.split(";"),
        start=1,
    ):
        statement = statement.strip()
        if not statement:
            continue

        try:
            cursor.execute(statement)
        except Exception as exc:
            raise RuntimeError(
                f"Failed {path.name} statement "
                f"#{statement_number}: {statement[:180]}"
            ) from exc


def upsert_stage(
    cursor,
    pipeline_run_id: str,
    stage_name: str,
    status: str,
    error_message: str | None = None,
) -> None:
    cursor.execute(
        """
        INSERT INTO ops.pipeline_stages (
            pipeline_run_id,
            stage_name,
            status,
            records_read,
            records_accepted,
            records_rejected,
            started_at_utc,
            finished_at_utc,
            error_message
        )
        VALUES (
            %s,
            %s,
            %s,
            0,
            0,
            0,
            NOW(),
            CASE
                WHEN %s IN ('SUCCESS', 'FAILED', 'SKIPPED')
                THEN NOW()
            END,
            %s
        )
        ON CONFLICT (pipeline_run_id, stage_name)
        DO UPDATE SET
            status = EXCLUDED.status,
            finished_at_utc = EXCLUDED.finished_at_utc,
            error_message = EXCLUDED.error_message
        """,
        (
            pipeline_run_id,
            stage_name,
            status,
            status,
            error_message,
        ),
    )


def validate_raw(pipeline_run_id: str) -> dict:
    from pipelines.bronze.build_bronze import (
        load_manifest,
        validate_source_files,
    )

    manifest = load_manifest(RAW_ROOT)
    validate_source_files(RAW_ROOT, manifest)

    return {
        "pipeline_run_id": pipeline_run_id,
        "validated_files": len(manifest["files"]),
    }


def initialize_schemas(pipeline_run_id: str) -> dict:
    schema_root = PROJECT_ROOT / "database/schemas"
    connection = connect_local()

    try:
        cursor = connection.cursor()

        for schema_file in SCHEMA_FILES:
            execute_sql_file(cursor, schema_root / schema_file)

            if schema_file == "ops.sql":
                cursor.execute(
                    """
                    INSERT INTO ops.pipeline_runs (
                        pipeline_run_id,
                        dag_run_id,
                        status,
                        raw_root,
                        run_metadata
                    )
                    VALUES (
                        %s,
                        %s,
                        'RUNNING',
                        %s,
                        jsonb_build_object(
                            'orchestrator',
                            'airflow'
                        )
                    )
                    ON CONFLICT (pipeline_run_id)
                    DO UPDATE SET
                        dag_run_id = EXCLUDED.dag_run_id,
                        status = 'RUNNING',
                        finished_at_utc = NULL,
                        error_stage = NULL,
                        error_message = NULL,
                        run_metadata = EXCLUDED.run_metadata
                    """,
                    (
                        pipeline_run_id,
                        pipeline_run_id,
                        str(RAW_ROOT),
                    ),
                )

                upsert_stage(
                    cursor,
                    pipeline_run_id,
                    "validate_raw",
                    "SUCCESS",
                )

        upsert_stage(
            cursor,
            pipeline_run_id,
            "initialize_schemas",
            "SUCCESS",
        )
        connection.commit()

        return {
            "pipeline_run_id": pipeline_run_id,
            "schemas": list(SCHEMA_FILES),
        }

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_bronze(pipeline_run_id: str) -> dict:
    from pipelines.bronze.build_bronze import run

    return run(RAW_ROOT, pipeline_run_id)


def build_silver(pipeline_run_id: str) -> dict:
    from pipelines.silver.build_silver import run

    return run(PROJECT_ROOT, pipeline_run_id)


def quality_gate(pipeline_run_id: str) -> dict:
    from pipelines.quality.check_silver import run

    return run(pipeline_run_id)


def build_gold(pipeline_run_id: str) -> dict:
    from pipelines.gold.build_gold import run

    return run(PROJECT_ROOT, pipeline_run_id)


def validate_gold_and_record_metadata(
    pipeline_run_id: str,
) -> dict:
    from pipelines.quality.validate_gold import run

    return run(pipeline_run_id)


def pipeline_failure_callback(context) -> None:
    task_instance = context.get("task_instance")
    dag_run = context.get("dag_run")
    exception = context.get("exception")

    pipeline_run_id = (
        dag_run.run_id
        if dag_run is not None
        else "unknown-airflow-run"
    )
    stage_name = (
        task_instance.task_id
        if task_instance is not None
        else "unknown_stage"
    )
    error_message = str(exception or "Unknown Airflow task failure")

    connection = connect_local()

    try:
        cursor = connection.cursor()

        execute_sql_file(
            cursor,
            PROJECT_ROOT / "database/schemas/ops.sql",
        )

        cursor.execute(
            """
            INSERT INTO ops.pipeline_runs (
                pipeline_run_id,
                dag_run_id,
                status,
                raw_root,
                finished_at_utc,
                error_stage,
                error_message
            )
            VALUES (
                %s,
                %s,
                'FAILED',
                %s,
                NOW(),
                %s,
                %s
            )
            ON CONFLICT (pipeline_run_id)
            DO UPDATE SET
                status = 'FAILED',
                finished_at_utc = NOW(),
                error_stage = EXCLUDED.error_stage,
                error_message = EXCLUDED.error_message
            """,
            (
                pipeline_run_id,
                pipeline_run_id,
                str(RAW_ROOT),
                stage_name,
                error_message,
            ),
        )

        upsert_stage(
            cursor,
            pipeline_run_id,
            stage_name,
            "FAILED",
            error_message,
        )
        connection.commit()
    except Exception:
        connection.rollback()
    finally:
        connection.close()


default_args = {
    "owner": "data-engineering",
    "retries": 0,
    "on_failure_callback": pipeline_failure_callback,
}


with DAG(
    dag_id="omnichannel_retail_pipeline",
    description="Bronze, Silver, quality, and Gold retail pipeline",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["milestone-1", "retail", "data-pipeline"],
) as dag:
    pipeline_run_id = "{{ dag_run.run_id }}"

    validate_raw_task = PythonOperator(
        task_id="validate_raw",
        python_callable=validate_raw,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    initialize_schemas_task = PythonOperator(
        task_id="initialize_schemas",
        python_callable=initialize_schemas,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    load_bronze_task = PythonOperator(
        task_id="load_bronze",
        python_callable=load_bronze,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    build_silver_task = PythonOperator(
        task_id="build_silver",
        python_callable=build_silver,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    quality_gate_task = PythonOperator(
        task_id="quality_gate",
        python_callable=quality_gate,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    build_gold_task = PythonOperator(
        task_id="build_gold",
        python_callable=build_gold,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    validate_gold_task = PythonOperator(
        task_id="validate_gold_and_record_metadata",
        python_callable=validate_gold_and_record_metadata,
        op_kwargs={"pipeline_run_id": pipeline_run_id},
    )

    (
        validate_raw_task
        >> initialize_schemas_task
        >> load_bronze_task
        >> build_silver_task
        >> quality_gate_task
        >> build_gold_task
        >> validate_gold_task
    )
