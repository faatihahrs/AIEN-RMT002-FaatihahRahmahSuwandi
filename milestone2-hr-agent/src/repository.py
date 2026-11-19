"""Repository contracts. Implement MemoryRepository first, then PostgreSQL/pgvector."""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .contracts import (
    ActionProposal,
    ActionResult,
    AuthContext,
    LeaveBalance,
    LeaveRequestView,
    PolicyChunk,
)
from .security import token_hash


class Repository(Protocol):
    def health(self) -> bool: ...
    def authenticate(self, token: str) -> AuthContext | None: ...
    def lexical_search(
        self,
        query: str,
        limit: int,
    ) -> list[PolicyChunk]: ...
    def vector_search(
        self,
        embedding: list[float],
        limit: int,
    ) -> list[PolicyChunk]: ...
    def get_balances(
        self,
        employee_id: str,
    ) -> list[LeaveBalance]: ...
    def has_overlap(
        self,
        employee_id: str,
        start: date,
        end: date,
    ) -> bool: ...
    def save_proposal(
        self,
        employee_id: str,
        proposal: ActionProposal,
    ) -> None: ...
    def get_proposal(
        self,
        proposal_id: str,
    ) -> tuple[str, ActionProposal] | None: ...
    def confirm_proposal(
        self,
        employee_id: str,
        proposal_id: str,
        idempotency_key: str,
    ) -> ActionResult: ...
    def list_leave_requests(
        self,
        employee_id: str,
    ) -> list[LeaveRequestView]: ...
    def create_ticket(
        self,
        employee_id: str,
        category: str,
        summary: str,
    ) -> str: ...
    def save_trace(self, trace: dict[str, Any]) -> None: ...
    def get_trace(
        self,
        trace_id: str,
    ) -> dict[str, Any] | None: ...
    def monitoring_summary(self) -> dict[str, Any]:
        traces = list(self._traces.values())
        run_count = len(traces)

        error_count = sum(
            1
            for trace in traces
            if trace.get("outcome") in {"error", "abstained"}
            or trace.get("error")
        )

        latencies = sorted(
            int(trace.get("usage", {}).get("latency_ms", 0))
            for trace in traces
        )

        if latencies:
            p95_index = min(
                len(latencies) - 1,
                max(0, int(len(latencies) * 0.95)),
            )
            p95_latency_ms = latencies[p95_index]
        else:
            p95_latency_ms = 0

        tool_calls = sum(
            len(trace.get("tool_calls", []))
            for trace in traces
        )

        return {
            "run_count": run_count,
            "error_count": error_count,
            "error_rate": (
                error_count / run_count
                if run_count
                else 0.0
            ),
            "p95_latency_ms": p95_latency_ms,
            "tool_calls": tool_calls,
            "ticket_count": len(self._tickets),
            "leave_request_count": sum(
                len(items)
                for items in self._leave_requests.values()
            ),
        }


class ConflictError(RuntimeError):
    pass


