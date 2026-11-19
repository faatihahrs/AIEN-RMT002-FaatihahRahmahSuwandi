from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .config import Settings
from .providers import OpenAIProvider
from .security import token_hash


ROOT = Path(__file__).resolve().parents[1]


def _parse_front_matter(
    raw_text: str,
    path: Path,
) -> tuple[dict[str, str], str]:
    match = re.match(
        r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
        raw_text,
        flags=re.DOTALL,
    )

    if match is None:
        raise ValueError(
            f"Policy {path.name} tidak memiliki front matter yang valid."
        )

    front_matter_text, body = match.groups()
    metadata: dict[str, str] = {}

    for raw_line in front_matter_text.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if ":" not in line:
            raise ValueError(
                f"Metadata tidak valid pada {path.name}: {raw_line!r}"
            )

        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")

    required_fields = {
        "document_id",
        "title",
        "version",
        "effective_date",
    }
    missing = required_fields - metadata.keys()

    if missing:
        raise ValueError(
            f"Metadata {path.name} belum lengkap: "
            + ", ".join(sorted(missing))
        )

    return metadata, body.strip()


def _stable_chunk_id(
    document_id: str,
    version: str,
    section: str,
    content: str,
) -> str:
    material = "\n".join(
        [
            document_id,
            version,
            section,
            content,
        ]
    )
    digest = hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:16]

    return f"{document_id}-{digest}"


def chunk_policy(path: Path) -> list[dict[str, str]]:
    """Parse one policy into stable, section-level chunks."""
    raw_text = path.read_text(encoding="utf-8-sig")
    metadata, body = _parse_front_matter(raw_text, path)

    heading_matches = list(
        re.finditer(
            r"^##\s+(.+?)\s*$",
            body,
            flags=re.MULTILINE,
        )
    )

    if not heading_matches:
        raise ValueError(
            f"Policy {path.name} tidak memiliki heading section level 2."
        )

    chunks: list[dict[str, str]] = []

    for index, heading_match in enumerate(heading_matches):
        section = heading_match.group(1).strip()
        content_start = heading_match.end()

        if index + 1 < len(heading_matches):
            content_end = heading_matches[index + 1].start()
        else:
            content_end = len(body)

        content = body[content_start:content_end].strip()

        if not content:
            continue

        chunks.append(
            {
                "chunk_id": _stable_chunk_id(
                    metadata["document_id"],
                    metadata["version"],
                    section,
                    content,
                ),
                "document_id": metadata["document_id"],
                "title": metadata["title"],
                "section": section,
                "version": metadata["version"],
                "effective_date": metadata["effective_date"],
                "content": content,
            }
        )

    if not chunks:
        raise ValueError(
            f"Policy {path.name} tidak menghasilkan chunk."
        )

    return chunks


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(
        format(float(value), ".12g")
        for value in values
    ) + "]"


