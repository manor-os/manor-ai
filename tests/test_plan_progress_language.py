"""Dispatcher worker-job transitions share one runtime-owned language snapshot."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.services.task_response_language import (
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
