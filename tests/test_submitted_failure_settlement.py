"""Failure-control settlement through the real Dispatcher, without I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.dispatcher.service as dispatcher_module
from packages.core.contracts.envelope import step_result_envelope_schema
from packages.core.contracts.task_output import (
    TaskOutputValueKind,
    task_output_envelope_schema,
)
from packages.core.dispatcher.retry_policy import RetryPolicy
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.services.task_response_language import PLAN_RESPONSE_LOCALE_KEY


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
    state = SimpleNamespace(plan=plan, step=step, lease=lease, policy=RetryPolicy(), statements=[], commits=0)

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
    monkeypatch.setattr("packages.core.database.async_session", Session)

    async def resolve_retry_policy(*_args, **_kwargs):
        return state.policy

    monkeypatch.setattr("packages.core.dispatcher.retry_policy.resolve_retry_policy_for_step", resolve_retry_policy)
    for name in ("_safe_chat_step_failed", "_safe_chat_step_needs_human", "_safe_task_log", "_safe_task_event"):
        monkeypatch.setattr(dispatcher_module, name, AsyncMock())
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
