"""Failure-control settlement through the real worker and Dispatcher, without I/O."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import packages.core.dispatcher.service as dispatcher_module
from packages.core.contracts.envelope import step_result_envelope_schema
from packages.core.contracts.shapes import get_shape
from packages.core.contracts.task_output import (
    TaskOutputValueKind,
    plan_output_contract_schema,
    task_output_envelope_schema,
)
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans.response_language import PLAN_RESPONSE_LOCALE_KEY
from packages.core.services.retry_policy import RetryPolicy
from packages.core.constants.execution import WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
from packages.core.workers import internal


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        assert self.value is not None
        return self.value


@pytest.fixture
def runtime(monkeypatch):
    now = datetime.now(timezone.utc)
    plan = ExecutionPlan(
        id="plan", entity_id="entity", status="running", execution_mode="live",
        plan_dag={"steps": []}, task_id=None,
        dispatcher_state={PLAN_RESPONSE_LOCALE_KEY: "zh"},
    )
    step = ExecutionStep(
        id="step", entity_id="entity", plan_id=plan.id, step_key="write_artifact",
        kind="subagent", step_status="running", attempt_count=1, max_attempts=3,
        current_lease_id="lease", params={"prompt": "Write the required artifact."},
        depends_on=[],
    )
    lease = WorkLease(
        id="lease", entity_id="entity", plan_id=plan.id, step_id=step.id,
        worker_id="worker", status="active", lease_until=now + timedelta(minutes=5),
    )
    state = SimpleNamespace(
        plan=plan, step=step, lease=lease, policy=RetryPolicy(), payload=None,
        messages=[], statements=[], commits=0,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, statement):
            state.statements.append(statement)
            model = statement.column_descriptions[0]["entity"]
            return _Result({ExecutionPlan: plan, ExecutionStep: step, WorkLease: lease, Worker: None}[model])

        async def get(self, model, identity):
            assert model is ExecutionPlan and identity == plan.id
            return plan

        async def commit(self):
            state.commits += 1

        async def flush(self):
            pass

        def add(self, _value):
            pass

    state.session = Session()
    monkeypatch.setattr(internal, "async_session", Session)
    monkeypatch.setattr("packages.core.database.async_session", Session)
    # The worker passes ``on_inactive=`` (lease revocation cancels the body);
    # the fake must accept it like the real ``_start_lease_heartbeat`` does.
    monkeypatch.setattr(internal, "_start_lease_heartbeat", lambda _lease, **_kwargs: None)
    monkeypatch.setattr(internal, "resolve_step_deadline", AsyncMock(return_value=SimpleNamespace(
        max_runtime_seconds=30, source="test",
    )))

    async def resolve_retry_policy(*_args, **_kwargs):
        return state.policy

    monkeypatch.setattr("packages.core.services.retry_policy.resolve_retry_policy_for_step", resolve_retry_policy)
    monkeypatch.setattr("packages.core.ai.context.build_agent_context", AsyncMock(return_value=SimpleNamespace(
        system_prompt="test", runtime_envelope=None, tools=[], allowed_tool_names=[],
        tool_profile="test", model="test", llm_metadata=None,
    )))

    async def loop(**kwargs):
        kwargs["dynamic_tool_handlers"]["submit_result"](state.payload)
        return SimpleNamespace(result=SimpleNamespace(
            usage={}, content="", messages=state.messages, rounds=1,
            tool_calls_made=[], stop_reason="submit_result",
        ))

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    state.persist_events = AsyncMock()
    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", state.persist_events)
    state.complete = AsyncMock(side_effect=AssertionError("failure cannot complete a lease"))
    monkeypatch.setattr(Dispatcher, "complete_lease", state.complete)
    state.artifact = AsyncMock(side_effect=AssertionError("failure cannot synthesize an artifact"))
    monkeypatch.setattr(internal, "_persist_subagent_text_artifact", state.artifact)
    monkeypatch.setattr(internal, "_merge_artifact_refs", Mock(side_effect=AssertionError("failure cannot enter output enrichment")))
    for name in ("_safe_chat_step_failed", "_safe_chat_step_needs_human", "_safe_task_log", "_safe_task_event"):
        monkeypatch.setattr(dispatcher_module, name, AsyncMock())
    state.stage_wake = AsyncMock()
    monkeypatch.setattr(
        "packages.core.plans.wakeup.stage_plan_cycle_wake",
        state.stage_wake,
    )
    return state


def _submission(*, retryable=False, requires_human=True):
    return {
        # A model summary resembling a transient runtime signature must not
        # refund an executed attempt or override explicit failure control.
        "summary": "Chrome extension is unavailable",
        "status": "failed",
        "failure": {
            "reason": "file.write denied",
            "blockers": ["file.write"],
            "retryable": retryable,
            "requires_human": requires_human,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["plan.output_shape", "plan.expected_output_schema"])
@pytest.mark.parametrize(
    ("retryable", "requires_human", "attempt", "auto_human", "expected_status"),
    [
        (False, True, 1, False, "failed"),
        (False, False, 1, False, "failed"),
        (True, False, 1, False, "pending"),
        (True, False, 3, False, "failed"),
        (False, True, 1, True, "failed"),
    ],
)
async def test_submitted_failure_settles_without_success_or_unbounded_retry(
    runtime, source, retryable, requires_human, attempt, auto_human, expected_status,
):
    runtime.step.expected_output_schema = plan_output_contract_schema(
        get_shape("ArtifactResult").json_schema(), source=source,
    )
    runtime.step.attempt_count = attempt
    runtime.policy = RetryPolicy(max_attempts=3, auto_human_on_exhausted=auto_human)
    runtime.payload = _submission(retryable=retryable, requires_human=requires_human)

    outcome = await internal._execute_claimed_lease(runtime.lease.id)

    assert outcome["outcome"] == "failed"
    assert runtime.lease.status == "failed"
    assert runtime.step.step_status == expected_status
    assert runtime.step.attempt_count == attempt
    assert runtime.step.current_lease_id is None
    assert runtime.step.result is None
    assert runtime.lease.result is None
    assert runtime.step.error["type"] == "StepResultFailed"
    assert runtime.step.error["failure"] == runtime.payload["failure"]
    assert runtime.step.last_execution_error == runtime.step.error
    assert "transient_retry" not in runtime.step.error
    runtime.complete.assert_not_awaited()
    runtime.artifact.assert_not_awaited()
    runtime.persist_events.assert_awaited_once()
    runtime.stage_wake.assert_awaited_once()
    wake_args, wake_kwargs = runtime.stage_wake.await_args
    assert wake_args[1] == runtime.plan.id
    assert wake_kwargs == {"reason": "lease_failed"}
    assert dispatcher_module._safe_chat_step_failed.await_args.kwargs["locale"] == "zh"
    dispatcher_module._safe_chat_step_needs_human.assert_not_awaited()
    assert runtime.step.human_input_prompt is None

@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_submission", [False, True])
async def test_pending_approval_retains_priority_over_submitted_failure(runtime, monkeypatch, malformed_submission):
    runtime.step.expected_output_schema = plan_output_contract_schema(get_shape("ArtifactResult").json_schema())
    runtime.payload = _submission()
    if malformed_submission:
        runtime.payload.pop("failure")
    pending = {"kind": "needs_confirmation", "prompt": "Approve the exact action", "approval_token": "test-only"}
    runtime.messages = [{"role": "tool", "content": json.dumps({"_pending_action": pending})}]
    needs_human = AsyncMock()
    fail = AsyncMock(side_effect=AssertionError("pending approval must not fail the lease"))
    monkeypatch.setattr(Dispatcher, "lease_needs_human", needs_human)
    monkeypatch.setattr(Dispatcher, "fail_lease", fail)

    outcome = await internal._execute_claimed_lease(runtime.lease.id)

    assert outcome["outcome"] == "needs_human"
    reported = dict(needs_human.await_args.kwargs)
    # An approval pause raised after the loop returned now parks the attempt's
    # transcript so the resumed attempt continues it instead of re-running
    # the step; the pending action itself is reported unchanged.
    checkpoint = reported.pop("runtime_checkpoint", None)
    assert reported == {
        "prompt": pending["prompt"],
        "pending_action": pending,
        "expected_execution_claim_id": None,
    }
    assert checkpoint is None or checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    fail.assert_not_awaited()
    runtime.complete.assert_not_awaited()
    runtime.artifact.assert_not_awaited()
    runtime.persist_events.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_contract", [False, True])
async def test_envelope_retryable_failure_still_exhausts_attempt_budget(runtime, task_contract):
    runtime.step.expected_output_schema = (
        task_output_envelope_schema({"type": "string"})
        if task_contract else step_result_envelope_schema()
    )
    runtime.step.attempt_count = 3
    payload = _submission(retryable=True, requires_human=False)
    payload["summary"] = "The source could not be read."

    await Dispatcher()._complete_lease_inner(
        runtime.session, runtime.lease.id, result=payload,
        cost=None, evidence_refs=None, metadata=None,
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE if task_contract else None,
    )

    assert runtime.step.step_status == "failed"
    assert runtime.lease.status == "failed"
    assert runtime.step.attempt_count == 3
    assert runtime.step.error["failure"] == payload["failure"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["missing_failure", "invalid_boolean", "mixed_result", "success_with_failure"])
async def test_malformed_failure_settles_as_protocol_error_without_artifact(runtime, malformed):
    runtime.step.expected_output_schema = plan_output_contract_schema(get_shape("ArtifactResult").json_schema())
    runtime.payload = _submission()
    if malformed == "missing_failure":
        runtime.payload.pop("failure")
    elif malformed == "invalid_boolean":
        runtime.payload["failure"]["retryable"] = "false"
    else:
        runtime.payload["result"] = {"files": []}
        if malformed == "success_with_failure":
            runtime.payload["status"] = "succeeded"

    outcome = await internal._execute_claimed_lease(runtime.lease.id)

    assert outcome["outcome"] == "failed"
    assert runtime.lease.status == "failed"
    assert runtime.step.step_status == "pending"
    assert runtime.step.error["type"] == "TaskOutputProtocolError"
    assert "failure" not in runtime.step.error
    assert runtime.step.result is None
    runtime.complete.assert_not_awaited()
    runtime.artifact.assert_not_awaited()
    runtime.persist_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_rate_limit_keeps_its_distinct_transient_recovery(runtime, monkeypatch):
    from packages.core.ai.llm_client import LLMRateLimited

    async def rate_limited(_snapshot):
        raise LLMRateLimited(retry_after=10)

    monkeypatch.setattr(internal, "_execute_by_kind", rate_limited)

    outcome = await internal._execute_claimed_lease(runtime.lease.id)

    assert outcome["outcome"] == "retry"
    assert runtime.step.step_status == "pending"
    assert runtime.step.attempt_count == 0
    assert runtime.step.error["type"] == "LLMRateLimited"
    assert runtime.step.error["retry_after_seconds"] == 10
    assert runtime.step.error["transient_retry"]["count"] == 1
    runtime.complete.assert_not_awaited()
