"""M1 wave 3 — ledger adapters wiring existing execution paths into workspace_events.

Covers:
* task status transitions through the unified state machine entry point
  (root/causation correlation, workspace-only scope, idempotent replay)
* approval lifecycle events (requested / granted / denied / consumed)
* goal measurement events (measured / pace change / achieved)
* strategist proposal item decisions (approved / rejected)
* adapter failure safety — a broken ledger never breaks the host flow
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services.task_state_machine import apply_task_status_transition


def _mk_workspace(db, *, entity_id: str | None = None) -> Workspace:
    ws = Workspace(
        id=generate_ulid(),
        entity_id=entity_id or generate_ulid(),
        name="Ledger Adapter WS",
        status="active",
    )
    db.add(ws)
    return ws


def _mk_task(db, ws: Workspace | None, *, entity_id: str | None = None,
             status: str = "pending", details: dict | None = None) -> Task:
    task = Task(
        id=generate_ulid(),
        entity_id=(ws.entity_id if ws else entity_id) or generate_ulid(),
        workspace_id=ws.id if ws else None,
        title="ledger adapter task",
        status=status,
        details=details or {},
    )
    db.add(task)
    return task


async def _events(db, *, entity_id: str, event_type: str | None = None) -> list[WorkspaceEvent]:
    stmt = select(WorkspaceEvent).where(WorkspaceEvent.entity_id == entity_id)
    if event_type:
        stmt = stmt.where(WorkspaceEvent.event_type == event_type)
    stmt = stmt.order_by(WorkspaceEvent.id.asc())
    return list((await db.execute(stmt)).scalars().all())


# ── Task transitions ───────────────────────────────────────────────

async def test_task_transition_emits_started_and_completed_with_batch_root(db_session):
    ws = _mk_workspace(db_session)
    task = _mk_task(
        db_session, ws,
        details={"workspace_work_batch_id": "batch_1", "strategist_review_id": "rv_1"},
    )
    await db_session.flush()

    await apply_task_status_transition(task, "in_progress", db=db_session)
    await apply_task_status_transition(task, "completed", db=db_session)

    started = await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_STARTED)
    completed = await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_COMPLETED)
    assert len(started) == 1 and len(completed) == 1

    for row in (started[0], completed[0]):
        assert row.workspace_id == ws.id
        assert row.source_kind == "task"
        assert row.source_id == task.id
        assert row.root_execution_id == "batch_1"     # cohort batch honored
        assert row.causation_id == "rv_1"             # strategist review
        assert row.actor_kind == "system"
    assert started[0].status == "in_progress"
    assert completed[0].status == "completed"


async def test_task_transition_self_root_fallback_and_actor_threading(db_session):
    ws = _mk_workspace(db_session)
    task = _mk_task(db_session, ws)  # no batch / review details
    await db_session.flush()

    await apply_task_status_transition(
        task, "in_progress", db=db_session, actor_kind="user", actor_id="user_9",
    )

    [row] = await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_STARTED)
    assert row.root_execution_id == task.id          # self-root fallback
    assert row.causation_id is None
    assert row.actor_kind == "user"
    assert row.actor_id == "user_9"


async def test_entity_level_task_without_workspace_writes_nothing(db_session):
    entity_id = generate_ulid()
    task = _mk_task(db_session, None, entity_id=entity_id)
    await db_session.flush()

    await apply_task_status_transition(task, "in_progress", db=db_session)
    await apply_task_status_transition(task, "completed", db=db_session)

    assert task.status == "completed"
    assert await _events(db_session, entity_id=entity_id) == []


async def test_reapplied_terminal_transition_is_idempotent(db_session):
    ws = _mk_workspace(db_session)
    task = _mk_task(db_session, ws)
    await db_session.flush()

    await apply_task_status_transition(task, "in_progress", db=db_session)
    await apply_task_status_transition(task, "completed", db=db_session)
    # Reopen + re-run + re-complete (manual retry path).
    await apply_task_status_transition(task, "pending", db=db_session)
    await apply_task_status_transition(task, "in_progress", db=db_session)
    await apply_task_status_transition(task, "completed", db=db_session)

    assert len(await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_COMPLETED)) == 1
    # execution_started is attempt-free by design: first start wins.
    assert len(await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_STARTED)) == 1


async def test_proposed_status_transition_emits_no_execution_event(db_session):
    ws = _mk_workspace(db_session)
    task = _mk_task(db_session, ws, status="created")
    await db_session.flush()

    await apply_task_status_transition(task, "proposed", db=db_session)

    assert await _events(db_session, entity_id=ws.entity_id) == []


# ── Approvals ──────────────────────────────────────────────────────

async def test_approval_lifecycle_events(db_session):
    from packages.core.governance.approvals import (
        ApprovalOrigin, ApprovalSubject, consume_approval,
        deny_approval, grant_approval, mint_approval_request,
    )

    ws = _mk_workspace(db_session)
    await db_session.flush()
    step_id = generate_ulid()
    subject = ApprovalSubject(
        entity_id=ws.entity_id, action_key="email.send", workspace_id=ws.id,
    )
    req = await mint_approval_request(
        db_session, subject=subject,
        origin=ApprovalOrigin(kind="step", step_id=step_id),
    )

    [requested] = await _events(db_session, entity_id=ws.entity_id, event_type=et.APPROVAL_REQUESTED)
    assert requested.source_kind == "approval"
    assert requested.source_id == req.id
    assert requested.causation_id == step_id
    assert requested.actor_kind == "system"

    await grant_approval(db_session, req, by_user_id="user_1", via="chat_card")
    [granted] = await _events(db_session, entity_id=ws.entity_id, event_type=et.APPROVAL_GRANTED)
    assert granted.actor_kind == "user"
    assert granted.actor_id == "user_1"
    assert granted.status == "granted"

    await consume_approval(db_session, req)
    [consumed] = await _events(db_session, entity_id=ws.entity_id, event_type=et.APPROVAL_CONSUMED)
    assert consumed.status == "consumed"

    # A second request on another step gets denied.
    req2 = await mint_approval_request(
        db_session, subject=subject,
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
    await deny_approval(db_session, req2, by_user_id="user_2", via="chat_card", reason="too risky")
    [denied] = await _events(db_session, entity_id=ws.entity_id, event_type=et.APPROVAL_DENIED)
    assert denied.source_id == req2.id
    assert denied.actor_id == "user_2"


async def test_entity_only_approval_writes_no_event(db_session):
    from packages.core.governance.approvals import (
        ApprovalOrigin, ApprovalSubject, mint_approval_request,
    )

    entity_id = generate_ulid()
    await mint_approval_request(
        db_session,
        subject=ApprovalSubject(entity_id=entity_id, action_key="email.send"),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
    assert await _events(db_session, entity_id=entity_id) == []


# ── Goals ──────────────────────────────────────────────────────────

async def test_goal_measurement_and_achievement_events(db_session):
    from packages.core.models.goal import Goal
    from packages.core.goals.service import record_measurement

    ws = _mk_workspace(db_session)
    goal = Goal(
        id=generate_ulid(),
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        title="Followers",
        metric_key="followers",
        target_value=Decimal("10"),
        baseline_value=Decimal("0"),
        status="active",
    )
    db_session.add(goal)
    await db_session.flush()

    t1 = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    await record_measurement(db_session, goal, value=5, source="manual", measured_at=t1)

    measured = await _events(db_session, entity_id=ws.entity_id, event_type=et.GOAL_MEASURED)
    assert len(measured) == 1
    assert measured[0].source_kind == "goal"
    assert measured[0].source_id == goal.id
    assert measured[0].goal_refs == [goal.id]
    assert measured[0].payload == {"value": 5.0, "source": "manual"}
    assert measured[0].status is None
    assert await _events(db_session, entity_id=ws.entity_id, event_type=et.GOAL_ACHIEVED) == []

    t2 = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    await record_measurement(db_session, goal, value=10, source="manual", measured_at=t2)

    assert len(await _events(db_session, entity_id=ws.entity_id, event_type=et.GOAL_MEASURED)) == 2
    [achieved] = await _events(db_session, entity_id=ws.entity_id, event_type=et.GOAL_ACHIEVED)
    assert achieved.payload == {"value": 10.0}
    pace_changes = await _events(db_session, entity_id=ws.entity_id, event_type=et.GOAL_PACE_CHANGED)
    assert pace_changes and pace_changes[-1].payload["new_pace"] == "achieved"


# ── Strategist proposals ───────────────────────────────────────────

async def test_approve_proposal_emits_item_approved_with_batch_root(db_session):
    from packages.core.strategist import service as strategist_service

    ws = _mk_workspace(db_session)
    review_id = "rv_ledger_approve"
    tasks = [
        _mk_task(db_session, ws, status="proposed", details={"strategist_review_id": review_id})
        for _ in range(2)
    ]
    await db_session.commit()

    moved = await strategist_service.approve_proposal(
        db_session, entity_id=ws.entity_id, review_id=review_id, actor_id="user_1",
    )
    assert set(moved) == {t.id for t in tasks}

    approved = await _events(db_session, entity_id=ws.entity_id, event_type=et.PROPOSAL_ITEM_APPROVED)
    assert {row.source_id for row in approved} == {t.id for t in tasks}
    batch_id = tasks[0].details.get("workspace_work_batch_id")
    assert batch_id
    for row in approved:
        assert row.causation_id == review_id
        assert row.root_execution_id == batch_id
        assert row.actor_kind == "user"
        assert row.actor_id == "user_1"

    # The status flip to in_progress also produced execution_started facts
    # rooted at the SAME work batch (details applied before the transition).
    started = await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_STARTED)
    assert {row.source_id for row in started} == {t.id for t in tasks}
    for row in started:
        assert row.root_execution_id == batch_id
        assert row.causation_id == review_id


async def test_reject_proposal_emits_item_rejected_with_reason(db_session):
    from packages.core.strategist import service as strategist_service

    ws = _mk_workspace(db_session)
    review_id = "rv_ledger_reject"
    task = _mk_task(db_session, ws, status="proposed", details={"strategist_review_id": review_id})
    await db_session.commit()

    cancelled = await strategist_service.reject_proposal(
        db_session, entity_id=ws.entity_id, review_id=review_id,
        reason="not now", actor_id="user_2",
    )
    assert cancelled == [task.id]

    [rejected] = await _events(db_session, entity_id=ws.entity_id, event_type=et.PROPOSAL_ITEM_REJECTED)
    assert rejected.source_id == task.id
    assert rejected.causation_id == review_id
    assert rejected.payload["rejection_reason"] == "not now"
    assert rejected.actor_id == "user_2"
    # proposed → cancelled is also an execution fact.
    [cancelled_event] = await _events(db_session, entity_id=ws.entity_id, event_type=et.EXECUTION_CANCELLED)
    assert cancelled_event.source_id == task.id


# ── Automation period key (pure mapping) ───────────────────────────

def test_automation_period_key_daily_vs_hourly():
    from packages.core.ledger.adapters import automation_period_key

    now = datetime(2026, 7, 24, 15, 30, tzinfo=timezone.utc)
    hourly_job = SimpleNamespace(schedule_kind="every", every_seconds=3600)
    daily_job = SimpleNamespace(schedule_kind="every", every_seconds=86400)
    cron_job = SimpleNamespace(schedule_kind="cron", every_seconds=None)

    assert automation_period_key(hourly_job, now) == "2026-07-24T15"
    assert automation_period_key(daily_job, now) == "2026-07-24"
    assert automation_period_key(cron_job, now) == "2026-07-24"


# ── Failure safety ─────────────────────────────────────────────────

async def test_ledger_failure_never_breaks_the_transition(db_session, monkeypatch):
    ws = _mk_workspace(db_session)
    task = _mk_task(db_session, ws)
    await db_session.flush()

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("packages.core.ledger.adapters.record_event", _boom)

    transition = await apply_task_status_transition(task, "in_progress", db=db_session)

    assert transition.new_status == "in_progress"
    assert task.status == "in_progress"
    assert task.started_at is not None
    assert await _events(db_session, entity_id=ws.entity_id) == []
    # The session is still usable after the swallowed failure.
    await db_session.flush()
