"""Content acceptance cannot outlive the current Workspace/factory boundary."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.executor import (
    PlanExecutor, finalize_task_coordinator_submission, prepare_task_coordinator_submission,
)
from packages.core.services.task_acceptance_review import parse_task_acceptance_review
from packages.core.services.task_coordinator_wakeup import claim_task_coordinator_wake, stage_task_coordinator_wake
from test_task_coordinator_executor_handoff import _seed


async def _ready(db):
    seeded = await _seed(db, step_status="done", run_phase="running")
    wake = await stage_task_coordinator_wake(db, task_id=seeded.task.id, reason="fence_test")
    claim, reason = await claim_task_coordinator_wake(
        db, task_id=seeded.task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="review-owner",
        selection=seeded.selection, lease_seconds=120,
    )
    assert reason == "claimed"
    return seeded, claim


def _accepted(snapshot):
    return parse_task_acceptance_review(json.dumps({
        "verdict": "accepted", "evidence": "The reviewed text satisfies the task.", "issues": [],
        "checks": [{"criterion": "Requested result", "pointer": "/steps/0/result/text", "quote": "Done"}],
    }), snapshot)


async def _drift(db, seeded, change):
    if change == "workspace_deleted":
        seeded.workspace.deleted_at = datetime.now(timezone.utc)
    elif change == "workspace_inactive":
        seeded.workspace.status = "archived"
    elif change == "workspace_missing":
        await db.delete(seeded.workspace)
    elif change == "subscription_inactive":
        seeded.subscription.status = "paused"
    elif change == "subscription_rebound":
        replacement = Agent(id=generate_ulid(), entity_id=seeded.task.entity_id, name="Replacement")
        db.add(replacement)
        seeded.subscription.agent_id = replacement.id
    elif change == "subscription_missing":
        await db.delete(seeded.subscription)
    elif change == "subscription_ambiguous":
        db.add(AgentSubscription(
            id=generate_ulid(), entity_id=seeded.task.entity_id, workspace_id=seeded.workspace.id,
            agent_id=generate_ulid(), service_key="writer", status="active",
        ))
    elif change == "task_runtime_changed":
        seeded.task.task_type = "interactive"
    elif change == "run_root_changed":
        seeded.run.root_agent_id = generate_ulid()
    else:
        raise AssertionError(change)
    await db.flush()


@pytest.mark.parametrize("entry", ["prepare", "finalize"])
@pytest.mark.parametrize("change", [
    "workspace_deleted", "workspace_inactive", "workspace_missing", "subscription_inactive",
    "subscription_rebound", "subscription_missing", "subscription_ambiguous",
    "task_runtime_changed", "run_root_changed",
])
async def test_current_admission_required_before_and_after_review(db_session, monkeypatch, entry, change):
    seeded, claim = await _ready(db_session)
    snapshot, gate = await prepare_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert snapshot is not None and gate is None
    forbidden = AsyncMock(side_effect=AssertionError("stale owner must not finalize"))
    monkeypatch.setattr(PlanExecutor, "_finalize", forbidden)
    await _drift(db_session, seeded, change)
    with pytest.raises(ValueError):
        if entry == "prepare":
            await prepare_task_coordinator_submission(
                db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
            )
        else:
            await finalize_task_coordinator_submission(
                db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
                acceptance_review=_accepted(snapshot),
            )
    forbidden.assert_not_awaited()
    assert seeded.task.status == "in_progress" and seeded.plan.status == "running"


async def test_paused_workspace_still_allows_manual_task_admission(db_session):
    seeded, claim = await _ready(db_session)
    seeded.workspace.status = "paused"
    await db_session.flush()
    snapshot, gate = await prepare_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert snapshot is not None and gate is None


async def test_submission_uses_workspace_first_lock_order(db_session):
    seeded, claim = await _ready(db_session)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            statements.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        await prepare_task_coordinator_submission(
            db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
        )
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)
    tables = ["workspaces", "tasks", "agent_subscriptions", "execution_plans",
              "work_leases", "execution_steps", "task_coordinator_runs"]
    first_lock = [next(index for index, sql in enumerate(statements) if f"FROM {table} " in sql) for table in tables]
    assert first_lock == sorted(first_lock)


@pytest_asyncio.fixture
async def committed_scopes(db_session):
    scopes = []
    yield scopes
    await db_session.rollback()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with sessions() as db:
        for task_id, plan_id, entity_id in scopes:
            await db.execute(delete(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id == task_id))
            await db.execute(delete(ExecutionStep).where(ExecutionStep.plan_id == plan_id))
            await db.execute(delete(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id))
            await db.execute(delete(ExecutionPlan).where(ExecutionPlan.id == plan_id))
            await db.execute(delete(Task).where(Task.id == task_id))
            for model in (AgentSubscription, Agent, Workspace):
                await db.execute(delete(model).where(model.entity_id == entity_id))
        await db.commit()


@pytest.mark.parametrize("change", ["workspace_inactive", "subscription_rebound"])
async def test_another_session_can_revoke_during_review_and_finalization_refuses(
    db_session, monkeypatch, committed_scopes, change,
):
    seeded, claim = await _ready(db_session)
    task_id, plan_id = seeded.task.id, seeded.plan.id
    committed_scopes.append((task_id, plan_id, seeded.task.entity_id))
    snapshot, gate = await prepare_task_coordinator_submission(
        db_session, task_id=task_id, plan_id=plan_id, claim=claim,
    )
    assert snapshot is not None and gate is None
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def revoke():
        async with sessions() as other:
            if change == "workspace_inactive":
                workspace = await other.get(Workspace, seeded.workspace.id)
                workspace.status = "archived"
            else:
                subscription = await other.get(AgentSubscription, seeded.subscription.id)
                subscription.agent_id = generate_ulid()
            await other.commit()

    await asyncio.wait_for(revoke(), timeout=2)
    forbidden = AsyncMock(side_effect=AssertionError("stale owner must not finalize"))
    monkeypatch.setattr(PlanExecutor, "_finalize", forbidden)
    async with sessions() as final:
        with pytest.raises(ValueError):
            await finalize_task_coordinator_submission(
                final, task_id=task_id, plan_id=plan_id, claim=claim,
                acceptance_review=_accepted(snapshot),
            )
    forbidden.assert_not_awaited()
