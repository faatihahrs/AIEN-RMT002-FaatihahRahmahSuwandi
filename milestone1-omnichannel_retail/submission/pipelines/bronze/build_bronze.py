from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import pg8000.dbapi

ID_COLUMNS = (
    "source_row_id",
    "event_id",
    "order_item_id",
    "order_id",
    "customer_id",
    "product_id",
    "promotion_id",
    "store_id",
    "channel_id",
    "address_id",
    "city_id",
    "campaign_id",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def load_manifest(raw_root: Path) -> dict:
    manifest_path = raw_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest.json must contain a non-empty 'files' object")

    if manifest.get("file_count") != len(files):
        raise ValueError(
            "Manifest file_count does not match the number of file entries"
        )

    return manifest


def validate_source_files(raw_root: Path, manifest: dict) -> None:
    errors: list[str] = []

    for relative_name, expected_checksum in manifest["files"].items():
        path = raw_root / relative_name

        if not path.is_file():
            errors.append(f"Missing file: {relative_name}")
            continue

        actual_checksum = sha256_bytes(path.read_bytes())
        if actual_checksum != expected_checksum:
            errors.append(
                f"Checksum mismatch: {relative_name}; "
                f"expected={expected_checksum}; actual={actual_checksum}"
            )

    if errors:
        raise ValueError("\n".join(errors))


def iter_records(path: Path):
    suffix = path.suffix.lower()

    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, list):
            raise ValueError(f"Expected a JSON array in {path}")

        for number, row in enumerate(value, start=1):
            if not isinstance(row, dict):
                raise ValueError(
                    f"Expected an object at record {number} in {path}"
                )
            yield number, row
        return

    if suffix == ".csv":
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            for number, row in enumerate(reader, start=1):
                yield number, dict(row)
        return

    raise ValueError(f"Unsupported source format: {path}")


def source_record_id(row: dict) -> str | None:
    for column in ID_COLUMNS:
        value = row.get(column)
        if value not in (None, ""):
            return str(value)
    return None


def upsert_stage(cursor, run_id: str, status: str, **counts) -> None:
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
            finished_at_utc
        )
        VALUES (
            %s,
            'load_bronze',
            %s,
            %s,
            %s,
            %s,
            NOW(),
            CASE WHEN %s IN ('SUCCESS', 'FAILED') THEN NOW() END
        )
        ON CONFLICT (pipeline_run_id, stage_name)
        DO UPDATE SET
            status = EXCLUDED.status,
            records_read = EXCLUDED.records_read,
            records_accepted = EXCLUDED.records_accepted,
            records_rejected = EXCLUDED.records_rejected,
            finished_at_utc = EXCLUDED.finished_at_utc
        """,
        (
            run_id,
            status,
            counts.get("read", 0),
            counts.get("accepted", 0),
            counts.get("rejected", 0),
            status,
        ),
    )


def load_file(
    cursor,
    raw_root: Path,
    relative_name: str,
    file_checksum: str,
    run_id: str,
) -> tuple[int, int]:
    path = raw_root / relative_name
    source_format = path.suffix.lower().lstrip(".")

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM bronze.raw_records
        WHERE source_file = %s
          AND source_file_checksum = %s
        """,
        (relative_name, file_checksum),
    )
    count_before = cursor.fetchone()[0]

    record_count = 0
    batch: list[tuple] = []

    insert_sql = """
        INSERT INTO bronze.raw_records (
            source_file,
            source_format,
            source_file_checksum,
            source_record_number,
            source_record_id,
            raw_record_text,
            raw_payload,
            record_checksum,
            first_ingestion_run_id
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            CAST(%s AS JSONB), %s, %s
        )
        ON CONFLICT (
            source_file,
            source_file_checksum,
            source_record_number
        )
        DO NOTHING
    """

    for number, row in iter_records(path):
        raw_text = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        record_checksum = sha256_bytes(raw_text.encode("utf-8"))

        batch.append(
            (
                relative_name,
                source_format,
                file_checksum,
                number,
                source_record_id(row),
                raw_text,
                raw_text,
                record_checksum,
                run_id,
            )
        )
        record_count += 1

        if len(batch) >= 1000:
            cursor.executemany(insert_sql, batch)
            batch.clear()

    if batch:
        cursor.executemany(insert_sql, batch)

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM bronze.raw_records
        WHERE source_file = %s
          AND source_file_checksum = %s
        """,
        (relative_name, file_checksum),
    )
    count_after = cursor.fetchone()[0]

    inserted_count = count_after - count_before
    return record_count, inserted_count


def run(raw_root: Path, run_id: str) -> dict:
    raw_root = raw_root.resolve()
    manifest = load_manifest(raw_root)
    validate_source_files(raw_root, manifest)

    connection = connect_local()
    total_read = 0
    total_inserted = 0

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
            VALUES (%s, 'RUNNING', %s, CAST(%s AS JSONB))
            ON CONFLICT (pipeline_run_id)
            DO UPDATE SET
                status = 'RUNNING',
                error_stage = NULL,
                error_message = NULL
            """,
            (
                run_id,
                str(raw_root),
                json.dumps(
                    {
                        "manifest_version": manifest.get("manifest_version"),
                        "start_date": manifest.get("start_date"),
                        "end_date": manifest.get("end_date"),
                    }
                ),
            ),
        )
        upsert_stage(cursor, run_id, "RUNNING")
        connection.commit()

        for relative_name, checksum in manifest["files"].items():
            record_count, inserted_count = load_file(
                cursor,
                raw_root,
                relative_name,
                checksum,
                run_id,
            )

            total_read += record_count
            total_inserted += inserted_count

            cursor.execute(
                """
                INSERT INTO ops.ingested_files (
                    pipeline_run_id,
                    source_file,
                    source_format,
                    sha256_checksum,
                    expected_record_count,
                    actual_record_count,
                    ingestion_status
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'SUCCESS')
                ON CONFLICT (pipeline_run_id, source_file)
                DO UPDATE SET
                    source_format = EXCLUDED.source_format,
                    sha256_checksum = EXCLUDED.sha256_checksum,
                    expected_record_count =
                        EXCLUDED.expected_record_count,
                    actual_record_count = EXCLUDED.actual_record_count,
                    ingestion_status = 'SUCCESS',
                    error_message = NULL
                """,
                (
                    run_id,
                    relative_name,
                    Path(relative_name).suffix.lstrip("."),
                    checksum,
                    record_count,
                    record_count,
                ),
            )
            connection.commit()

            print(
                f"{relative_name}: "
                f"read={record_count}, inserted={inserted_count}"
            )

        upsert_stage(
            cursor,
            run_id,
            "SUCCESS",
            read=total_read,
            accepted=total_inserted,
            rejected=0,
        )
        connection.commit()

        return {
            "pipeline_run_id": run_id,
            "files": len(manifest["files"]),
            "records_read": total_read,
            "records_inserted": total_inserted,
        }

    except Exception as exc:
        connection.rollback()
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE ops.pipeline_runs
            SET status = 'FAILED',
                finished_at_utc = NOW(),
                error_stage = 'load_bronze',
                error_message = %s
            WHERE pipeline_run_id = %s
            """,
            (str(exc), run_id),
        )
        connection.commit()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/raw"),
    )
    parser.add_argument("--pipeline-run-id")
    args = parser.parse_args()

    run_id = args.pipeline_run_id or (
        "manual-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )

    result = run(args.raw_root, run_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
