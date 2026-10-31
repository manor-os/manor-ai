"""Task-level coordinator storage and generation-fenced wake regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent, RuntimeRun
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.services.task_coordinator_wakeup import (
    TASK_COORDINATOR_AWAITING_ACK,
    TASK_COORDINATOR_OUTBOX_EVENT,
    checkpoint_task_coordinator_run,
    claim_task_coordinator_wake,
    ensure_task_coordinator_run,
    release_task_coordinator_claim,
    renew_task_coordinator_claim,
    settle_task_coordinator_wake,
    stage_task_coordinator_wake,
    task_coordinator_delivery_state,
    task_coordinator_outbox_dedupe_key,
)
from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
    task_execution_intent_generation,
)
from packages.core.tasks.runtime_tasks import dispatch_runtime_outbox_once


@pytest.mark.asyncio
async def test_external_task_cancel_reconciles_run(db_session, monkeypatch):
    from packages.core import database
    from packages.core.tasks.runtime_tasks import reconcile_terminal_task_coordinator_runs_once

    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    # This sweep is global, and standalone DB tests can leave committed runs.
    # Drain those before creating the Task whose exact counts we assert below.
    while await reconcile_terminal_task_coordinator_runs_once():
        pass

    task = await _task(db_session)
    run = await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    run.phase = TaskCoordinatorPhase.WAITING_CHILD.value
    await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="pending_child",
    )
    task.status = "cancelled"
    await db_session.commit()

    assert await reconcile_terminal_task_coordinator_runs_once() == 1
    await db_session.refresh(run)
    assert run.phase == TaskCoordinatorPhase.CANCELLED.value
    assert run.wake_generation is None
    assert await reconcile_terminal_task_coordinator_runs_once() == 0


async def _task(db) -> Task:
    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        title="Coordinate a Workspace Task",
        status="in_progress",
        task_type="general",
        details={},
    )
    db.add(task)
    await db.flush()
    return task


def _selection() -> CoordinatorRuntimeSelection:
    root = TaskPlanServiceBinding(
        subscription_id=generate_ulid(),
        service_key="owner",
        agent_id=generate_ulid(),
    )
    return CoordinatorRuntimeSelection(root=root, bindings=(root,))


async def _event(db, task_id: str) -> RuntimeOutboxEvent:
    return (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task_id)
        ).execution_options(populate_existing=True)
    )).scalar_one()


@pytest.mark.asyncio
async def test_one_task_run_pins_factory_identity_and_workspace_scope(db_session):
    task = await _task(db_session)
    selection = _selection()
    first = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    second = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    assert first.id == second.id
    assert first.phase == TaskCoordinatorPhase.READY.value
    assert first.entity_id == task.entity_id
    assert first.workspace_id == task.workspace_id
    assert first.root_subscription_id == selection.root.subscription_id
    assert first.root_agent_id == selection.root.agent_id
    assert first.selection_fingerprint == selection.stable_fingerprint()
    assert first.lifecycle_generation == task_execution_intent_generation(task)
    assert (await db_session.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
    )).scalars().all() == [first]

    with pytest.raises(ValueError, match="factory selection changed"):
        await ensure_task_coordinator_run(
            db_session, task=task, selection=_selection(),
        )


@pytest.mark.asyncio
async def test_new_task_lifecycle_resets_stopped_run_and_rotates_old_wake(db_session):
    task = await _task(db_session)
    old_selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=old_selection)
    old_lifecycle = run.lifecycle_generation
    old_wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="old_lifecycle",
    )
    assert old_wake is not None
    run.phase = TaskCoordinatorPhase.WAITING_HUMAN.value
    run.checkpoint = {"messages": [{"role": "assistant", "content": "old"}]}
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    new_selection = _selection()

    reopened = await ensure_task_coordinator_run(
        db_session, task=task, selection=new_selection,
    )
    assert reopened.id == run.id
    assert reopened.lifecycle_generation != old_lifecycle
    assert reopened.lifecycle_generation == task_execution_intent_generation(task)
    assert reopened.phase == TaskCoordinatorPhase.READY.value
    assert reopened.current_plan_id is None
    # A paused agent loop keeps its transcript across the new lifecycle and
    # re-checks external state on its next round.
    assert reopened.checkpoint["messages"] == [{"role": "assistant", "content": "old"}]
    assert reopened.checkpoint["retried_after"]["after"] == "task_resumed"
    assert reopened.root_subscription_id == new_selection.root.subscription_id
    assert reopened.selection_fingerprint == new_selection.stable_fingerprint()
    assert reopened.wake_generation != old_wake.wake_generation
    event = await _event(db_session, task.id)
    assert event.id == old_wake.event_id
    assert event.payload["wake_generation"] == reopened.wake_generation
    assert await task_coordinator_delivery_state(db_session, event) == (reopened, "ready")
    stale, reason = await claim_task_coordinator_wake(
        db_session, task_id=task.id, event_id=old_wake.event_id,
        wake_generation=old_wake.wake_generation, claim_token="old-owner",
        selection=new_selection, lease_seconds=60,
    )
    assert stale is None and reason == "stale_generation"
    assert await settle_task_coordinator_wake(
        db_session, task_id=task.id, event_id=old_wake.event_id,
        wake_generation=old_wake.wake_generation, claim_token="old-owner",
    ) is False


@pytest.mark.asyncio
async def test_changed_lifecycle_rejects_live_claim(db_session):
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    old_lifecycle = run.lifecycle_generation
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="old_lifecycle",
    )
    assert wake is not None
    run.phase = TaskCoordinatorPhase.WAITING_HUMAN.value
    run.claim_token = "old-owner"
    run.claim_generation = wake.wake_generation
    run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(ValueError, match="live claim"):
        await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    assert run.lifecycle_generation == old_lifecycle
    assert run.wake_generation == wake.wake_generation


@pytest.mark.asyncio
async def test_changed_task_generation_cannot_claim_unreopened_run(db_session):
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="old_lifecycle",
    )
    assert wake is not None
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await db_session.flush()
    event = await _event(db_session, task.id)
    assert await task_coordinator_delivery_state(db_session, event) == (None, "stale_lifecycle")
    stale, reason = await claim_task_coordinator_wake(
        db_session, task_id=task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="stale-owner",
        selection=selection, lease_seconds=60,
    )
    assert stale is None and reason == "stale_lifecycle"
    assert run.wake_generation == wake.wake_generation


@pytest.mark.asyncio
async def test_stale_generation_cannot_claim_or_ack_new_wake(db_session):
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    first = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="first",
    )
    assert first is not None
    claim, reason = await claim_task_coordinator_wake(
        db_session,
        task_id=task.id,
        event_id=first.event_id,
        wake_generation=first.wake_generation,
        claim_token="owner-one",
        selection=selection,
        lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None
    second = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="child_completed",
    )
    assert second is not None
    assert second.event_id == first.event_id
    assert second.wake_generation != first.wake_generation
    assert run.claim_token == "owner-one"
    assert run.claim_generation == first.wake_generation
    with pytest.raises(ValueError, match="wake generation was lost"):
        await stage_task_coordinator_wake(
            db_session, task_id=task.id, reason="stale_owner",
            claim_token="owner-one", claim_generation=first.wake_generation,
        )

    stale, reason = await claim_task_coordinator_wake(
        db_session,
        task_id=task.id,
        event_id=first.event_id,
        wake_generation=first.wake_generation,
        claim_token="stale-delivery",
        selection=selection,
        lease_seconds=60,
    )
    assert stale is None and reason == "stale_generation"
    assert await settle_task_coordinator_wake(
        db_session,
        task_id=task.id,
        event_id=first.event_id,
        wake_generation=first.wake_generation,
        claim_token="owner-one",
    ) is False
    assert (await _event(db_session, task.id)).delivered_at is None
    assert await release_task_coordinator_claim(
        db_session, task_id=task.id,
        claim_token="owner-one", claim_generation=first.wake_generation,
    ) is True
    next_claim, reason = await claim_task_coordinator_wake(
        db_session,
        task_id=task.id,
        event_id=second.event_id,
        wake_generation=second.wake_generation,
        claim_token="owner-two",
        selection=selection,
        lease_seconds=60,
    )
    assert reason == "claimed" and next_claim is not None
    assert await settle_task_coordinator_wake(
        db_session,
        task_id=task.id,
        event_id=second.event_id,
        wake_generation=second.wake_generation,
        claim_token="owner-two",
    ) is True
    assert (await _event(db_session, task.id)).delivered_at is not None


@pytest.mark.asyncio
async def test_handoff_reuses_existing_pending_wake_without_rotating(db_session):
    task = await _task(db_session)
    await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    first = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="child_completed",
    )
    assert first is not None
    second = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="plan_terminal_handoff",
        if_pending=True,
    )
    assert second == first
    event = await _event(db_session, task.id)
    assert event.payload["reason"] == "child_completed"


@pytest.mark.asyncio
async def test_checkpoint_and_renew_require_live_exact_claim(db_session):
    task = await _task(db_session)
    selection = _selection()
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="start",
    )
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db_session,
        task_id=task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="owner",
        selection=selection,
        lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None
    assert await renew_task_coordinator_claim(
        db_session, task_id=task.id, claim_token="wrong",
        claim_generation=wake.wake_generation, lease_seconds=60,
    ) is False
    assert await renew_task_coordinator_claim(
        db_session, task_id=task.id, claim_token="owner",
        claim_generation=wake.wake_generation, lease_seconds=60,
    ) is True
    new_version = await checkpoint_task_coordinator_run(
        db_session,
        task_id=task.id,
        claim_token="owner",
        claim_generation=wake.wake_generation,
        expected_state_version=claim.state_version,
        checkpoint={"rounds": 1, "messages": []},
        phase=TaskCoordinatorPhase.RUNNING.value,
    )
    assert new_version == claim.state_version + 1
    assert run.checkpoint == {"rounds": 1, "messages": []}
    assert await checkpoint_task_coordinator_run(
        db_session,
        task_id=task.id,
        claim_token="owner",
        claim_generation=wake.wake_generation,
        expected_state_version=claim.state_version,
        checkpoint={"rounds": 2},
    ) is None
    run.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()
    assert await renew_task_coordinator_claim(
        db_session, task_id=task.id, claim_token="owner",
        claim_generation=wake.wake_generation, lease_seconds=60,
    ) is False
    assert await checkpoint_task_coordinator_run(
        db_session,
        task_id=task.id,
        claim_token="owner",
        claim_generation=wake.wake_generation,
        expected_state_version=new_version,
        checkpoint={"rounds": 2},
    ) is None


@pytest.mark.asyncio
async def test_publisher_redelivers_until_exact_consumer_ack(db_session, monkeypatch):
    # The global publisher can recreate events from earlier committed runs,
    # so reset both the queue and its recovery source before this target.
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.execute(delete(RuntimeRun))
    await db_session.commit()
    task = await _task(db_session)
    selection = _selection()
    await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="start",
    )
    assert wake is not None
    await db_session.commit()
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        lambda name, **kwargs: sent.append((name, kwargs)),
    )
    assert await dispatch_runtime_outbox_once() == 1
    event = await _event(db_session, task.id)
    assert event.delivered_at is None
    assert event.last_error == TASK_COORDINATOR_AWAITING_ACK
    assert sent[0][0] == TASK_COORDINATOR_OUTBOX_EVENT
    assert sent[0][1]["kwargs"]["wake_generation"] == wake.wake_generation
    assert set(sent[0][1]["kwargs"]) == {
        "event_id", "task_id", "wake_generation", "reason",
    }
    event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    assert await dispatch_runtime_outbox_once() == 1
    assert [name for name, _kwargs in sent] == [
        TASK_COORDINATOR_OUTBOX_EVENT,
        TASK_COORDINATOR_OUTBOX_EVENT,
    ]
    assert (await _event(db_session, task.id)).delivered_at is None


# ── B2: parked phases and absolute wake times ──────────────────────────


def test_parked_wait_phases_are_non_terminal_members():
    assert TaskCoordinatorPhase.WAITING_TIMER.value == "waiting_timer"
    assert TaskCoordinatorPhase.WAITING_JOB.value == "waiting_job"
    terminal = TaskCoordinatorPhase.terminal()
    assert "waiting_timer" not in terminal and "waiting_job" not in terminal
    # The column is String(24) without a DB enum: every member must fit.
    assert all(len(member.value) <= 24 for member in TaskCoordinatorPhase)


@pytest.mark.asyncio
async def test_stage_accepts_an_absolute_wake_time(db_session):
    task = await _task(db_session)
    await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    at = datetime.now(timezone.utc) + timedelta(days=3, minutes=7)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="timer", available_at=at,
    )
    await db_session.commit()
    event = await _event(db_session, task.id)
    assert abs((event.available_at - at).total_seconds()) < 1
    assert wake.available_at == event.available_at

    # A past absolute time is delivered at once, never in the past.
    before = datetime.now(timezone.utc)
    await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="timer",
        available_at=before - timedelta(hours=2),
    )
    await db_session.commit()
    event = await _event(db_session, task.id)
    assert before - timedelta(seconds=1) <= event.available_at <= datetime.now(timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(("delay", "expected_days"), [
    (float("nan"), 0), (float("-inf"), 0), (-30, 0),
    (float("inf"), 90), (10 * 365 * 86400, 90),
])
async def test_stage_delay_is_finite_and_bounded(db_session, delay, expected_days):
    task = await _task(db_session)
    await ensure_task_coordinator_run(db_session, task=task, selection=_selection())
    before = datetime.now(timezone.utc)
    await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="bounded", delay_seconds=delay,
    )
    await db_session.commit()
    event = await _event(db_session, task.id)
    offset = (event.available_at - before).total_seconds()
    assert abs(offset - expected_days * 86400) < 5
