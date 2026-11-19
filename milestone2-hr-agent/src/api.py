from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Security,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from .agent import AgentService
from .cache import NullCache, ResilientCache
from .config import Settings, get_settings
from .contracts import (
    ActionResult,
    AuthContext,
    ChatRequest,
    ChatResponse,
    ConfirmActionRequest,
    LeaveRequestView,
    ProblemDetail,
)
from .providers import DeterministicProvider, OpenAIProvider
from .repository import (
    ConflictError,
    MemoryRepository,
    PostgresRepository,
)
from .retrieval import RetrievalService
from .tools import ToolExecutor


ROOT = Path(__file__).resolve().parents[1]
bearer_scheme = HTTPBearer(auto_error=False)


def build_runtime(settings: Settings):
    """Build either the deterministic test runtime or live runtime."""
    if settings.model_mode == "fake":
        repository = MemoryRepository()
        cache = NullCache()
        provider = DeterministicProvider()

    elif settings.model_mode == "live":
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY diperlukan ketika MODEL_MODE=live."
            )

        repository = PostgresRepository(
            settings.database_url,
            settings.embedding_dimensions,
            settings.policy_index_version,
        )
        cache = ResilientCache(settings.redis_url)
        provider = OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            agent_model=settings.agent_model,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.model_timeout_seconds,
        )

    else:
        raise RuntimeError(
            f"MODEL_MODE tidak didukung: {settings.model_mode!r}. "
            "Gunakan 'fake' atau 'live'."
        )

    retrieval = RetrievalService(
        repository,
        provider,
        cache,
        settings.retrieval_cache_ttl_seconds,
    )
    tools = ToolExecutor(
        repository,
        retrieval,
        settings,
    )
    agent = AgentService(
        repository,
        provider,
        tools,
        settings,
    )
    return repository, cache, agent

def _problem_response(
    *,
    status_code: int,
    title: str,
    detail: str,
    trace_id: str | None = None,
) -> JSONResponse:
    body = ProblemDetail(
        title=title,
        status=status_code,
        detail=detail,
        trace_id=trace_id,
    )

    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )


def create_app(
    *,
    settings: Settings | None = None,
    repository=None,
    cache=None,
    agent=None,
) -> FastAPI:
    """Build the documented API without changing endpoint contracts."""
    active_settings = settings or get_settings()

    if repository is None or cache is None or agent is None:
        repository, cache, agent = build_runtime(active_settings)

    app = FastAPI(
        title=active_settings.app_name,
        version="1.0.0",
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        del request
        detail = (
            exc.detail
            if isinstance(exc.detail, str)
            else "Permintaan tidak dapat diproses."
        )
        return _problem_response(
            status_code=exc.status_code,
            title="HTTP Error",
            detail=detail,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request, exc
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Validation Error",
            detail="Request tidak valid.",
        )

    def authenticate(
        credentials: HTTPAuthorizationCredentials | None = Security(
            bearer_scheme
        ),
    ) -> AuthContext:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token diperlukan.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        auth = repository.authenticate(credentials.credentials)

        if auth is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token tidak valid.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return auth

    def require_monitoring_admin(
        auth: AuthContext = Depends(authenticate),
    ) -> AuthContext:
        if auth.role != "monitoring_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akses monitoring memerlukan role admin.",
            )
        return auth

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        repository_ready = bool(repository.health())
        cache_ready = bool(cache.health())
        model_ready = (
            active_settings.model_mode == "fake"
            or active_settings.live_model_ready
        )

        ready = repository_ready and model_ready

        return {
            "status": "ready" if ready else "not_ready",
            "database": (
                "ready" if repository_ready else "unavailable"
            ),
            "cache": (
                "ready" if cache_ready else "degraded"
            ),
            "model": (
                "ready" if model_ready else "not_configured"
            ),
        }

    @app.post(
        "/api/v1/chat",
        response_model=ChatResponse,
    )
    def chat(
        payload: ChatRequest,
        auth: AuthContext = Depends(authenticate),
    ) -> ChatResponse:
        return agent.run(payload, auth)

    @app.post(
        "/api/v1/action-proposals/{proposal_id}/confirm",
        response_model=ActionResult,
    )
    def confirm_action(
        proposal_id: str,
        payload: ConfirmActionRequest,
        auth: AuthContext = Depends(authenticate),
    ) -> ActionResult:
        try:
            return repository.confirm_proposal(
                auth.employee_id,
                proposal_id,
                payload.idempotency_key,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Proposal tidak ditemukan.",
            ) from exc
        except ConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.get(
        "/api/v1/leave-requests/me",
        response_model=list[LeaveRequestView],
    )
    def my_leave_requests(
        auth: AuthContext = Depends(authenticate),
    ) -> list[LeaveRequestView]:
        return repository.list_leave_requests(
            auth.employee_id
        )

    @app.get("/api/v1/traces/{trace_id}")
    def get_trace(
        trace_id: str,
        auth: AuthContext = Depends(authenticate),
    ) -> dict[str, Any]:
        trace = repository.get_trace(trace_id)

        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trace tidak ditemukan.",
            )

        if (
            auth.role != "monitoring_admin"
            and trace.get("employee_id") != auth.employee_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Anda tidak boleh mengakses trace ini.",
            )

        return trace

    @app.get("/api/v1/monitoring/summary")
    def monitoring_summary(
        auth: AuthContext = Depends(require_monitoring_admin),
    ) -> dict[str, Any]:
        del auth
        return repository.monitoring_summary()

    @app.get("/api/v1/monitoring/alerts")
    def monitoring_alerts(
        auth: AuthContext = Depends(require_monitoring_admin),
    ) -> list[dict[str, Any]]:
        del auth

        summary = repository.monitoring_summary()
        rules_path = ROOT / "data" / "monitoring_rules.json"

        rules = json.loads(
            rules_path.read_text(encoding="utf-8")
        )

        alerts: list[dict[str, Any]] = []

        for rule in rules:
            metric = rule["metric"]
            value = summary.get(metric, 0)
            threshold = rule["threshold"]
            minimum_samples = rule["minimum_samples"]

            enough_samples = (
                summary.get("run_count", 0) >= minimum_samples
            )

            triggered = False

            if enough_samples and rule["operator"] == "gt":
                triggered = value > threshold

            if enough_samples and rule["operator"] == "gte":
                triggered = value >= threshold

            if triggered:
                alerts.append(
                    {
                        **rule,
                        "current_value": value,
                        "status": "active",
                    }
                )

        return alerts

    return app