"""Real Plan/Dispatcher transitions share one runtime-owned language snapshot."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.constants.supervisor import (
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task, TaskLog
from packages.core.models.user import User
from packages.core.plans.executor import PlanExecutor
from packages.core.plans.response_language import (
    PLAN_RESPONSE_LOCALE_KEY,
    resolve_plan_response_locale,
)
from packages.core.services.task_execution_origin import record_manual_plan_execution


async def _user(db, entity_id, locale):
    user = User(
        id=generate_ulid(), entity_id=entity_id,
        email=f"{generate_ulid()}@language.example", password_hash="test-only",
        status="active", locale=locale,
    )
    db.add(user)
    await db.flush()
    return user


def _session_factory(db):
    @asynccontextmanager
    async def factory():
        yield db
    return factory


async def _pending_plan(db, *, task=None):
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=task.entity_id if task else generate_ulid(),
        task_id=task.id if task else None, status="draft", execution_mode="live",
        plan_dag={"steps": [], "metadata": {"response_locale": "de", "_response_locale": "fr"}},
        dispatcher_state={"cycle_count": 4},
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=plan.entity_id,
        step_key="deliver_report", kind="llm", params={"prompt": "Write a report."},
        step_status="pending", depends_on=[],
    )
    db.add_all([plan, step])
    await db.flush()
    return plan, step


@pytest.mark.asyncio
async def test_cycle_uses_late_manual_receipt_and_keeps_snapshot_on_later_cycle(db_session, monkeypatch):
    plan, _ = await _pending_plan(db_session)
    user = await _user(db_session, plan.entity_id, "zh")
    assert PLAN_RESPONSE_LOCALE_KEY not in plan.dispatcher_state
    # Authenticated manual creation writes this AFTER create_plan_from_dag.
    record_manual_plan_execution(plan, user_id=user.id, action="plan_create")
    announce = AsyncMock()
    monkeypatch.setattr(PlanExecutor, "_announce", announce)

    executor = PlanExecutor(session_factory=_session_factory(db_session))
    first = await executor.run_cycle(plan.id)
    assert first["next_action"] == "schedule_self"
    assert plan.dispatcher_state[PLAN_RESPONSE_LOCALE_KEY] == "zh"
    assert plan.dispatcher_state["cycle_count"] == 4
    assert announce.await_args.kwargs["locale"] == "zh"

    user.locale = "en"
    record_manual_plan_execution(plan, user_id="another-operator", action="plan_resume")
    await db_session.flush()
    await executor.run_cycle(plan.id)
    assert announce.await_args.kwargs["locale"] == "zh"
    assert plan.plan_dag["metadata"]["response_locale"] == "de"


@pytest.mark.asyncio
async def test_cycle_freezes_after_resolving_actual_runtime_actor(db_session, monkeypatch):
    entity_id = generate_ulid()
    creator = await _user(db_session, entity_id, "en")
    actor = await _user(db_session, entity_id, "zh")
    task = Task(id=generate_ulid(), entity_id=entity_id, creator_id=creator.id, title="Report", status="in_progress")
    db_session.add(task)
    await db_session.flush()
    plan, _ = await _pending_plan(db_session, task=task)

    async def resolve_billing(db, task_id, **kwargs):
        assert db is db_session and task_id == task.id
        assert PLAN_RESPONSE_LOCALE_KEY not in plan.dispatcher_state
        return SimpleNamespace(user_id=actor.id)

    monkeypatch.setattr("packages.core.plans.executor.runtime_ensure_task_billing_context", resolve_billing)
    monkeypatch.setattr(PlanExecutor, "_announce", AsyncMock())
    await PlanExecutor(session_factory=_session_factory(db_session)).run_cycle(plan.id)
    assert plan.dispatcher_state[PLAN_RESPONSE_LOCALE_KEY] == "zh"
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id))).scalars().all()
    assert any("执行计划已启动" in log.content for log in logs)


@pytest.mark.asyncio
async def test_legacy_plan_resolves_task_actor_and_ignores_forged_dag_language(db_session):
    entity_id = generate_ulid()
    user = await _user(db_session, entity_id, "zh-CN")
    task = Task(id=generate_ulid(), entity_id=entity_id, creator_id=user.id, title="Legacy", status="in_progress")
    db_session.add(task)
    await db_session.flush()
    plan, _ = await _pending_plan(db_session, task=task)
    assert await resolve_plan_response_locale(db_session, plan) == "zh"
    user.locale = "en"
    task.creator_id = "retired-actor"
    await db_session.flush()
    assert await resolve_plan_response_locale(db_session, plan, user_id="changed-actor") == "zh"


@pytest.mark.asyncio
async def test_pending_approval_does_not_freeze_locale_before_execution(db_session):
    plan, _ = await _pending_plan(db_session)
    plan.status = "pending_approval"
    result = await PlanExecutor(session_factory=_session_factory(db_session)).run_cycle(plan.id)
    assert result["next_action"] == "wait_for_approval"
    assert PLAN_RESPONSE_LOCALE_KEY not in plan.dispatcher_state


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "fail", "needs_human"])
async def test_dispatcher_transitions_use_snapshot_and_preserve_result_data(db_session, monkeypatch, operation):
    from tests.test_dispatcher_envelope_status import _setup_step
    from packages.core.dispatcher import service

    step_id, lease_id = await _setup_step(db_session, kind="action", schema={}, max_attempts=1)
    step = await db_session.get(ExecutionStep, step_id)
    plan = await db_session.get(ExecutionPlan, step.plan_id)
    plan.dispatcher_state = {PLAN_RESPONSE_LOCALE_KEY: "zh"}
    step.started_at = datetime.now(timezone.utc) - timedelta(seconds=3)
    log = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(service, "_safe_task_log", log)
    monkeypatch.setattr(service, "_safe_task_event", AsyncMock())
    monkeypatch.setattr(service, "_safe_chat_step_done", notify)
    monkeypatch.setattr(service, "_safe_chat_step_failed", notify)
    monkeypatch.setattr(service, "_safe_chat_step_needs_human", notify)

    if operation == "complete":
        payload = {"text": "English artifact body", "machine_key": "unchanged"}
        await Dispatcher().complete_lease(db_session, lease_id, result=payload)
        assert step.step_status == "done"
        assert step.result == payload
        assert "完成" in log.await_args.args[2]
        assert "English artifact body" in log.await_args.args[2]
        assert "completed" not in log.await_args.args[2]
    elif operation == "fail":
        error = {"type": "ProviderError", "message": "Exact external error"}
        await Dispatcher().fail_lease(db_session, lease_id, error=error, will_retry=False)
        assert step.step_status in {"failed", "waiting_human"}
        assert step.error["type"] == "ProviderError"
        assert step.error["message"] == "Exact external error"
        failure = next(call.args[2] for call in log.await_args_list if call.args[1] == "step_failed")
        assert "失败" in failure and "Exact external error" in failure
        assert "failed:" not in failure
    else:
        await Dispatcher().lease_needs_human(db_session, lease_id, prompt="Exact requested input")
        assert step.step_status == "waiting_human"
        assert step.human_input_prompt == "Exact requested input"
        assert "等待" in log.await_args.args[2]
        assert "Exact requested input" in log.await_args.args[2]
    assert all(call.kwargs["locale"] == "zh" for call in notify.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [SupervisorVerdict.COMPLETED, SupervisorVerdict.NEEDS_HUMAN])
async def test_finalize_localizes_visible_summary_without_changing_supervisor_or_artifact_contract(db_session, monkeypatch, verdict):
    entity_id = generate_ulid()
    user = await _user(db_session, entity_id, "en")
    task = Task(id=generate_ulid(), entity_id=entity_id, creator_id=user.id, title="Test report", status="in_progress")
    db_session.add(task)
    await db_session.flush()
    plan, step = await _pending_plan(db_session, task=task)
    plan.dispatcher_state = {PLAN_RESPONSE_LOCALE_KEY: "zh"}
    plan.started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    step.step_status = "done"
    step.result = {"text": "English document content"}
    decision = SupervisorDecision(verdict, "Exact model evidence", SupervisorDecisionSource.MODEL)
    monkeypatch.setattr(PlanExecutor, "_supervise_outcome", AsyncMock(return_value=decision))
    recovery = AsyncMock()
    monkeypatch.setattr("packages.core.services.task_chat_hitl.ensure_task_recovery_hitl", recovery)

    await PlanExecutor._finalize(db_session, plan, "completed")
    await db_session.flush()
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id))).scalars().all()
    text = "\n".join(log.content for log in logs)
    assert "Plan completed" not in text
    assert "Task Completed" not in text
    assert "**Verdict:**" not in text
    assert "Exact model evidence" in text
    assert "English document content" in text
    assert "计划" in text
    assert task.actual_output["supervisor_verdict"] == verdict.value
    assert task.actual_output["supervisor_evidence"] == "Exact model evidence"
    assert step.result == {"text": "English document content"}
    if verdict is SupervisorVerdict.NEEDS_HUMAN:
        assert task.status == "waiting_on_customer"
        prompt = recovery.await_args.kwargs["prompt"]
        assert "This task stopped" not in prompt
        assert "任务" in prompt
    else:
        assert task.status == "completed"
    # A duplicate executor delivery does not append another terminal record.
    await PlanExecutor(session_factory=_session_factory(db_session)).run_cycle(plan.id)
    after = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == task.id))).scalars().all()
    assert len(after) == len(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_locale", [None, "en"])
async def test_first_snapshot_preserves_concurrent_manual_receipt_and_locale_winner(db_session, concurrent_locale):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    plan, _ = await _pending_plan(db_session)
    user = await _user(db_session, plan.entity_id, "zh")
    await db_session.commit()
    # Keep the original ORM instance stale while another runtime transaction
    # writes its manual-execution receipt and optionally the first snapshot.
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with factory() as other:
        current = await other.get(ExecutionPlan, plan.id)
        record_manual_plan_execution(current, user_id=user.id, action="plan_dispatch")
        if concurrent_locale:
            current.dispatcher_state = {**current.dispatcher_state, PLAN_RESPONSE_LOCALE_KEY: concurrent_locale}
        await other.commit()

    chosen = await resolve_plan_response_locale(db_session, plan, user_id=user.id)
    await db_session.commit()
    assert chosen == (concurrent_locale or "zh")
    async with factory() as check:
        state = await check.scalar(select(ExecutionPlan.dispatcher_state).where(ExecutionPlan.id == plan.id))
    assert state["_manual_execution"]["action"] == "plan_dispatch"
    assert state["cycle_count"] == 4
    assert state[PLAN_RESPONSE_LOCALE_KEY] == chosen
    assert plan.dispatcher_state == state