class MemoryRepository:
    """Isolated in-memory repository used by deterministic tests."""

    def __init__(self) -> None:
        self.chunks: list[PolicyChunk] = []

        self._identities: dict[str, AuthContext] = {
            token_hash("token-alya"): AuthContext(
                employee_id="EMP-001",
                display_name="Alya Pratama",
                role="employee",
            ),
            token_hash("token-bima"): AuthContext(
                employee_id="EMP-002",
                display_name="Bima Santoso",
                role="employee",
            ),
            token_hash("token-admin"): AuthContext(
                employee_id="ADM-001",
                display_name="Monitoring Admin",
                role="monitoring_admin",
            ),
        }

        balance_date = date(2026, 1, 1)
        self._balances: dict[str, list[LeaveBalance]] = {
            "EMP-001": [
                LeaveBalance(
                    leave_type="annual",
                    available_days=8,
                    as_of_date=balance_date,
                ),
                LeaveBalance(
                    leave_type="sick",
                    available_days=10,
                    as_of_date=balance_date,
                ),
                LeaveBalance(
                    leave_type="parental",
                    available_days=60,
                    as_of_date=balance_date,
                ),
            ],
            "EMP-002": [
                LeaveBalance(
                    leave_type="annual",
                    available_days=3,
                    as_of_date=balance_date,
                ),
                LeaveBalance(
                    leave_type="sick",
                    available_days=10,
                    as_of_date=balance_date,
                ),
                LeaveBalance(
                    leave_type="parental",
                    available_days=60,
                    as_of_date=balance_date,
                ),
            ],
            "ADM-001": [],
        }

        self._proposals: dict[
            str,
            tuple[str, ActionProposal],
        ] = {}

        self._confirmations: dict[str, dict[str, str]] = {}
        self._leave_requests: dict[str, list[LeaveRequestView]] = {}
        self._tickets: list[dict[str, Any]] = []
        self._traces: dict[str, dict[str, Any]] = {}

    def health(self) -> bool:
        return True

    def authenticate(self, token: str) -> AuthContext | None:
        if not token:
            return None

        auth = self._identities.get(token_hash(token))
        return auth.model_copy() if auth else None

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.lower())
            if len(token) > 1
        }

    def lexical_search(
        self,
        query: str,
        limit: int,
    ) -> list[PolicyChunk]:
        query_terms = self._terms(query)
        ranked: list[tuple[float, PolicyChunk]] = []

        for chunk in self.chunks:
            searchable = " ".join(
                [
                    chunk.document_id,
                    chunk.title,
                    chunk.section,
                    chunk.content,
                ]
            )
            document_terms = self._terms(searchable)
            overlap = query_terms & document_terms

            if not overlap:
                continue

            score = len(overlap) / max(len(query_terms), 1)
            ranked.append(
                (
                    score,
                    chunk.model_copy(update={"score": score}),
                )
            )

        ranked.sort(
            key=lambda item: (
                -item[0],
                item[1].document_id,
                item[1].chunk_id,
            )
        )

        return [item[1] for item in ranked[:limit]]

    def vector_search(
        self,
        embedding: list[float],
        limit: int,
    ) -> list[PolicyChunk]:
        if not embedding:
            return []

        # Deterministic fallback for unit tests. PostgreSQL implements the
        # real pgvector similarity query.
        return [
            chunk.model_copy(update={"score": chunk.score})
            for chunk in self.chunks[:limit]
        ]

    def get_balances(
        self,
        employee_id: str,
    ) -> list[LeaveBalance]:
        return [
            balance.model_copy()
            for balance in self._balances.get(employee_id, [])
        ]

    def has_overlap(
        self,
        employee_id: str,
        start: date,
        end: date,
    ) -> bool:
        for request in self._leave_requests.get(employee_id, []):
            if request.status not in {
                "pending_manager_review",
                "approved",
            }:
                continue

            if start <= request.end_date and end >= request.start_date:
                return True

        return False

    def save_proposal(
        self,
        employee_id: str,
        proposal: ActionProposal,
    ) -> None:
        existing = self._proposals.get(proposal.proposal_id)

        if existing is not None:
            existing_owner, existing_proposal = existing

            if (
                existing_owner != employee_id
                or existing_proposal != proposal
            ):
                raise ConflictError("proposal ID already exists")

            return

        self._proposals[proposal.proposal_id] = (
            employee_id,
            proposal.model_copy(deep=True),
        )

    def get_proposal(
        self,
        proposal_id: str,
    ) -> tuple[str, ActionProposal] | None:
        record = self._proposals.get(proposal_id)

        if record is None:
            return None

        employee_id, proposal = record
        return employee_id, proposal.model_copy(deep=True)

    def confirm_proposal(
        self,
        employee_id: str,
        proposal_id: str,
        idempotency_key: str,
    ) -> ActionResult:
        record = self._proposals.get(proposal_id)

        if record is None:
            raise KeyError("proposal not found")

        owner_id, proposal = record

        # Do not reveal that another employee's proposal exists.
        if owner_id != employee_id:
            raise KeyError("proposal not found")

        previous = self._confirmations.get(proposal_id)

        if previous is not None:
            if previous["idempotency_key"] != idempotency_key:
                raise ConflictError(
                    "proposal already confirmed with another key"
                )

            return ActionResult(
                request_id=previous["request_id"],
                status="pending_manager_review",
                idempotent_replay=True,
                audit_reference=previous["audit_reference"],
            )

        now = datetime.now(timezone.utc)

        expires_at = proposal.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at <= now:
            raise ConflictError("proposal expired")

        request_id = f"LR-{uuid4().hex[:12].upper()}"
        audit_reference = f"AUD-{uuid4().hex[:12].upper()}"

        request = LeaveRequestView(
            request_id=request_id,
            leave_type=proposal.leave_type,
            start_date=proposal.start_date,
            end_date=proposal.end_date,
            business_days=proposal.business_days,
            status="pending_manager_review",
            created_at=now,
        )

        self._leave_requests.setdefault(employee_id, []).append(request)

        self._confirmations[proposal_id] = {
            "idempotency_key": idempotency_key,
            "request_id": request_id,
            "audit_reference": audit_reference,
        }

        return ActionResult(
            request_id=request_id,
            status="pending_manager_review",
            idempotent_replay=False,
            audit_reference=audit_reference,
        )

    def list_leave_requests(
        self,
        employee_id: str,
    ) -> list[LeaveRequestView]:
        requests = self._leave_requests.get(employee_id, [])

        return [
            request.model_copy(deep=True)
            for request in sorted(
                requests,
                key=lambda item: item.created_at,
                reverse=True,
            )
        ]

    def create_ticket(
        self,
        employee_id: str,
        category: str,
        summary: str,
    ) -> str:
        ticket_id = f"HRT-{uuid4().hex[:12].upper()}"

        self._tickets.append(
            {
                "ticket_id": ticket_id,
                "employee_id": employee_id,
                "category": category,
                "summary": summary,
                "status": "open",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        return ticket_id

    def save_trace(self, trace: dict[str, Any]) -> None:
        trace_id = trace.get("trace_id")

        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("trace_id is required")

        self._traces[trace_id] = copy.deepcopy(trace)

    def get_trace(
        self,
        trace_id: str,
    ) -> dict[str, Any] | None:
        trace = self._traces.get(trace_id)
        return copy.deepcopy(trace) if trace is not None else None

    def monitoring_summary(self) -> dict[str, Any]:
        traces = list(self._traces.values())
        total = len(traces)
        errors = sum(
            1
            for trace in traces
            if trace.get("outcome") in {"error", "abstained"}
            or trace.get("error")
        )

        return {
            "total_traces": total,
            "error_count": errors,
            "error_rate": errors / total if total else 0.0,
            "ticket_count": len(self._tickets),
            "leave_request_count": sum(
                len(items)
                for items in self._leave_requests.values()
            ),
        }


class PostgresRepository:
    """PostgreSQL and pgvector repository for the live application."""

    def __init__(
        self,
        database_url: str,
        embedding_dimensions: int,
        policy_index_version: str = "policies-2026-01",
    ) -> None:
        self.database_url = database_url
        self.embedding_dimensions = embedding_dimensions
        self.policy_index_version = policy_index_version

        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=5,
            open=True,
            kwargs={"row_factory": dict_row},
        )
        self.pool.wait(timeout=10)

    @staticmethod
    def _vector_literal(embedding: list[float]) -> str:
        return "[" + ",".join(
            format(float(value), ".12g")
            for value in embedding
        ) + "]"

    @staticmethod
    def _policy_chunk(row: dict[str, Any]) -> PolicyChunk:
        return PolicyChunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            title=row["title"],
            section=row["section"],
            version=row["version"],
            effective_date=row["effective_date"],
            content=row["content"],
            score=float(row.get("score", 0.0) or 0.0),
        )

    def close(self) -> None:
        self.pool.close()

    def health(self) -> bool:
        try:
            with self.pool.connection() as connection:
                row = connection.execute(
                    "SELECT 1 AS healthy"
                ).fetchone()
                return bool(row and row["healthy"] == 1)
        except psycopg.Error:
            return False

    def authenticate(
        self,
        token: str,
    ) -> AuthContext | None:
        if not token:
            return None

        digest = token_hash(token)

        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    employee.employee_id,
                    employee.display_name,
                    employee.role
                FROM auth_tokens AS auth
                JOIN employees AS employee
                    ON employee.employee_id = auth.employee_id
                WHERE auth.token_hash = %s
                """,
                (digest,),
            ).fetchone()

        if row is None:
            return None

        return AuthContext(
            employee_id=row["employee_id"],
            display_name=row["display_name"],
            role=row["role"],
        )

    def lexical_search(
        self,
        query: str,
        limit: int,
    ) -> list[PolicyChunk]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    chunk_id,
                    document_id,
                    title,
                    section,
                    version,
                    effective_date,
                    content,
                    ts_rank_cd(
                        search_vector,
                        websearch_to_tsquery('simple', %s)
                    ) AS score
                FROM policy_chunks
                WHERE index_version = %s
                  AND search_vector @@
                      websearch_to_tsquery('simple', %s)
                ORDER BY
                    score DESC,
                    effective_date DESC,
                    chunk_id
                LIMIT %s
                """,
                (
                    query,
                    self.policy_index_version,
                    query,
                    limit,
                ),
            ).fetchall()

        return [self._policy_chunk(row) for row in rows]

    def vector_search(
        self,
        embedding: list[float],
        limit: int,
    ) -> list[PolicyChunk]:
        if len(embedding) != self.embedding_dimensions:
            raise ValueError(
                "Dimensi query embedding tidak sesuai: "
                f"diharapkan {self.embedding_dimensions}, "
                f"diterima {len(embedding)}"
            )

        vector = self._vector_literal(embedding)

        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    chunk_id,
                    document_id,
                    title,
                    section,
                    version,
                    effective_date,
                    content,
                    1 - (embedding <=> %s::vector) AS score
                FROM policy_chunks
                WHERE index_version = %s
                ORDER BY
                    embedding <=> %s::vector,
                    chunk_id
                LIMIT %s
                """,
                (
                    vector,
                    self.policy_index_version,
                    vector,
                    limit,
                ),
            ).fetchall()

        return [self._policy_chunk(row) for row in rows]

    def get_balances(
        self,
        employee_id: str,
    ) -> list[LeaveBalance]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    leave_type,
                    available_days,
                    as_of_date
                FROM leave_balances
                WHERE employee_id = %s
                ORDER BY leave_type
                """,
                (employee_id,),
            ).fetchall()

        return [
            LeaveBalance(
                leave_type=row["leave_type"],
                available_days=row["available_days"],
                as_of_date=row["as_of_date"],
            )
            for row in rows
        ]

    def has_overlap(
        self,
        employee_id: str,
        start: date,
        end: date,
    ) -> bool:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM leave_requests
                    WHERE employee_id = %s
                      AND status IN (
                          'pending_manager_review',
                          'approved'
                      )
                      AND start_date <= %s
                      AND end_date >= %s
                ) AS overlaps
                """,
                (
                    employee_id,
                    end,
                    start,
                ),
            ).fetchone()

        return bool(row and row["overlaps"])

    def save_proposal(
        self,
        employee_id: str,
        proposal: ActionProposal,
    ) -> None:
        with self.pool.connection() as connection:
            inserted = connection.execute(
                """
                INSERT INTO action_proposals (
                    proposal_id,
                    employee_id,
                    action_type,
                    leave_type,
                    start_date,
                    end_date,
                    business_days,
                    reason,
                    validation_summary,
                    expires_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (proposal_id) DO NOTHING
                RETURNING proposal_id
                """,
                (
                    proposal.proposal_id,
                    employee_id,
                    proposal.action_type,
                    proposal.leave_type,
                    proposal.start_date,
                    proposal.end_date,
                    proposal.business_days,
                    proposal.reason,
                    Jsonb(proposal.validation_summary),
                    proposal.expires_at,
                ),
            ).fetchone()

            if inserted is not None:
                return

            existing = connection.execute(
                """
                SELECT
                    employee_id,
                    leave_type,
                    start_date,
                    end_date,
                    business_days,
                    reason,
                    expires_at
                FROM action_proposals
                WHERE proposal_id = %s
                """,
                (proposal.proposal_id,),
            ).fetchone()

            if existing is None:
                raise ConflictError(
                    "proposal could not be persisted"
                )

            same_proposal = (
                existing["employee_id"] == employee_id
                and existing["leave_type"] == proposal.leave_type
                and existing["start_date"] == proposal.start_date
                and existing["end_date"] == proposal.end_date
                and existing["business_days"]
                == proposal.business_days
                and existing["reason"] == proposal.reason
                and existing["expires_at"]
                == proposal.expires_at
            )

            if not same_proposal:
                raise ConflictError(
                    "proposal ID already exists"
                )

    def get_proposal(
        self,
        proposal_id: str,
    ) -> tuple[str, ActionProposal] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    employee_id,
                    proposal_id,
                    action_type,
                    leave_type,
                    start_date,
                    end_date,
                    business_days,
                    reason,
                    validation_summary,
                    expires_at
                FROM action_proposals
                WHERE proposal_id = %s
                """,
                (proposal_id,),
            ).fetchone()

        if row is None:
            return None

        proposal = ActionProposal(
            proposal_id=row["proposal_id"],
            action_type=row["action_type"],
            leave_type=row["leave_type"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            business_days=row["business_days"],
            reason=row["reason"],
            validation_summary=list(
                row["validation_summary"]
            ),
            expires_at=row["expires_at"],
        )

        return row["employee_id"], proposal

    def confirm_proposal(
        self,
        employee_id: str,
        proposal_id: str,
        idempotency_key: str,
    ) -> ActionResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT
                        employee_id,
                        leave_type,
                        start_date,
                        end_date,
                        business_days,
                        expires_at,
                        confirmed_at,
                        idempotency_key,
                        request_id
                    FROM action_proposals
                    WHERE proposal_id = %s
                    FOR UPDATE
                    """,
                    (proposal_id,),
                ).fetchone()

                if (
                    row is None
                    or row["employee_id"] != employee_id
                ):
                    raise KeyError("proposal not found")

                if row["request_id"] is not None:
                    if row["idempotency_key"] != idempotency_key:
                        raise ConflictError(
                            "proposal already confirmed "
                            "with another key"
                        )

                    return ActionResult(
                        request_id=row["request_id"],
                        status="pending_manager_review",
                        idempotent_replay=True,
                        audit_reference=(
                            f"AUD-{proposal_id}"
                        ),
                    )

                now = datetime.now(timezone.utc)

                if row["expires_at"] <= now:
                    raise ConflictError("proposal expired")

                request_id = (
                    f"LR-{uuid4().hex[:12].upper()}"
                )
                audit_reference = f"AUD-{proposal_id}"

                connection.execute(
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
                        %s, %s,
                        'pending_manager_review',
                        %s
                    )
                    """,
                    (
                        request_id,
                        employee_id,
                        row["leave_type"],
                        row["start_date"],
                        row["end_date"],
                        row["business_days"],
                        now,
                    ),
                )

                connection.execute(
                    """
                    UPDATE action_proposals
                    SET
                        confirmed_at = %s,
                        idempotency_key = %s,
                        request_id = %s
                    WHERE proposal_id = %s
                    """,
                    (
                        now,
                        idempotency_key,
                        request_id,
                        proposal_id,
                    ),
                )

                return ActionResult(
                    request_id=request_id,
                    status="pending_manager_review",
                    idempotent_replay=False,
                    audit_reference=audit_reference,
                )

    def list_leave_requests(
        self,
        employee_id: str,
    ) -> list[LeaveRequestView]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    request_id,
                    leave_type,
                    start_date,
                    end_date,
                    business_days,
                    status,
                    created_at
                FROM leave_requests
                WHERE employee_id = %s
                ORDER BY created_at DESC, request_id
                """,
                (employee_id,),
            ).fetchall()

        return [
            LeaveRequestView(
                request_id=row["request_id"],
                leave_type=row["leave_type"],
                start_date=row["start_date"],
                end_date=row["end_date"],
                business_days=row["business_days"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def create_ticket(
        self,
        employee_id: str,
        category: str,
        summary: str,
    ) -> str:
        ticket_id = f"HRT-{uuid4().hex[:12].upper()}"

        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO hr_tickets (
                    ticket_id,
                    employee_id,
                    category,
                    summary,
                    status
                )
                VALUES (%s, %s, %s, %s, 'open')
                """,
                (
                    ticket_id,
                    employee_id,
                    category,
                    summary,
                ),
            )

        return ticket_id

    def save_trace(
        self,
        trace: dict[str, Any],
    ) -> None:
        trace_id = trace.get("trace_id")
        employee_id = trace.get("employee_id")

        if not trace_id or not employee_id:
            raise ValueError(
                "trace_id and employee_id are required"
            )

        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_traces (
                    trace_id,
                    employee_id,
                    role,
                    outcome,
                    retrieved_document_ids,
                    tool_calls,
                    usage,
                    error
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (trace_id) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    retrieved_document_ids =
                        EXCLUDED.retrieved_document_ids,
                    tool_calls = EXCLUDED.tool_calls,
                    usage = EXCLUDED.usage,
                    error = EXCLUDED.error
                """,
                (
                    trace_id,
                    employee_id,
                    trace.get("role", "employee"),
                    trace.get("outcome", "unknown"),
                    Jsonb(
                        trace.get(
                            "retrieved_document_ids",
                            [],
                        )
                    ),
                    Jsonb(trace.get("tool_calls", [])),
                    Jsonb(trace.get("usage", {})),
                    trace.get("error"),
                ),
            )

    def get_trace(
        self,
        trace_id: str,
    ) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    trace_id,
                    employee_id,
                    role,
                    outcome,
                    retrieved_document_ids,
                    tool_calls,
                    usage,
                    error,
                    created_at
                FROM agent_traces
                WHERE trace_id = %s
                """,
                (trace_id,),
            ).fetchone()

        return dict(row) if row is not None else None

    def monitoring_summary(self) -> dict[str, Any]:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    count(*)::integer AS run_count,
                    count(*) FILTER (
                        WHERE outcome IN ('error', 'abstained')
                           OR error IS NOT NULL
                    )::integer AS error_count,
                    coalesce(
                        percentile_disc(0.95)
                        WITHIN GROUP (
                            ORDER BY
                                (usage ->> 'latency_ms')::integer
                        ),
                        0
                    )::integer AS p95_latency_ms,
                    coalesce(
                        sum(jsonb_array_length(tool_calls)),
                        0
                    )::integer AS tool_calls
                FROM agent_traces
                """
            ).fetchone()

            ticket_count = connection.execute(
                """
                SELECT count(*)::integer AS count
                FROM hr_tickets
                """
            ).fetchone()["count"]

            request_count = connection.execute(
                """
                SELECT count(*)::integer AS count
                FROM leave_requests
                """
            ).fetchone()["count"]

        run_count = row["run_count"]
        error_count = row["error_count"]

        return {
            "run_count": run_count,
            "error_count": error_count,
            "error_rate": (
                error_count / run_count
                if run_count
                else 0.0
            ),
            "p95_latency_ms": row["p95_latency_ms"],
            "tool_calls": row["tool_calls"],
            "ticket_count": ticket_count,
            "leave_request_count": request_count,
        }