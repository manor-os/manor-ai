"""Concurrent Plan cycles must not duplicate terminal work or announcements."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.plans.executor import PlanExecutor


async def _running_plan(db):
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=generate_ulid(), status="running",
        execution_mode="live", started_at=datetime.now(timezone.utc),
        plan_dag={"steps": []}, dispatcher_state={"_response_locale": "en"},
    )
    db.add(plan)
    await db.commit()
    return plan.id


@pytest.mark.asyncio
async def test_duplicate_cycle_defers_without_repeating_terminal_work(db_session, monkeypatch):
    plan_id = await _running_plan(db_session)
    runner = PlanExecutor(session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False))
    entered = asyncio.Event()
    release = asyncio.Event()
    finalize = PlanExecutor._finalize
    finalized = []

    async def hold_first(db, plan, status):
        finalized.append(plan.id)
        if len(finalized) == 1:
            entered.set()
            await release.wait()
        return await finalize(db, plan, status)

    announce = AsyncMock()
    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(hold_first))
    monkeypatch.setattr(PlanExecutor, "_announce", announce)
    first = asyncio.create_task(runner.run_cycle(plan_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        duplicate = await asyncio.wait_for(runner.run_cycle(plan_id), timeout=3)
        assert duplicate["next_action"] == "schedule_self"
        assert duplicate["delay_seconds"] > 0
        assert len(finalized) == 1
        # A different Plan must remain independently runnable.
        other_id = await _running_plan(db_session)
        other = await asyncio.wait_for(runner.run_cycle(other_id), timeout=3)
        assert other["status"] == "completed"
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=3)

    replay = await runner.run_cycle(plan_id)
    assert replay["status"] == "completed" and replay["next_action"] == "stop"
    assert finalized.count(plan_id) == 1
    assert announce.await_count == 2  # one receipt per distinct Plan


@pytest.mark.asyncio
async def test_failed_cycle_releases_claim_for_normal_retry(db_session, monkeypatch):
    plan_id = await _running_plan(db_session)
    runner = PlanExecutor(session_factory=async_sessionmaker(db_session.bind, expire_on_commit=False))
    finalize = PlanExecutor._finalize
    calls = 0

    async def fail_once(db, plan, status):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retryable test failure")
        return await finalize(db, plan, status)

    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(fail_once))
    monkeypatch.setattr(PlanExecutor, "_announce", AsyncMock())
    with pytest.raises(RuntimeError, match="retryable test failure"):
        await runner.run_cycle(plan_id)
    result = await runner.run_cycle(plan_id)
    assert result["status"] == "completed"
    assert calls == 2
