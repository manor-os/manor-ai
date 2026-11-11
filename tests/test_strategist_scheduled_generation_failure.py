"""Terminal Proposal errors settle once; transport/credit retry control is unchanged."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.ai.llm_client import CreditExhaustedError, LLMStreamProviderError
from packages.core.services import workflow_run_execution_claim as claims
from packages.core.strategist.prompt import StrategistProposalGenerationError
from packages.core.tasks import ai_tasks


class RetryRequested(Exception):
    pass


@pytest.fixture
def scheduled_boundary(monkeypatch):
    state = {"settled": False}

    @asynccontextmanager
    async def execution_claim(run_id):
        yield claims._claim(run_id, "worker", granted=True, reason=claims.CLAIM_GRANTED)

    async def admit(*_args, **_kwargs):
        return SimpleNamespace(admitted=not state["settled"], reason="already_settled" if state["settled"] else "admitted")

    async def finalize(**_kwargs):
        state["settled"] = True

    finalizer = AsyncMock(side_effect=finalize)
    notification = AsyncMock()
    task = SimpleNamespace(
        request=SimpleNamespace(retries=0), max_retries=2,
        retry=Mock(side_effect=RetryRequested("retry must be explicitly allowed")),
    )
    monkeypatch.setattr(ai_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(claims, "scheduled_run_execution_claim", execution_claim)
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admit)
    monkeypatch.setattr(ai_tasks, "_finalize_scheduled_run", finalizer)

    def execute(body):
        return ai_tasks._run_scheduled(
            task, "Strategist review", "workspace", body,
            run_id="run", job_id_str="job", on_terminal_error=notification,
        )

    return SimpleNamespace(task=task, state=state, finalizer=finalizer, notification=notification, execute=execute)


@pytest.mark.parametrize("code", ["proposal_invalid_output", "proposal_llm_unavailable"])
def test_generation_failure_settles_first_attempt_and_duplicate_delivery_does_not_notify_again(scheduled_boundary, code):
    boundary = scheduled_boundary
    error = StrategistProposalGenerationError(
        "A valid Proposal was not generated", code=code,
        details={"attempts": 2, "validation_errors": [{"path": ["tasks", 0, "deliverables"], "type": "missing"}]},
    )
    body = AsyncMock(side_effect=error)
    result = boundary.execute(body)

    assert result == {"status": "failed", "error": f"{code}: {error}", "code": code, "details": error.details}
    body.assert_awaited_once()
    boundary.task.retry.assert_not_called()
    boundary.finalizer.assert_awaited_once_with(
        run_id="run", job_id_str="job", result=result, error=result["error"],
    )
    boundary.notification.assert_awaited_once_with(result["error"])

    duplicate = boundary.execute(body)
    assert duplicate["status"] == "already_settled"
    assert duplicate["duplicate_suppressed"] is True
    body.assert_awaited_once()
    boundary.finalizer.assert_awaited_once()
    boundary.notification.assert_awaited_once()
    boundary.task.retry.assert_not_called()


def test_terminal_notification_failure_does_not_replay_generation(scheduled_boundary):
    boundary = scheduled_boundary
    boundary.notification.side_effect = RuntimeError("notification unavailable")
    body = AsyncMock(side_effect=StrategistProposalGenerationError("Invalid Proposal", code="proposal_invalid_output"))
    result = boundary.execute(body)
    assert result["status"] == "failed"
    body.assert_awaited_once()
    boundary.finalizer.assert_awaited_once()
    boundary.notification.assert_awaited_once()
    boundary.task.retry.assert_not_called()


def test_generation_failure_settlement_error_uses_existing_settlement_only_recovery(scheduled_boundary, monkeypatch):
    boundary = scheduled_boundary
    boundary.finalizer.side_effect = RuntimeError("database temporarily unavailable")
    deferred = AsyncMock()
    monkeypatch.setattr(ai_tasks, "_defer_scheduled_run_settlement", deferred)
    body = AsyncMock(side_effect=StrategistProposalGenerationError("Invalid Proposal", code="proposal_invalid_output"))
    result = boundary.execute(body)
    assert result["status"] == "failed"
    deferred.assert_awaited_once_with(run_id="run", job_id_str="job", result=result, error=result["error"])
    boundary.task.retry.assert_not_called()
    body.assert_awaited_once()
    boundary.notification.assert_awaited_once()


@pytest.mark.parametrize("error", [TimeoutError("stream timeout"), LLMStreamProviderError("stream failed")])
def test_transient_transport_failure_keeps_existing_scheduler_retry(scheduled_boundary, error):
    boundary = scheduled_boundary
    body = AsyncMock(side_effect=error)
    with pytest.raises(RetryRequested):
        boundary.execute(body)
    boundary.task.retry.assert_called_once_with(exc=error, countdown=300)
    body.assert_awaited_once()
    boundary.finalizer.assert_not_awaited()
    boundary.notification.assert_not_awaited()


def test_credit_exhaustion_keeps_single_top_up_retry(scheduled_boundary):
    boundary = scheduled_boundary
    error = CreditExhaustedError("credits exhausted")
    body = AsyncMock(side_effect=error)
    with pytest.raises(RetryRequested):
        boundary.execute(body)
    boundary.task.retry.assert_called_once_with(exc=error, countdown=120)
    boundary.finalizer.assert_not_awaited()
    boundary.notification.assert_not_awaited()


def test_credit_exhaustion_after_top_up_retry_keeps_existing_terminal_settlement(scheduled_boundary):
    boundary = scheduled_boundary
    boundary.task.request.retries = 1
    error = CreditExhaustedError("credits exhausted")
    body = AsyncMock(side_effect=error)
    assert boundary.execute(body) is None
    boundary.task.retry.assert_not_called()
    boundary.finalizer.assert_awaited_once_with(
        run_id="run", job_id_str="job", result=None, error="credits_exhausted: credits exhausted",
    )
    boundary.notification.assert_awaited_once_with("credits_exhausted: credits exhausted")


@pytest.mark.parametrize("error", ["proposal_llm_unavailable: provider down", "credits_exhausted: out of credit"])
def test_terminal_review_failure_never_posts_a_retry_card(monkeypatch, error):
    # A technical failure is the platform's (ops is told, the next review
    # recovers); only running out of credit is said in chat, with no Retry.
    from packages.core.services import platform_faults
    from packages.core.workspace_chat import service as chat_service

    report = AsyncMock(return_value=True)
    post = AsyncMock()
    workspace = SimpleNamespace(id="ws", entity_id="ent", deleted_at=None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: workspace)),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(platform_faults, "report_platform_fault", report)
    monkeypatch.setattr(chat_service, "post_message", post)
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: session)

    asyncio.run(ai_tasks._handle_strategist_terminal_failure(
        workspace_id="ws", trigger="scheduled", error_msg=error, run_id="run", job_id_str="job",
    ))

    if error.startswith("credits_exhausted"):
        report.assert_not_awaited()
        post.assert_awaited_once()
        assert "pending_action" not in post.await_args.kwargs
    else:
        post.assert_not_awaited()
        report.assert_awaited_once()
        assert report.await_args.kwargs["code"] == "strategist_review_failed"