def _create_schema(
    connection: psycopg.Connection,
    dimensions: int,
) -> None:
    if dimensions < 64 or dimensions > 4096:
        raise ValueError("Dimensi embedding tidak valid")

    schema_sql = f"""
    CREATE EXTENSION IF NOT EXISTS vector;

    CREATE TABLE IF NOT EXISTS employees (
        employee_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (
            role IN ('employee', 'monitoring_admin')
        ),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS auth_tokens (
        token_hash CHAR(64) PRIMARY KEY,
        employee_id TEXT NOT NULL REFERENCES employees(employee_id)
            ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS auth_tokens_employee_idx
        ON auth_tokens(employee_id);

    CREATE TABLE IF NOT EXISTS leave_balances (
        employee_id TEXT NOT NULL REFERENCES employees(employee_id)
            ON DELETE CASCADE,
        leave_type TEXT NOT NULL CHECK (
            leave_type IN ('annual', 'sick', 'parental')
        ),
        available_days INTEGER NOT NULL CHECK (available_days >= 0),
        as_of_date DATE NOT NULL,
        PRIMARY KEY (employee_id, leave_type)
    );

    CREATE TABLE IF NOT EXISTS leave_requests (
        request_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL REFERENCES employees(employee_id)
            ON DELETE CASCADE,
        leave_type TEXT NOT NULL CHECK (
            leave_type IN ('annual', 'sick', 'parental')
        ),
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        business_days INTEGER NOT NULL CHECK (business_days > 0),
        status TEXT NOT NULL CHECK (
            status IN (
                'pending_manager_review',
                'approved',
                'rejected',
                'cancelled'
            )
        ),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (end_date >= start_date)
    );

    CREATE INDEX IF NOT EXISTS leave_requests_owner_dates_idx
        ON leave_requests(employee_id, start_date, end_date);

    CREATE TABLE IF NOT EXISTS action_proposals (
        proposal_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL REFERENCES employees(employee_id)
            ON DELETE CASCADE,
        action_type TEXT NOT NULL DEFAULT 'leave_request'
            CHECK (action_type = 'leave_request'),
        leave_type TEXT NOT NULL CHECK (
            leave_type IN ('annual', 'sick', 'parental')
        ),
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        business_days INTEGER NOT NULL CHECK (business_days > 0),
        reason TEXT,
        validation_summary JSONB NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        confirmed_at TIMESTAMPTZ,
        idempotency_key TEXT,
        request_id TEXT UNIQUE REFERENCES leave_requests(request_id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (end_date >= start_date)
    );

    CREATE INDEX IF NOT EXISTS action_proposals_owner_idx
        ON action_proposals(employee_id);

    CREATE TABLE IF NOT EXISTS hr_tickets (
        ticket_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL REFERENCES employees(employee_id)
            ON DELETE CASCADE,
        category TEXT NOT NULL CHECK (
            category IN (
                'policy_question',
                'leave_exception',
                'technical_issue',
                'privacy'
            )
        ),
        summary TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'closed')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS policy_chunks (
        index_version TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        title TEXT NOT NULL,
        section TEXT NOT NULL,
        version TEXT NOT NULL,
        effective_date DATE NOT NULL,
        content TEXT NOT NULL,
        embedding VECTOR({dimensions}) NOT NULL,
        search_vector TSVECTOR GENERATED ALWAYS AS (
            to_tsvector(
                'simple',
                coalesce(document_id, '') || ' ' ||
                coalesce(title, '') || ' ' ||
                coalesce(section, '') || ' ' ||
                coalesce(content, '')
            )
        ) STORED,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (index_version, chunk_id)
    );

    CREATE INDEX IF NOT EXISTS policy_chunks_fts_idx
        ON policy_chunks USING GIN(search_vector);

    CREATE INDEX IF NOT EXISTS policy_chunks_document_idx
        ON policy_chunks(index_version, document_id);

    CREATE INDEX IF NOT EXISTS policy_chunks_vector_idx
        ON policy_chunks
        USING hnsw (embedding vector_cosine_ops);

    CREATE TABLE IF NOT EXISTS agent_traces (
        trace_id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL REFERENCES employees(employee_id)
            ON DELETE CASCADE,
        role TEXT NOT NULL,
        outcome TEXT NOT NULL,
        retrieved_document_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
        usage JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS agent_traces_employee_idx
        ON agent_traces(employee_id, created_at DESC);
    """

    with connection.cursor() as cursor:
        cursor.execute(schema_sql)


