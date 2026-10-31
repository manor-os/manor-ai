"""A Workflow step that failed only on a transient platform fault is re-run.

2026-10-21: Workflow runs failed at their first agent step on a single
``credit_check_unavailable`` (Postgres refusing connections for a moment) or
one hung provider call, while Task agent loops survived the same faults. The
step now parks on a durable timed continuation and is re-executed, bounded
by its retry budget, but only when the failed attempt provably changed
nothing outside the run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from packages.core.ai import workflow_runner as workflow_runner_module
from packages.core.ai.llm_client import CreditCheckUnavailableError
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeToolClassification,
    RuntimeToolEffect,
)
from packages.core.ai.runtime.tool_effect_intents import runtime_record_tool_effect_intent
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_step_resilience import plan_step_transient_retry
from packages.core.tasks import ai_tasks
from test_workflow_expired_claim_recovery import _chain, _start_unexecuted_run

CREDIT_ERROR = (
    "credit_check_unavailable: Unable to verify workspace budget right now. "
    "Please try again shortly."
)


def _loop_result(*, stop_reason="completed", error=None, error_detail=None, effect_state="none"):
    return SimpleNamespace(
        content="Weekly report ready" if stop_reason == "completed" else "The request failed.",
        usage={}, tool_calls_made=[], rounds=1, stop_reason=stop_reason, error=error,
        error_detail=error_detail, control=None, tool_evidence={"effect_state": effect_state},
    )


def _provider_outage(effect_state="none"):
    return _loop_result(
        stop_reason="error", error="llm_provider_unavailable", effect_state=effect_state,
        error_detail={
            "code": "llm_provider_unavailable", "transient": True,
            "message": "no response from the provider after 1 attempt over 363s",
        },
    )


async def _effectful_call_then_credit_outage():
    # A tool that may change the world started before the credit check failed.
    await runtime_record_tool_effect_intent(
        "send_report_email", {}, None,
        RuntimeToolClassification(RuntimeToolEffect.UNKNOWN, reason="unclassified"),
    )
    raise CreditCheckUnavailableError(CREDIT_ERROR)


@pytest.fixture
def scripted_agent(monkeypatch):
    """Fake only the model loop; the runner, ledger fields and gates are real."""
    outcomes: list = []

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    async def loop(**_kwargs):
        outcome = outcomes.pop(0)
        if callable(outcome):
            outcome = await outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(workflow_runner_module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(workflow_runner_module, "runtime_execute_workflow_agent_loop", loop)
    return outcomes


def _report_workflow(**agent_config) -> list[dict]:
    return _chain(
        {"id": "collect", "type": "transform", "config": {"set": {"region": "EMEA"}}},
        {"id": "summarize", "type": "agent", "name": "Summarize sales",
         "config": {"prompt": "Summarize this week's {{collect.region}} sales.", **agent_config}},
    )


async def _run(client, headers, run_id) -> dict:
    return (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()


async def _fire_due_continuation(db_session, monkeypatch, run_id: str) -> None:
    """What the resume sweep does once the backoff has elapsed."""
    from packages.core.database import async_session

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.execute(
        update(WorkflowRun).where(WorkflowRun.id == run_id)
        .values(continuation_due_at=past, continuation_next_attempt_at=past)
    )
    await db_session.commit()
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)
    claimed = await ai_tasks._claim_due_workflow_continuations()
    assert [item[0] for item in claimed] == [run_id]
    assert await WorkflowRunner.resume(
        run_id, execute=True, continuation_token=claimed[0][1], session_factory=async_session,
    ) == "resumed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_failure",
    [CreditCheckUnavailableError(CREDIT_ERROR), _provider_outage()],
    ids=["credit-check-unavailable", "provider-outage"],
)
async def test_transient_failure_parks_the_step_then_reruns_it(
    client, db_session, monkeypatch, scripted_agent, first_failure,
):
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(client, "wftransient", _report_workflow())
    scripted_agent.extend([first_failure, _loop_result()])

    await WorkflowRunner(session_factory=async_session).run(run_id)
    run = await _run(client, headers, run_id)
    assert run["status"] == "paused", run
    assert run["error"] is None
    parked = run["step_results"]["summarize"]
    assert parked["wait_type"] == "transient_retry"
    assert parked["resume_strategy"] == "reexecute"
    assert parked["output"].startswith("Retrying (1/3):")
    assert parked["transient_retry"]["retry"] == 1
    assert parked["transient_retry"]["history"][0]["error"]
    stored = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(stored)
    # Parked on the durable continuation, after a backoff.
    assert stored.continuation_token
    assert stored.continuation_due_at > datetime.now(timezone.utc) + timedelta(seconds=30)

    await _fire_due_continuation(db_session, monkeypatch, run_id)
    run = await _run(client, headers, run_id)
    assert run["status"] == "completed", run
    assert run["step_results"]["summarize"]["output"] == "Weekly report ready"
    assert "transient_retry" not in run["step_results"]["summarize"]
    await db_session.refresh(stored)
    assert stored.continuation_token is None
    assert scripted_agent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "agent_config"),
    [
        (_loop_result(stop_reason="error", error="llm_call_failed"), {}),
        (_provider_outage(effect_state="confirmed"), {}),
        (_effectful_call_then_credit_outage, {}),
        (CreditCheckUnavailableError(CREDIT_ERROR), {"transient_retry": False}),
    ],
    ids=["permanent-provider-error", "outage-after-an-effect", "credit-check-after-an-effectful-call",
         "disabled-by-node-config"],
)
async def test_permanent_or_effectful_failures_fail_the_run_at_once(
    client, db_session, scripted_agent, failure, agent_config,
):
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(client, "wfpermanent", _report_workflow(**agent_config))
    scripted_agent.append(failure)

    await WorkflowRunner(session_factory=async_session).run(run_id)
    run = await _run(client, headers, run_id)
    assert run["status"] == "failed", run
    result = run["step_results"]["summarize"]
    assert result["status"] == "failed"
    assert "transient_retry" not in result
    stored = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(stored)
    assert stored.continuation_token is None


@pytest.mark.asyncio
async def test_exhausted_retry_budget_fails_with_the_original_error(
    client, db_session, monkeypatch, scripted_agent,
):
    from packages.core.database import async_session

    headers, run_id = await _start_unexecuted_run(
        client, "wfexhausted", _report_workflow(transient_retry={"max_retries": 1}),
    )
    scripted_agent.extend([
        CreditCheckUnavailableError(CREDIT_ERROR), CreditCheckUnavailableError(CREDIT_ERROR),
    ])

    await WorkflowRunner(session_factory=async_session).run(run_id)
    run = await _run(client, headers, run_id)
    assert run["status"] == "paused"
    assert run["step_results"]["summarize"]["output"].startswith("Retrying (1/1):")

    await _fire_due_continuation(db_session, monkeypatch, run_id)
    run = await _run(client, headers, run_id)
    assert run["status"] == "failed", run
    assert run["error"].startswith("credit_check_unavailable"), run["error"]
    failed = run["step_results"]["summarize"]
    assert failed["status"] == "failed"
    assert failed["error"] == CREDIT_ERROR
    assert failed["transient_failure"] == "credit_check_unavailable"
    assert failed["transient_retry"]["exhausted"] is True
    assert failed["transient_retry"]["retry"] == 1
    assert len(failed["transient_retry"]["history"]) == 2


@pytest.mark.asyncio
async def test_chat_projection_reports_the_retry_not_a_wait_for_input(monkeypatch):
    from packages.core.services import workflow_chat_projection as projection

    step = {"id": "summarize", "type": "agent"}
    now = datetime.now(timezone.utc)
    parked = plan_step_transient_retry(
        step,
        {"status": "failed", "error": CREDIT_ERROR, "transient_failure": "credit_check_unavailable",
         "effect_state": "none"},
        previous_result=None,
        now=now,
    )
    activity = SimpleNamespace(meta={"workflow_title": "Weekly sales report"}, content="", pending_action=None)
    run = SimpleNamespace(
        id="run-transient", binding_id="binding-1", trigger_source="workspace_chat",
        trigger_data={"_workspace_chat_entrypoint": {
            "enabled": True, "conversation_id": "conversation-1", "activity_message_id": "activity-1",
        }},
        definition_snapshot={"nodes": []}, step_results={"summarize": parked}, variables={},
        status="paused", current_step_id="summarize", effective_attempt_number=1,
        effective_retry_of_run_id=None, effective_retry_from_step_id=None, error=None,
    )

    async def activity_message(*_args, **_kwargs):
        return activity

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(projection, "_activity_message", activity_message)
    monkeypatch.setattr(projection, "_notify_update", noop)
    await projection._project_workflow_run_status_chat(object(), run=run)

    assert activity.meta["workflow_status"] == "running"
    assert activity.content == (
        "Weekly sales report: Retrying (1/3): the credit and budget check was temporarily "
        "unavailable. It continues automatically."
    )
    assert activity.pending_action is None
