from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import pg8000.dbapi

SILVER_TABLES = (
    "city_reference",
    "customers",
    "customer_profiles",
    "customer_addresses",
    "products",
    "product_categories",
    "stores",
    "sales_channels",
    "promotions",
    "orders",
    "order_items",
    "order_promotions",
    "payment_events",
    "refund_events",
    "return_events",
    "support_events",
    "web_events",
    "inventory_snapshots",
    "campaign_spend",
)


def connect_local():
    dsn = os.getenv(
        "LOCAL_DATABASE_URL",
        "postgresql://retail:retail@localhost:5435/retail_analytics",
    )
    parsed = urlparse(dsn)

    return pg8000.dbapi.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5435,
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
                f"Failed SQL statement #{statement_number}: "
                f"{statement[:180]}"
            ) from exc


def upsert_stage(
    cursor,
    run_id: str,
    status: str,
    records_read: int = 0,
    records_accepted: int = 0,
    records_rejected: int = 0,
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
            'build_silver',
            %s,
            %s,
            %s,
            %s,
            NOW(),
            CASE
                WHEN %s IN ('SUCCESS', 'FAILED')
                THEN NOW()
            END,
            %s
        )
        ON CONFLICT (pipeline_run_id, stage_name)
        DO UPDATE SET
            status = EXCLUDED.status,
            records_read = EXCLUDED.records_read,
            records_accepted = EXCLUDED.records_accepted,
            records_rejected = EXCLUDED.records_rejected,
            finished_at_utc = EXCLUDED.finished_at_utc,
            error_message = EXCLUDED.error_message
        """,
        (
            run_id,
            status,
            records_read,
            records_accepted,
            records_rejected,
            status,
            error_message,
        ),
    )


def run(project_root: Path, run_id: str) -> dict:
    sql_path = project_root / "pipelines/silver/build_silver.sql"
    raw_root = project_root / "data/raw"

    if not sql_path.exists():
        raise FileNotFoundError(sql_path)

    connection = connect_local()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO ops.pipeline_runs (
                pipeline_run_id,
                status,
                raw_root,
                run_metadata
            )
            VALUES (
                %s,
                'RUNNING',
                %s,
                CAST(%s AS JSONB)
            )
            ON CONFLICT (pipeline_run_id)
            DO UPDATE SET
                status = 'RUNNING',
                finished_at_utc = NULL,
                error_stage = NULL,
                error_message = NULL
            """,
            (
                run_id,
                str(raw_root.resolve()),
                json.dumps({"execution": "full_silver_rebuild"}),
            ),
        )

        upsert_stage(cursor, run_id, "RUNNING")
        connection.commit()

        cursor.execute(
            "SELECT set_config('app.pipeline_run_id', %s, false)",
            (run_id,),
        )

        execute_sql_file(cursor, sql_path)

        cursor.execute("SELECT COUNT(*) FROM bronze.raw_records")
        bronze_count = cursor.fetchone()[0]

        table_counts: dict[str, int] = {}
        accepted_count = 0

        for table_name in SILVER_TABLES:
            cursor.execute(
                f"SELECT COUNT(*) FROM silver.{table_name}"
            )
            count = cursor.fetchone()[0]
            table_counts[table_name] = count
            accepted_count += count

        cursor.execute("SELECT COUNT(*) FROM silver.rejected_records")
        rejected_count = cursor.fetchone()[0]

        upsert_stage(
            cursor,
            run_id,
            "SUCCESS",
            records_read=bronze_count,
            records_accepted=accepted_count,
            records_rejected=rejected_count,
        )
        connection.commit()

        result = {
            "pipeline_run_id": run_id,
            "bronze_records": bronze_count,
            "silver_rows": accepted_count,
            "rejected_rows": rejected_count,
            "table_counts": table_counts,
        }
        return result

    except Exception as exc:
        connection.rollback()

        failure_cursor = connection.cursor()
        failure_cursor.execute(
            """
            UPDATE ops.pipeline_runs
            SET status = 'FAILED',
                finished_at_utc = NOW(),
                error_stage = 'build_silver',
                error_message = %s
            WHERE pipeline_run_id = %s
            """,
            (str(exc), run_id),
        )

        upsert_stage(
            failure_cursor,
            run_id,
            "FAILED",
            error_message=str(exc),
        )
        connection.commit()
        raise

    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument("--pipeline-run-id")
    args = parser.parse_args()

    run_id = args.pipeline_run_id or (
        "silver-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )

    result = run(args.project_root.resolve(), run_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