def _reset_tables(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            TRUNCATE TABLE
                agent_traces,
                hr_tickets,
                action_proposals,
                leave_requests,
                leave_balances,
                auth_tokens,
                employees,
                policy_chunks
            RESTART IDENTITY CASCADE
            """
        )


def _seed_people(
    connection: psycopg.Connection,
) -> None:
    employees = _load_json(ROOT / "data" / "employees.json")

    with connection.cursor() as cursor:
        for employee in employees:
            cursor.execute(
                """
                INSERT INTO employees (
                    employee_id,
                    display_name,
                    role
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (employee_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    role = EXCLUDED.role
                """,
                (
                    employee["employee_id"],
                    employee["display_name"],
                    employee["role"],
                ),
            )

            cursor.execute(
                """
                INSERT INTO auth_tokens (
                    token_hash,
                    employee_id
                )
                VALUES (%s, %s)
                ON CONFLICT (token_hash) DO UPDATE SET
                    employee_id = EXCLUDED.employee_id
                """,
                (
                    token_hash(employee["token"]),
                    employee["employee_id"],
                ),
            )

            for leave_type, available_days in employee[
                "balances"
            ].items():
                cursor.execute(
                    """
                    INSERT INTO leave_balances (
                        employee_id,
                        leave_type,
                        available_days,
                        as_of_date
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (employee_id, leave_type)
                    DO UPDATE SET
                        available_days = EXCLUDED.available_days,
                        as_of_date = EXCLUDED.as_of_date
                    """,
                    (
                        employee["employee_id"],
                        leave_type,
                        available_days,
                        "2026-01-01",
                    ),
                )


def _seed_historical_requests(
    connection: psycopg.Connection,
) -> None:
    rows = _load_json(
        ROOT / "data" / "historical_requests.json"
    )

    with connection.cursor() as cursor:
        for row in rows:
            cursor.execute(
                """
                INSERT INTO leave_requests (
                    request_id,
                    employee_id,
                    leave_type,
                    start_date,
                    end_date,
                    business_days,
                    status,
                    created_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (request_id) DO UPDATE SET
                    employee_id = EXCLUDED.employee_id,
                    leave_type = EXCLUDED.leave_type,
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date,
                    business_days = EXCLUDED.business_days,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at
                """,
                (
                    row["request_id"],
                    row["employee_id"],
                    row["leave_type"],
                    row["start_date"],
                    row["end_date"],
                    row["business_days"],
                    row["status"],
                    row["created_at"],
                ),
            )


def _collect_policy_chunks() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for path in sorted(
        (ROOT / "data" / "policies").glob("*.md")
    ):
        rows.extend(chunk_policy(path))

    return rows


def _policy_index_is_current(
    connection: psycopg.Connection,
    index_version: str,
    chunks: list[dict[str, str]],
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                chunk_id,
                content,
                version,
                effective_date::text
            FROM policy_chunks
            WHERE index_version = %s
            """,
            (index_version,),
        )
        existing = {
            row[0]: {
                "content": row[1],
                "version": row[2],
                "effective_date": row[3],
            }
            for row in cursor.fetchall()
        }

    expected = {
        chunk["chunk_id"]: {
            "content": chunk["content"],
            "version": chunk["version"],
            "effective_date": chunk["effective_date"],
        }
        for chunk in chunks
    }

    return existing == expected


def _seed_policy_chunks(
    connection: psycopg.Connection,
    settings: Settings,
    chunks: list[dict[str, str]],
) -> bool:
    if _policy_index_is_current(
        connection,
        settings.policy_index_version,
        chunks,
    ):
        return False

    provider = OpenAIProvider(
        api_key=settings.openai_api_key or "",
        base_url=settings.openai_base_url,
        agent_model=settings.agent_model,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.model_timeout_seconds,
    )

    embeddings = provider.embed(
        [chunk["content"] for chunk in chunks]
    )

    if len(embeddings) != len(chunks):
        raise RuntimeError(
            "Jumlah embedding tidak sesuai jumlah policy chunk."
        )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM policy_chunks
            WHERE index_version = %s
            """,
            (settings.policy_index_version,),
        )

        for chunk, embedding in zip(
            chunks,
            embeddings,
            strict=True,
        ):
            cursor.execute(
                """
                INSERT INTO policy_chunks (
                    index_version,
                    chunk_id,
                    document_id,
                    title,
                    section,
                    version,
                    effective_date,
                    content,
                    embedding
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::vector
                )
                """,
                (
                    settings.policy_index_version,
                    chunk["chunk_id"],
                    chunk["document_id"],
                    chunk["title"],
                    chunk["section"],
                    chunk["version"],
                    chunk["effective_date"],
                    chunk["content"],
                    _vector_literal(embedding),
                ),
            )

    return True


def reset_and_seed(
    *,
    reset: bool = False,
) -> None:
    settings = Settings()

    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY diperlukan untuk membuat policy embeddings."
        )

    if not settings.embedding_model:
        raise RuntimeError(
            "EMBEDDING_MODEL belum dikonfigurasi."
        )

    chunks = _collect_policy_chunks()

    with psycopg.connect(
        settings.database_url,
        autocommit=False,
    ) as connection:
        _create_schema(
            connection,
            settings.embedding_dimensions,
        )
        connection.commit()

        if reset:
            _reset_tables(connection)
            connection.commit()

        _seed_people(connection)
        _seed_historical_requests(connection)
        connection.commit()

        index_updated = _seed_policy_chunks(
            connection,
            settings,
            chunks,
        )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM employees")
            employee_count = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT count(*)
                FROM leave_requests
                """
            )
            request_count = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT count(*)
                FROM policy_chunks
                WHERE index_version = %s
                """,
                (settings.policy_index_version,),
            )
            chunk_count = cursor.fetchone()[0]

    print("Database:", settings.database_url)
    print("Employees:", employee_count)
    print("Historical requests:", request_count)
    print("Policy chunks:", chunk_count)
    print(
        "Policy embeddings:",
        "updated" if index_updated else "already current",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and seed the HR assistant database."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear synthetic lab tables before seeding.",
    )
    arguments = parser.parse_args()

    reset_and_seed(reset=arguments.reset)


if __name__ == "__main__":
    main()