from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.repository import ConflictError, MemoryRepository
from src.cache import NullCache
from src.config import Settings
from src.contracts import ActionProposal, AuthContext
from src.domain import DomainError
from src.providers import DeterministicProvider
from src.retrieval import RetrievalService
from src.tools import ToolExecutor


def proposal() -> ActionProposal:
    return ActionProposal(
        proposal_id="AP-TEST",
        leave_type="annual",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3),
        business_days=2,
        validation_summary=["valid"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def test_confirmation_is_pending_only_and_idempotent(repository: MemoryRepository) -> None:
    repository.save_proposal("EMP-001", proposal())
    first = repository.confirm_proposal("EMP-001", "AP-TEST", "idem-12345678")
    replay = repository.confirm_proposal("EMP-001", "AP-TEST", "idem-12345678")
    assert first.status == "pending_manager_review"
    assert replay.request_id == first.request_id
    assert replay.idempotent_replay is True
    assert repository.list_leave_requests("EMP-001")[0].status == "pending_manager_review"


def test_confirmation_rejects_wrong_identity_and_key(repository: MemoryRepository) -> None:
    repository.save_proposal("EMP-001", proposal())
    with pytest.raises(KeyError):
        repository.confirm_proposal("EMP-002", "AP-TEST", "idem-12345678")
    repository.confirm_proposal("EMP-001", "AP-TEST", "idem-12345678")
    with pytest.raises(ConflictError):
        repository.confirm_proposal("EMP-001", "AP-TEST", "another-key")


def test_expired_proposal_is_rejected(repository: MemoryRepository) -> None:
    item = proposal().model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    repository.save_proposal("EMP-001", item)
    with pytest.raises(ConflictError, match="expired"):
        repository.confirm_proposal("EMP-001", "AP-TEST", "idem-12345678")

def test_overlapping_leave_proposal_is_side_effect_free(
    repository: MemoryRepository,
    settings: Settings,
) -> None:
    existing_proposal = proposal().model_copy(
        update={
            "proposal_id": "AP-OVERLAP-SEED",
            "start_date": date(2026, 6, 15),
            "end_date": date(2026, 6, 16),
            "business_days": 2,
        }
    )
    repository.save_proposal(
        "EMP-001",
        existing_proposal,
    )
    repository.confirm_proposal(
        "EMP-001",
        "AP-OVERLAP-SEED",
        "idem-overlap-seed",
    )

    provider = DeterministicProvider()
    retrieval = RetrievalService(
        repository,
        provider,
        NullCache(),
        0,
    )
    executor = ToolExecutor(
        repository,
        retrieval,
        settings,
        today_factory=lambda: date(2026, 5, 20),
    )
    auth = AuthContext(
        employee_id="EMP-001",
        display_name="Alya Pratama",
        role="employee",
    )

    before_requests = [
        item.request_id
        for item in repository.list_leave_requests("EMP-001")
    ]

    with pytest.raises(
        DomainError,
        match="bertumpang tindih",
    ):
        executor.execute(
            "prepare_leave_request",
            {
                "start_date": "2026-06-15",
                "end_date": "2026-06-16",
                "leave_type": "annual",
                "reason": "Regression overlap",
            },
            auth,
        )

    after_requests = [
        item.request_id
        for item in repository.list_leave_requests("EMP-001")
    ]

    assert after_requests == before_requests
    assert len(after_requests) == 1
    assert (
        repository.list_leave_requests("EMP-001")[0].status
        == "pending_manager_review"
    )