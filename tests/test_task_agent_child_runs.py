"""Delegated child runs of a Task agent loop: storage, delivery, recovery.

Nothing produces child rows yet, so every test seeds them: a Task, its
agent-loop root run parked ``waiting_child`` on one ``delegate_to_agent`` call,
and the call's child rows. The durable-run protocol itself runs for the child
spec in ``test_durable_run_protocol.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent, RuntimeRun
from packages.core.models.task import Task
from packages.core.models.task_agent_child import (
    TASK_AGENT_CHILD_LIVE_PHASES,
    TASK_AGENT_CHILD_MAX_DEPTH,
    TASK_AGENT_CHILD_PHASES,
    TaskAgentChildRun,
)
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.models.task_run_journal import TaskRunJournal
from packages.core.queues import CeleryQueue, queue_for_task
from packages.core.services import durable_run
from packages.core.services import task_agent_child_wakeup as children
from packages.core.services.task_agent_child_wakeup import (
    TASK_AGENT_CHILD_AWAITING_ACK,
    TASK_AGENT_CHILD_AWAITING_HEAVY_ACK,
    TASK_AGENT_CHILD_CLOSED_PREFIX,
    TASK_AGENT_CHILD_OUTBOX_EVENT,
    TASK_AGENT_CHILD_CLOSED_CARD_PHASES,
    TASK_AGENT_CHILD_PERSON_PHASES,
    TASK_AGENT_CHILD_RECOVERY_PHASES,
    TASK_AGENT_CHILD_RUN_SPEC,
    TASK_AGENT_CHILD_RUN_TASK,
    TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED,
    WAITING_CHILD_WAIT_KIND,
    stage_task_agent_child_wake,
    task_agent_child_delivery_state,
    task_agent_child_outbox_dedupe_key,
)
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.tasks import runtime_tasks
from packages.core.workers import task_agent_child_loop as child_worker
from packages.core.workers import task_run_host


_LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)
# Every Task this module seeds; the teardown removes them with their runs.
_SEEDED_TASK_IDS: list[str] = []
# The root-run sweep reads the oldest rows first; a root it must see sorts
# ahead of every other test's.
_OLDEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
P = TaskCoordinatorPhase


# ── seeding ─────────────────────────────────────────────────────────────


def _waiting(call_id: str, child_ids: list[str], *, resolution=None) -> dict:
    return {"task_loop_wait": {
        "version": 1, "kind": WAITING_CHILD_WAIT_KIND, "tool_call_id": call_id,
        "tool_name": "delegate_to_agent", "child_ids": child_ids,
        "parked_at": "2026-10-08T00:00:00+00:00", "resolution": resolution,
    }}


def _child(task: Task, root: TaskCoordinatorRun, *, index: int, batch: int, phase: str,
           parent: TaskAgentChildRun | None = None, call_id: str = "call-1") -> TaskAgentChildRun:
    return TaskAgentChildRun(
        id=generate_ulid(),
        task_id=task.id,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        root_run_id=root.id,
        parent_ref=parent.id if parent is not None else root.id,
        parent_child_id=parent.id if parent is not None else None,
        parent_tool_call_id=call_id,
        item_index=index,
        batch_size=batch,
        depth=(parent.depth + 1) if parent is not None else 1,
        agent_subscription_id=generate_ulid(),
        agent_id=generate_ulid(),
        service_key="writer",
        agent_label=f"Writer {index}",
        prompt="Draft the launch note.",
        answer_max_chars=4000,
        phase=phase,
        lifecycle_generation=root.lifecycle_generation,
        state_version=0,
        updated_at=_LONG_AGO,
    )


async def _tree(
    db,
    *,
    count: int = 1,
    child_phase: str = P.READY.value,
    root_phase: str = P.WAITING_CHILD.value,
    engine: str = TaskCoordinatorEngine.AGENT_LOOP.value,
    commit: bool = True,
    root_updated_at: datetime = _LONG_AGO,
) -> tuple[Task, TaskCoordinatorRun, list[TaskAgentChildRun]]:
    task = Task(
        id=generate_ulid(), entity_id=generate_ulid(), workspace_id=generate_ulid(),
        title="Delegate the launch note", status="in_progress", task_type="general",
        details={}, delegate_service_keys=[], status_changed_at=datetime.now(timezone.utc),
    )
    root = TaskCoordinatorRun(
        id=generate_ulid(), task_id=task.id, entity_id=task.entity_id,
        workspace_id=task.workspace_id, root_subscription_id=generate_ulid(),
        root_agent_id=generate_ulid(), selection_fingerprint="child-runs",
        lifecycle_generation=task_execution_intent_generation(task),
        engine=engine, phase=root_phase, state_version=0, updated_at=root_updated_at,
    )
    kids = [_child(task, root, index=index, batch=count, phase=child_phase) for index in range(count)]
    root.checkpoint = _waiting("call-1", [kid.id for kid in kids])
    _SEEDED_TASK_IDS.append(task.id)
    db.add_all([task, root, *kids])
    await db.flush()
    if commit:
        await db.commit()
    return task, root, kids


async def _fresh(db, model, row_id):
    return (await db.execute(
        select(model).where(model.id == row_id).execution_options(populate_existing=True)
    )).scalar_one()


async def _event(db, child_id: str) -> RuntimeOutboxEvent | None:
    return (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == task_agent_child_outbox_dedupe_key(child_id),
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _journal(db, run_id: str) -> list[TaskRunJournal]:
    return list((await db.execute(
        select(TaskRunJournal).where(TaskRunJournal.run_id == run_id)
        .order_by(TaskRunJournal.seq).execution_options(populate_existing=True)
    )).scalars().all())


@pytest.fixture
def worker_sessions(db_session, monkeypatch):
    from packages.core import database

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    return session_factory


@pytest.fixture(autouse=True)
async def _only_this_tests_children(db_session):
    # Child rows exist only in these tests; sweeps scan the whole table.
    await db_session.execute(delete(TaskAgentChildRun))
    await db_session.commit()
    yield
    # Leave no in-progress Task with a waiting agent-loop root behind: other
    # modules' global sweeps would find it (its children are gone).
    seeded = list(_SEEDED_TASK_IDS)
    _SEEDED_TASK_IDS.clear()
    if not seeded:
        return
    # The test's own open transaction may hold row locks on them: end it first.
    await db_session.rollback()
    await db_session.execute(delete(TaskAgentChildRun).where(TaskAgentChildRun.task_id.in_(seeded)))
    await db_session.execute(delete(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id.in_(seeded)))
    await db_session.execute(delete(Task).where(Task.id.in_(seeded)))
    await db_session.commit()


# ── model, spec, constants ──────────────────────────────────────────────


def test_child_depth_matches_the_chat_delegation_limit():
    from packages.core.ai.runtime.workspace_delegation_actions import MAX_DELEGATION_DEPTH

    assert TASK_AGENT_CHILD_MAX_DEPTH == MAX_DELEGATION_DEPTH


def test_child_phases_are_the_task_run_phases_without_waiting_plan():
    assert TASK_AGENT_CHILD_PHASES == {phase.value for phase in P} - {P.WAITING_PLAN.value}
    assert TASK_AGENT_CHILD_RUN_SPEC.phases == TASK_AGENT_CHILD_PHASES
    assert TASK_AGENT_CHILD_RUN_SPEC.terminal_phases == P.terminal()


def test_child_events_are_declared_on_control_and_heavy():
    assert queue_for_task(TASK_AGENT_CHILD_OUTBOX_EVENT) is CeleryQueue.CONTROL
    assert queue_for_task(TASK_AGENT_CHILD_RUN_TASK) is CeleryQueue.HEAVY
    # The child is an agent loop: the same limits as the root loop.
    assert runtime_tasks.run_task_agent_child_task.time_limit == runtime_tasks.run_task_agent_loop_task.time_limit
    assert (
        runtime_tasks.run_task_agent_child_task.soft_time_limit
        == runtime_tasks.run_task_agent_loop_task.soft_time_limit
    )
    assert runtime_tasks.run_task_agent_child_task.max_retries == 0


def test_running_cap_defaults_to_four(monkeypatch):
    monkeypatch.delenv(children.TASK_AGENT_CHILD_MAX_RUNNING_ENV, raising=False)
    assert children.task_agent_child_max_running() == 4
    monkeypatch.setenv(children.TASK_AGENT_CHILD_MAX_RUNNING_ENV, "2")
    assert children.task_agent_child_max_running() == 2
    for bad in ("0", "-3", "many"):
        monkeypatch.setenv(children.TASK_AGENT_CHILD_MAX_RUNNING_ENV, bad)
        assert children.task_agent_child_max_running() == 4


@pytest.mark.asyncio
async def test_a_child_wake_names_its_task_and_child(db_session):
    task, _root, (kid,) = await _tree(db_session)
    wake = await stage_task_agent_child_wake(db_session, child_id=kid.id, reason="test")
    event = await _event(db_session, kid.id)
    assert event.event_type == TASK_AGENT_CHILD_OUTBOX_EVENT
    assert event.aggregate_id == kid.id
    assert event.payload == {
        "version": 1, "task_id": task.id, "child_id": kid.id,
        "wake_generation": wake.wake_generation, "reason": "test",
    }


# ── constraints ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_row_per_call_item(db_session):
    task, root, (kid,) = await _tree(db_session)
    again = _child(task, root, index=0, batch=1, phase=P.READY.value)
    again.agent_subscription_id = generate_ulid()
    db_session.add(again)
    with pytest.raises(IntegrityError, match="uq_task_agent_child_runs_call"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_one_live_run_per_agent_per_task(db_session):
    task, root, (kid,) = await _tree(db_session)
    task_id, root_id, kid_id = task.id, root.id, kid.id
    second = _child(task, root, index=0, batch=1, phase=P.READY.value, call_id="call-2")
    second.agent_subscription_id = kid.agent_subscription_id
    db_session.add(second)
    with pytest.raises(IntegrityError, match="uq_task_agent_child_runs_live_agent"):
        await db_session.flush()
    await db_session.rollback()

    # Once the first run ended, the same agent may run again in this Task,
    task = await _fresh(db_session, Task, task_id)
    root = await _fresh(db_session, TaskCoordinatorRun, root_id)
    kid = await _fresh(db_session, TaskAgentChildRun, kid_id)
    kid.phase = P.COMPLETED.value
    await db_session.commit()
    second = _child(task, root, index=0, batch=1, phase=P.READY.value, call_id="call-2")
    second.agent_subscription_id = kid.agent_subscription_id
    db_session.add(second)
    await db_session.flush()
    # and in another Task at the same time.
    other_task, other_root, _ = await _tree(db_session, commit=False)
    elsewhere = _child(other_task, other_root, index=0, batch=1, phase=P.READY.value, call_id="call-9")
    elsewhere.agent_subscription_id = kid.agent_subscription_id
    db_session.add(elsewhere)
    await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "constraint"), [
    (dict(item_index=1, batch_size=1), "ck_task_agent_child_runs_item"),
    (dict(depth=3, parent_child_id="01JCHILDPARENT000000000000", parent_ref="01JCHILDPARENT000000000000"),
     "ck_task_agent_child_runs_depth"),
    (dict(phase=P.WAITING_PLAN.value), "ck_task_agent_child_runs_phase"),
    (dict(state_version=-1), "ck_task_agent_child_runs_version"),
    (dict(claim_token="t"), "ck_task_agent_child_runs_claim"),
    # A depth-1 child names the root as its parent; a deeper one a child row.
    (dict(parent_child_id="01JCHILDPARENT000000000000"), "ck_task_agent_child_runs_parent"),
    (dict(depth=2), "ck_task_agent_child_runs_parent"),
    (dict(parent_ref="01JNOTTHEROOT0000000000000"), "ck_task_agent_child_runs_parent"),
])
async def test_check_constraints(db_session, change, constraint):
    task, root, _ = await _tree(db_session, count=0)
    kid = _child(task, root, index=0, batch=1, phase=P.READY.value)
    for key, value in change.items():
        setattr(kid, key, value)
    db_session.add(kid)
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_a_depth_two_child_points_at_its_parent_child(db_session):
    task, root, (parent,) = await _tree(db_session, child_phase=P.WAITING_CHILD.value)
    grandchild = _child(task, root, index=0, batch=1, phase=P.READY.value, parent=parent, call_id="call-2")
    db_session.add(grandchild)
    await db_session.flush()
    assert (grandchild.depth, grandchild.parent_ref, grandchild.parent_child_id) == (2, parent.id, parent.id)
    await db_session.rollback()


# ── delivery state ──────────────────────────────────────────────────────


async def _staged(db, kid) -> RuntimeOutboxEvent:
    await stage_task_agent_child_wake(db, child_id=kid.id, reason="test")
    await db.commit()
    return await _event(db, kid.id)


@pytest.mark.asyncio
async def test_delivery_state_admits_an_awaited_child(db_session):
    _task, _root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)
    child, state = await task_agent_child_delivery_state(db_session, event)
    assert (child.id, state) == (kid.id, "ready")


@pytest.mark.asyncio
@pytest.mark.parametrize("break_it", [
    "terminal_child", "inactive_task", "stale_lifecycle", "plan_dag_root", "terminal_root",
    "parent_not_waiting", "other_call", "not_listed", "settled_batch", "stale_generation",
    "foreign_task",
])
async def test_delivery_state_refuses(db_session, break_it):
    task, root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)
    task = await _fresh(db_session, Task, task.id)
    root = await _fresh(db_session, TaskCoordinatorRun, root.id)
    kid = await _fresh(db_session, TaskAgentChildRun, kid.id)
    expected = {
        "terminal_child": "terminal_run", "inactive_task": "inactive_task",
        "stale_lifecycle": "stale_lifecycle", "plan_dag_root": "root_unavailable",
        "terminal_root": "root_unavailable", "parent_not_waiting": "parent_not_waiting",
        "other_call": "parent_not_waiting", "not_listed": "parent_not_waiting",
        "settled_batch": "parent_not_waiting", "stale_generation": "stale_generation",
        "foreign_task": "missing_run",
    }[break_it]
    if break_it == "terminal_child":
        kid.phase = P.CANCELLED.value
    elif break_it == "inactive_task":
        task.status = "on_hold"
    elif break_it == "stale_lifecycle":
        kid.lifecycle_generation = "an-older-lifecycle"
    elif break_it == "plan_dag_root":
        root.engine = TaskCoordinatorEngine.PLAN_DAG.value
    elif break_it == "terminal_root":
        root.phase = P.FAILED.value
    elif break_it == "parent_not_waiting":
        root.phase = P.RUNNING.value
    elif break_it == "other_call":
        root.checkpoint = _waiting("call-other", [kid.id])
    elif break_it == "not_listed":
        root.checkpoint = _waiting("call-1", [generate_ulid()])
    elif break_it == "settled_batch":
        root.checkpoint = _waiting("call-1", [kid.id], resolution="children_settled")
    elif break_it == "stale_generation":
        event.payload = {**event.payload, "wake_generation": generate_ulid()}
    elif break_it == "foreign_task":
        event.payload = {**event.payload, "task_id": generate_ulid()}
    await db_session.commit()
    child, state = await task_agent_child_delivery_state(db_session, event)
    assert (child, state) == (None, expected)


# ── publisher and CONTROL consumer ──────────────────────────────────────


@pytest.mark.asyncio
async def test_publisher_sends_a_child_wake_and_keeps_it_until_acked(db_session, worker_sessions, monkeypatch):
    # The global publisher first recovers expired runs, which can stage a
    # terminal notice for a run an earlier test left: reset both the queue
    # and its recovery source before counting what it publishes.
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.execute(delete(RuntimeRun))
    await db_session.commit()
    task, _root, (kid, closed_kid) = await _tree(db_session, count=2)
    await stage_task_agent_child_wake(db_session, child_id=kid.id, reason="start")
    await stage_task_agent_child_wake(db_session, child_id=closed_kid.id, reason="start")
    closed_kid = await _fresh(db_session, TaskAgentChildRun, closed_kid.id)
    closed_kid.phase = P.CANCELLED.value
    await db_session.commit()
    sent = []
    monkeypatch.setattr(runtime_tasks.celery_app, "send_task", lambda name, **kwargs: sent.append((name, kwargs)))

    assert await runtime_tasks.dispatch_runtime_outbox_once() == 1

    assert [name for name, _ in sent] == [TASK_AGENT_CHILD_OUTBOX_EVENT]
    assert sent[0][1]["queue"] == CeleryQueue.CONTROL.value
    event = await _event(db_session, kid.id)
    assert sent[0][1]["kwargs"] == {
        "event_id": event.id, "task_id": task.id, "child_id": kid.id,
        "wake_generation": event.payload["wake_generation"], "reason": "start",
    }
    assert event.delivered_at is None
    assert event.last_error == TASK_AGENT_CHILD_AWAITING_ACK
    # A wake its child can no longer use is closed, never sent.
    closed = await _event(db_session, closed_kid.id)
    assert closed.delivered_at is not None
    assert closed.last_error == f"{TASK_AGENT_CHILD_CLOSED_PREFIX}:terminal_run"


async def _consume(event: RuntimeOutboxEvent, **overrides) -> dict:
    kwargs = {
        "event_id": event.id, "task_id": event.payload["task_id"],
        "child_id": event.payload["child_id"],
        "wake_generation": event.payload["wake_generation"], "reason": "test",
    }
    kwargs.update(overrides)
    return await runtime_tasks.consume_task_agent_child_intent_once(**kwargs)


@pytest.mark.asyncio
async def test_consumer_hands_the_exact_wake_to_heavy(db_session, worker_sessions, monkeypatch):
    _task, _root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)
    handed = []
    monkeypatch.setattr(
        runtime_tasks.run_task_agent_child_task, "apply_async",
        lambda **kwargs: handed.append(kwargs),
    )
    assert await _consume(event, wake_generation=generate_ulid()) == {
        "status": "closed", "reason": "stale_generation",
    }
    assert await _consume(event) == {"status": "dispatched", "child_id": kid.id}
    assert handed == [{"kwargs": {
        "event_id": event.id, "task_id": kid.task_id, "child_id": kid.id,
        "wake_generation": event.payload["wake_generation"], "reason": "test",
    }}]
    event = await _event(db_session, kid.id)
    assert event.delivered_at is None
    assert event.last_error == TASK_AGENT_CHILD_AWAITING_HEAVY_ACK
    # Redelivered before its ACK window: suppressed, not handed twice.
    assert await _consume(event) == {"status": "suppressed", "reason": "not_due"}
    assert len(handed) == 1


@pytest.mark.asyncio
async def test_consumer_closes_a_wake_of_a_child_nobody_awaits(db_session, worker_sessions, monkeypatch):
    _task, root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)
    root = await _fresh(db_session, TaskCoordinatorRun, root.id)
    root.phase = P.RUNNING.value
    await db_session.commit()
    monkeypatch.setattr(
        runtime_tasks.run_task_agent_child_task, "apply_async",
        lambda **_kwargs: pytest.fail("a refused wake is never handed to HEAVY"),
    )
    assert await _consume(event) == {"status": "closed", "reason": "parent_not_waiting"}
    event = await _event(db_session, kid.id)
    assert event.delivered_at is not None
    assert event.last_error == f"{TASK_AGENT_CHILD_CLOSED_PREFIX}:parent_not_waiting"


@pytest.mark.asyncio
async def test_consumer_defers_on_a_broker_failure(db_session, worker_sessions, monkeypatch):
    _task, _root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)

    def down(**_kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(runtime_tasks.run_task_agent_child_task, "apply_async", down)
    assert await _consume(event) == {"status": "deferred", "reason": "broker_failure"}
    event = await _event(db_session, kid.id)
    assert (event.delivered_at, event.attempt_count, event.last_error) == (None, 1, "broker down")


# ── HEAVY worker: admission and defer ───────────────────────────────────


@pytest.fixture
def admitted_workspace(monkeypatch):
    monkeypatch.setattr(
        task_run_host, "lock_workspace_access_boundary",
        AsyncMock(return_value=SimpleNamespace(status="active")),
    )
    monkeypatch.setattr(task_run_host, "workspace_allows_manual_work", lambda _workspace: True)
    monkeypatch.setattr(task_run_host, "evaluate_workspace_blocking_setup", AsyncMock(return_value=None))


async def _run_child(event: RuntimeOutboxEvent) -> dict:
    return await child_worker.run_task_agent_child_once(
        event_id=event.id, task_id=event.payload["task_id"], child_id=event.payload["child_id"],
        wake_generation=event.payload["wake_generation"], reason="test",
    )


@pytest.fixture
def child_loop_runs(monkeypatch):
    """Admission and the claim are real; the child loop itself is recorded."""

    ran: list[dict] = []

    async def run(claim, child, **_kwargs):
        ran.append({"claim": claim, "child_id": child.id})
        return {"status": "ran", "child_id": child.id}

    monkeypatch.setattr(child_worker, "_run_child", run)
    return ran


@pytest.mark.asyncio
async def test_worker_admits_claims_as_its_last_step_and_runs_the_child(
    db_session, worker_sessions, admitted_workspace, child_loop_runs,
):
    _task, _root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)

    assert await _run_child(event) == {"status": "ran", "child_id": kid.id}

    (ran,) = child_loop_runs
    assert ran["claim"].key == kid.id and ran["claim"].wake_generation == event.payload["wake_generation"]
    kid = await _fresh(db_session, TaskAgentChildRun, kid.id)
    # The wake ended without settling: the claim is released, the wake stays
    # for redelivery.
    assert kid.claim_token is None and kid.phase == P.READY.value
    assert (await _event(db_session, kid.id)).delivered_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("break_it", "reason"), [
    ("workspace", "workspace_unavailable"),
    ("autonomy", "workspace_autonomy_paused"),
    ("setup", "workspace_setup_incomplete"),
    ("task", "task_inactive"),
    ("parent", "parent_not_waiting"),
    ("generation", "stale_generation"),
    ("terminal", "terminal_run"),
])
async def test_worker_refusals_leave_the_row_alone(
    db_session, worker_sessions, admitted_workspace, monkeypatch, break_it, reason,
):
    task, root, (kid,) = await _tree(db_session)
    event = await _staged(db_session, kid)
    if break_it == "workspace":
        monkeypatch.setattr(task_run_host, "workspace_allows_manual_work", lambda _workspace: False)
    elif break_it == "autonomy":
        _pause_workspace(monkeypatch)
        (await _fresh(db_session, Task, task.id)).details = {"strategist_review_id": "review-1"}
    elif break_it == "setup":
        monkeypatch.setattr(
            task_run_host, "evaluate_workspace_blocking_setup",
            AsyncMock(return_value=SimpleNamespace(blocks_work=True)),
        )
    elif break_it == "task":
        (await _fresh(db_session, Task, task.id)).status = "on_hold"
    elif break_it == "parent":
        (await _fresh(db_session, TaskCoordinatorRun, root.id)).phase = P.RUNNING.value
    elif break_it == "generation":
        await stage_task_agent_child_wake(db_session, child_id=kid.id, reason="newer")
    elif break_it == "terminal":
        (await _fresh(db_session, TaskAgentChildRun, kid.id)).phase = P.FAILED.value
    await db_session.commit()
    assert await _run_child(event) == {"status": "deferred", "reason": reason}
    assert (await _fresh(db_session, TaskAgentChildRun, kid.id)).claim_token is None


def _pause_workspace(monkeypatch) -> None:
    monkeypatch.setattr(
        task_run_host, "lock_workspace_access_boundary",
        AsyncMock(return_value=SimpleNamespace(status="paused")),
    )


@pytest.mark.asyncio
async def test_paused_workspace_still_runs_a_user_directed_task(
    db_session, worker_sessions, admitted_workspace, monkeypatch, child_loop_runs,
):
    task, _root, (kid,) = await _tree(db_session)
    _pause_workspace(monkeypatch)
    (await _fresh(db_session, Task, task.id)).details = {
        "_manual_execution": {"user_id": "user-1", "action": "start"},
    }
    await db_session.commit()
    event = await _staged(db_session, kid)

    assert await _run_child(event) == {"status": "ran", "child_id": kid.id}


@pytest.mark.asyncio
async def test_worker_defers_its_own_wake_at_the_running_cap(
    db_session, worker_sessions, admitted_workspace, monkeypatch, child_loop_runs,
):
    monkeypatch.setenv(children.TASK_AGENT_CHILD_MAX_RUNNING_ENV, "1")
    _task, _root, (busy, kid) = await _tree(db_session, count=2)
    busy_wake = await stage_task_agent_child_wake(db_session, child_id=busy.id, reason="start")
    claim, state = await durable_run.claim_run_wake(
        db_session, TASK_AGENT_CHILD_RUN_SPEC, key=busy.id, event_id=busy_wake.event_id,
        wake_generation=busy_wake.wake_generation, claim_token=generate_ulid(), lease_seconds=120,
    )
    assert state == "claimed"
    event = await _staged(db_session, kid)

    started = datetime.now(timezone.utc)
    assert await _run_child(event) == {"status": "deferred", "reason": child_worker.RUNNING_CAP}

    deferred = await _event(db_session, kid.id)
    assert deferred.delivered_at is None
    assert deferred.payload["wake_generation"] == event.payload["wake_generation"]
    assert deferred.last_error.endswith(child_worker.RUNNING_CAP)
    assert deferred.available_at >= started + timedelta(
        seconds=children.TASK_AGENT_CHILD_RUNNING_CAP_DEFER_SECONDS - 1,
    )
    # Not a failure: the child itself is untouched.
    assert (await _fresh(db_session, TaskAgentChildRun, kid.id)).checkpoint is None

    # Once the sibling's claim is gone the child is admitted again.
    (await _fresh(db_session, TaskAgentChildRun, busy.id)).phase = P.COMPLETED.value
    await durable_run.release_run_claim(
        db_session, TASK_AGENT_CHILD_RUN_SPEC, key=busy.id,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
    )
    await db_session.commit()
    assert (await _run_child(event))["status"] == "ran"


# ── recovery: stalled child wakes and phase coverage ────────────────────


def _phase_coverage_gaps(recovery: frozenset[str], person: frozenset[str]) -> set[str]:
    """Live child phases nothing moves forward, and phases claimed twice."""

    return (set(TASK_AGENT_CHILD_LIVE_PHASES) - (recovery | person)) | (recovery & person)


def test_every_live_child_phase_is_moved_forward_by_something():
    assert _phase_coverage_gaps(TASK_AGENT_CHILD_RECOVERY_PHASES, TASK_AGENT_CHILD_PERSON_PHASES) == set()
    assert (TASK_AGENT_CHILD_RECOVERY_PHASES | TASK_AGENT_CHILD_PERSON_PHASES) <= TASK_AGENT_CHILD_PHASES


async def _person_reopens(db, monkeypatch, phase: str) -> bool:
    """Park one child in ``phase`` on its own question; does the real answer path reopen it?

    The answer goes through ``apply_task_input_answer``, the entry point every
    answer surface (card, Task page, comment, answer_task_blocker) funnels
    through. Reopened: the child is ready with a fresh wake of its own, its
    wake journaled, and its parent still waits on it.
    """

    from packages.core.services import runtime_authorization
    from packages.core.services import task_agent_loop_hitl as loop_hitl
    from packages.core.services.task_chat_hitl import ensure_task_input_hitl
    from packages.core.services.task_input_resume import apply_task_input_answer

    # Seeded ids have no member rows; who may answer is not under test here.
    monkeypatch.setattr(
        runtime_authorization, "authorize_hitl_resolution",
        AsyncMock(return_value=SimpleNamespace(allowed=True)),
    )
    task, root, (kid,) = await _tree(db, child_phase=phase, commit=False)
    card = await ensure_task_input_hitl(
        db, task, question={"question": "Which tone?"},
        dedup_key=f"task-input:{kid.id}:ask", asked_by=kid.agent_label, run_id=kid.id,
    )
    request_id = card.pending_action["approval_request_id"]
    parked = {"pending_tool_call": {"id": "ask", "name": "request_input", "arguments": {}}, "messages": []}
    kid.checkpoint = {
        "runtime_checkpoint": parked,
        "task_loop_wait": loop_hitl.human_wait(
            kind=loop_hitl.INPUT_WAIT_KIND, tool_call_id="ask", tool_name="request_input",
            hitl_request_id=request_id, checkpoint_digest=loop_hitl.runtime_checkpoint_digest(parked),
            summary="Which tone?", run_kind=loop_hitl.RUN_KIND_CHILD, agent_label=kid.agent_label,
        ),
    }
    await db.commit()
    applied = await apply_task_input_answer(
        db, task=await _fresh(db, Task, task.id), user_id="user-1", user_label="Ana",
        response_text="Warm.", payload={"response": "Warm."}, request_id=request_id,
    )
    await db.commit()
    kid = await _fresh(db, TaskAgentChildRun, kid.id)
    event = await _event(db, kid.id)
    reopened = bool(
        applied["resumed"]
        and kid.phase == P.READY.value
        and event is not None and event.delivered_at is None
        and event.payload["wake_generation"] == kid.wake_generation
        and [row.kind for row in await _journal(db, kid.id)][-1:] == ["wake"]
        and (await _fresh(db, TaskCoordinatorRun, root.id)).phase == P.WAITING_CHILD.value
    )
    if reopened:
        pending = kid.checkpoint["runtime_checkpoint"]["pending_tool_result"]
        reopened = pending["id"] == "ask" and "Warm." in pending["content"]
    return reopened


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", sorted(TASK_AGENT_CHILD_PERSON_PHASES))
async def test_a_person_reopens_a_child_waiting_on_them(db_session, monkeypatch, phase):
    # The person half of the coverage guard above, through the real answer
    # entry point: every phase declared "a person moves it" is moved by one.
    assert await _person_reopens(db_session, monkeypatch, phase)


@pytest.mark.asyncio
async def test_the_person_guard_catches_an_answer_that_no_longer_reaches_children(db_session, monkeypatch):
    from packages.core.services import task_agent_loop_hitl as loop_hitl

    # Mutation: the answer path stops seeing children's parks.
    monkeypatch.setattr(loop_hitl, "parked_child_human_wait", lambda *_args, **_kwargs: None)
    (phase,) = sorted(TASK_AGENT_CHILD_PERSON_PHASES)
    assert not await _person_reopens(db_session, monkeypatch, phase)


def test_the_sweep_wakes_a_person_phase_only_once_its_card_closed():
    # Every person phase has the closed-card backstop, and only that one:
    # the sweep never polls a person phase whose card is still open.
    assert TASK_AGENT_CHILD_CLOSED_CARD_PHASES == TASK_AGENT_CHILD_PERSON_PHASES
    # The root run gets the same backstop for the same (person) phase.
    assert runtime_tasks.TASK_AGENT_ROOT_CLOSED_CARD_PHASES == TASK_AGENT_CHILD_PERSON_PHASES
    assert not (TASK_AGENT_CHILD_CLOSED_CARD_PHASES & TASK_AGENT_CHILD_RECOVERY_PHASES)


async def _closed_card_reopens(db, phase: str) -> bool:
    """Park one child in ``phase`` on its question, close the card without the
    close-time wake, run the recovery sweep: is the child reopened?"""

    from packages.core.services import task_agent_loop_hitl as loop_hitl
    from packages.core.services.task_chat_hitl import ensure_task_input_hitl

    task, root, (kid,) = await _tree(db, child_phase=phase, commit=False)
    card = await ensure_task_input_hitl(
        db, task, question={"question": "Which tone?"},
        dedup_key=f"task-input:{kid.id}:ask", asked_by=kid.agent_label, run_id=kid.id,
    )
    request_id = card.pending_action["approval_request_id"]
    parked = {"pending_tool_call": {"id": "ask", "name": "request_input", "arguments": {}}, "messages": []}
    kid.checkpoint = {
        "runtime_checkpoint": parked,
        "task_loop_wait": loop_hitl.human_wait(
            kind=loop_hitl.INPUT_WAIT_KIND, tool_call_id="ask", tool_name="request_input",
            hitl_request_id=request_id, checkpoint_digest=loop_hitl.runtime_checkpoint_digest(parked),
            summary="Which tone?", run_kind=loop_hitl.RUN_KIND_CHILD, agent_label=kid.agent_label,
        ),
    }
    await db.commit()
    # Still open: the sweep leaves it alone.
    await runtime_tasks.recover_runs_on_closed_cards_once()
    if (await _fresh(db, TaskAgentChildRun, kid.id)).phase != phase:
        return False
    request = await _fresh(db, HitlRequest, request_id)
    request.status, request.resolved_reason = "expired", "lost_close"
    await db.commit()
    await runtime_tasks.recover_runs_on_closed_cards_once()
    kid = await _fresh(db, TaskAgentChildRun, kid.id)
    event = await _event(db, kid.id)
    return bool(
        kid.phase == P.READY.value
        and event is not None and event.delivered_at is None
        and kid.checkpoint["task_loop_wait"]["resolution"] == loop_hitl.CLOSED_RESOLUTION
        and kid.checkpoint["runtime_checkpoint"]["pending_tool_result"]["id"] == "ask"
        and (await _fresh(db, TaskCoordinatorRun, root.id)).phase == P.WAITING_CHILD.value
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", sorted(TASK_AGENT_CHILD_CLOSED_CARD_PHASES))
async def test_the_sweep_reopens_a_child_whose_card_closed_undecided(db_session, worker_sessions, phase):
    assert await _closed_card_reopens(db_session, phase)


@pytest.mark.asyncio
async def test_the_closed_card_guard_catches_a_sweep_that_skips_the_phase(db_session, worker_sessions, monkeypatch):
    # Mutation: the sweep's closed-card branch drops the person phase.
    monkeypatch.setattr(runtime_tasks, "TASK_AGENT_CHILD_CLOSED_CARD_PHASES", frozenset())
    (phase,) = sorted(TASK_AGENT_CHILD_CLOSED_CARD_PHASES)
    assert not await _closed_card_reopens(db_session, phase)


@pytest.mark.parametrize("phase", sorted(TASK_AGENT_CHILD_LIVE_PHASES))
def test_the_phase_coverage_guard_catches_a_dropped_phase(phase):
    assert _phase_coverage_gaps(
        TASK_AGENT_CHILD_RECOVERY_PHASES - {phase}, TASK_AGENT_CHILD_PERSON_PHASES - {phase},
    ) == {phase}


def _parked(phase: str) -> dict | None:
    if phase == P.WAITING_TIMER.value:
        return {"task_loop_wait": {"version": 1, "kind": "waiting_timer", "tool_call_id": "t",
                                   "wake_at": "2000-01-01T00:00:00+00:00"}}
    if phase == P.WAITING_JOB.value:
        return {"task_loop_wait": {"version": 1, "kind": "waiting_job", "tool_call_id": "j",
                                   "job_ids": ["job-gone"], "deadline_at": "2000-01-01T00:00:00+00:00"}}
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", sorted(TASK_AGENT_CHILD_LIVE_PHASES))
async def test_the_sweep_restores_a_lost_wake_in_every_recovery_phase(db_session, worker_sessions, phase):
    task, root, (kid,) = await _tree(db_session, child_phase=phase, commit=False)
    if phase == P.WAITING_CHILD.value:
        # Its own delegation has ended: every grandchild is terminal.
        grandchild = _child(task, root, index=0, batch=1, phase=P.COMPLETED.value, parent=kid, call_id="call-2")
        kid.checkpoint = _waiting("call-2", [grandchild.id])
        db_session.add(grandchild)
    else:
        kid.checkpoint = _parked(phase)
    await db_session.commit()

    await runtime_tasks.recover_stalled_task_agent_child_wakes_once()

    event = await _event(db_session, kid.id)
    rows = await _journal(db_session, kid.id)
    if phase in TASK_AGENT_CHILD_PERSON_PHASES:
        # A child waiting on a person is never woken by the sweep.
        assert event is None and rows == []
        return
    assert phase in TASK_AGENT_CHILD_RECOVERY_PHASES
    assert event is not None and event.delivered_at is None
    assert event.payload["wake_generation"] == (await _fresh(db_session, TaskAgentChildRun, kid.id)).wake_generation
    assert event.available_at <= datetime.now(timezone.utc)
    assert [(row.kind, row.run_kind, row.payload["resolution"]) for row in rows] == [
        ("wake", "child", "recovered"),
    ]
    assert (rows[0].root_run_id, rows[0].parent_run_id, rows[0].depth) == (root.id, root.id, 1)


@pytest.mark.asyncio
async def test_the_sweep_wakes_a_timer_at_its_own_time(db_session, worker_sessions):
    _task, _root, (kid,) = await _tree(db_session, child_phase=P.WAITING_TIMER.value, commit=False)
    wake_at = datetime.now(timezone.utc) + timedelta(hours=3)
    kid.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_timer", "tool_call_id": "t",
                                         "wake_at": wake_at.isoformat()}}
    await db_session.commit()
    await runtime_tasks.recover_stalled_task_agent_child_wakes_once()
    event = await _event(db_session, kid.id)
    assert abs((event.available_at - wake_at).total_seconds()) < 1


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", [
    "parent_not_waiting", "parent_child_not_waiting", "stale_lifecycle", "inactive_task",
    "live_claim", "pending_wake", "grandchildren_running",
    # The parent is waiting_child, but not on this child: the same refusal
    # as delivery, so the sweep never restages a wake the publisher closes.
    "settled_batch", "other_call", "not_listed", "root_stopped_above_parent_child",
])
async def test_the_sweep_leaves_children_that_move_or_cannot_move(db_session, worker_sessions, skip):
    task, root, (kid,) = await _tree(db_session, commit=False)
    target = kid
    if skip == "parent_not_waiting":
        root.phase = P.RUNNING.value
    elif skip == "parent_child_not_waiting":
        target = _child(task, root, index=0, batch=1, phase=P.READY.value, parent=kid, call_id="call-2")
        kid.phase = P.RUNNING.value
        db_session.add(target)
    elif skip == "stale_lifecycle":
        kid.lifecycle_generation = "an-older-lifecycle"
    elif skip == "inactive_task":
        task.status = "on_hold"
    elif skip == "settled_batch":
        root.checkpoint = _waiting("call-1", [kid.id], resolution="children_settled")
    elif skip == "other_call":
        root.checkpoint = _waiting("call-other", [kid.id])
    elif skip == "not_listed":
        root.checkpoint = _waiting("call-1", [generate_ulid()])
    elif skip == "root_stopped_above_parent_child":
        # A depth-2 child whose own parent waits on it, under a stopped root.
        target = _child(task, root, index=0, batch=1, phase=P.READY.value, parent=kid, call_id="call-2")
        kid.phase = P.WAITING_CHILD.value
        kid.checkpoint = _waiting("call-2", [target.id])
        root.phase = P.FAILED.value
        db_session.add(target)
    elif skip == "grandchildren_running":
        kid.phase = P.WAITING_CHILD.value
        grandchild = _child(task, root, index=0, batch=1, phase=P.RUNNING.value, parent=kid, call_id="call-2")
        kid.checkpoint = _waiting("call-2", [grandchild.id])
        db_session.add(grandchild)
        await db_session.flush()
        # The grandchild itself is busy (a live claim): it moves on its own.
        wake = await stage_task_agent_child_wake(db_session, child_id=grandchild.id, reason="start")
        await durable_run.claim_run_wake(
            db_session, TASK_AGENT_CHILD_RUN_SPEC, key=grandchild.id, event_id=wake.event_id,
            wake_generation=wake.wake_generation, claim_token=generate_ulid(), lease_seconds=120,
        )
    await db_session.flush()
    before = None
    if skip in {"live_claim", "pending_wake"}:
        wake = await stage_task_agent_child_wake(db_session, child_id=kid.id, reason="start")
        before = wake.wake_generation
        if skip == "live_claim":
            await durable_run.claim_run_wake(
                db_session, TASK_AGENT_CHILD_RUN_SPEC, key=kid.id, event_id=wake.event_id,
                wake_generation=wake.wake_generation, claim_token=generate_ulid(), lease_seconds=120,
            )
            event = await _event(db_session, kid.id)
            event.delivered_at = datetime.now(timezone.utc)
    await db_session.commit()

    await runtime_tasks.recover_stalled_task_agent_child_wakes_once()

    target = await _fresh(db_session, TaskAgentChildRun, target.id)
    assert target.wake_generation == before
    assert await _journal(db_session, target.id) == []


@pytest.mark.asyncio
async def test_the_sweep_recovers_a_grandchild_under_a_waiting_child(db_session, worker_sessions):
    task, root, (kid,) = await _tree(db_session, child_phase=P.WAITING_CHILD.value, commit=False)
    grandchild = _child(task, root, index=0, batch=1, phase=P.READY.value, parent=kid, call_id="call-2")
    kid.checkpoint = _waiting("call-2", [grandchild.id])
    db_session.add(grandchild)
    await db_session.commit()

    await runtime_tasks.recover_stalled_task_agent_child_wakes_once()

    assert (await _event(db_session, grandchild.id)) is not None
    # The waiting parent child is not woken while its grandchild still runs.
    assert (await _event(db_session, kid.id)) is None
    rows = await _journal(db_session, grandchild.id)
    assert [(row.parent_run_id, row.depth) for row in rows] == [(kid.id, 2)]


# ── recovery: the root run's waiting_child branch, by engine ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("batch", "woken"), [
    ("running", False),
    ("all_terminal", True),
    ("settled", True),
    ("malformed", True),
    ("foreign_child", True),
    # A live child left on another lifecycle can never end the batch.
    ("stale_child", True),
])
async def test_an_agent_loop_root_waiting_on_children(db_session, worker_sessions, batch, woken):
    _task, root, (kid,) = await _tree(db_session, commit=False, root_updated_at=_OLDEST)
    if batch == "all_terminal":
        kid.phase = P.COMPLETED.value
    elif batch == "settled":
        kid.phase = P.COMPLETED.value
        root.checkpoint = _waiting("call-1", [kid.id], resolution="children_settled")
    elif batch == "malformed":
        root.checkpoint = {"task_loop_wait": {"kind": WAITING_CHILD_WAIT_KIND}}
    elif batch == "foreign_child":
        root.checkpoint = _waiting("call-1", [kid.id, generate_ulid()])
    elif batch == "stale_child":
        kid.lifecycle_generation = "an-older-lifecycle"
    await db_session.commit()

    await runtime_tasks.recover_stalled_task_coordinator_wakes_once()

    root = await _fresh(db_session, TaskCoordinatorRun, root.id)
    assert (root.wake_generation is not None) is woken
    rows = await _journal(db_session, root.id)
    assert [(row.kind, row.payload["resolution"]) for row in rows] == (
        [("wake", "recovered")] if woken else []
    )
    if woken and batch != "malformed":
        assert rows[0].payload["wait"]["kind"] == WAITING_CHILD_WAIT_KIND


@pytest.mark.asyncio
async def test_a_plan_dag_root_keeps_its_step_lineage_check(db_session, worker_sessions):
    # A Plan/Step coordinator without child lineage is woken for the worker
    # to surface, as before; it never reads task_agent_child_runs.
    _task, root, _ = await _tree(
        db_session, count=0, engine=TaskCoordinatorEngine.PLAN_DAG.value, root_updated_at=_OLDEST,
    )
    await runtime_tasks.recover_stalled_task_coordinator_wakes_once()
    root = await _fresh(db_session, TaskCoordinatorRun, root.id)
    assert root.wake_generation is not None
    assert await _journal(db_session, root.id) == []


# ── reconcile: a terminal Task cancels its children ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_a_terminal_task_cancels_every_live_child(db_session, worker_sessions, status):
    task, root, (ready, parked, running) = await _tree(db_session, count=3, commit=False)
    parked.phase = P.WAITING_HUMAN.value
    done = _child(task, root, index=0, batch=1, phase=P.COMPLETED.value, call_id="call-0")
    db_session.add(done)
    await db_session.flush()
    await stage_task_agent_child_wake(db_session, child_id=ready.id, reason="start")
    # A parked child's wake may be a day away; it is closed now.
    await stage_task_agent_child_wake(
        db_session, child_id=parked.id, reason="later", delay_seconds=86400,
    )
    wake = await stage_task_agent_child_wake(db_session, child_id=running.id, reason="start")
    await durable_run.claim_run_wake(
        db_session, TASK_AGENT_CHILD_RUN_SPEC, key=running.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="old-owner", lease_seconds=120,
    )
    task.status = status
    await db_session.commit()

    assert await runtime_tasks.reconcile_terminal_task_coordinator_runs_once() >= 1

    root = await _fresh(db_session, TaskCoordinatorRun, root.id)
    assert root.phase == (P.FAILED.value if status == "failed" else P.CANCELLED.value)
    for kid in (ready, parked, running):
        kid = await _fresh(db_session, TaskAgentChildRun, kid.id)
        assert (kid.phase, kid.outcome_status, kid.interrupted) == (P.CANCELLED.value, "cancelled", True)
        assert kid.terminal_at is not None
        assert (kid.claim_token, kid.wake_generation) == (None, None)
        event = await _event(db_session, kid.id)
        assert event.delivered_at is not None
        assert event.last_error == TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED
        rows = await _journal(db_session, kid.id)
        assert [(row.kind, row.payload["source"], row.payload["error_code"]) for row in rows] == [
            ("terminal", "task_status", f"task_{status}"),
        ]
    # The stale owner can no longer renew: its heartbeat stops the work.
    assert not await durable_run.renew_run_claim(
        db_session, TASK_AGENT_CHILD_RUN_SPEC, key=running.id,
        claim_token="old-owner", claim_generation=wake.wake_generation, lease_seconds=120,
    )
    # A finished child keeps its outcome.
    assert (await _fresh(db_session, TaskAgentChildRun, done.id)).phase == P.COMPLETED.value
    # Nothing is left to stop.
    await db_session.commit()
    await runtime_tasks.reconcile_terminal_task_coordinator_runs_once()
    assert len(await _journal(db_session, ready.id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("root_phase", [P.CANCELLED.value, P.FAILED.value, P.COMPLETED.value])
async def test_children_of_an_already_stopped_root_are_cancelled(db_session, worker_sessions, root_phase):
    task, root, (kid,) = await _tree(db_session, root_phase=root_phase, commit=False)
    task.status = "cancelled"
    await db_session.commit()
    root_version = (await _fresh(db_session, TaskCoordinatorRun, root.id)).state_version

    assert await runtime_tasks.reconcile_terminal_task_coordinator_runs_once() >= 1

    assert (await _fresh(db_session, TaskAgentChildRun, kid.id)).phase == P.CANCELLED.value
    root = await _fresh(db_session, TaskCoordinatorRun, root.id)
    assert (root.phase, root.state_version) == (root_phase, root_version)


# ── media job hook ──────────────────────────────────────────────────────


def _job(task: Task, job_id: str, status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(id=job_id, entity_id=task.entity_id, status=status, params={"task_id": task.id})


@pytest.mark.asyncio
async def test_a_settled_media_job_wakes_the_child_parked_on_it(db_session):
    from packages.core.services.task_coordinator_wakeup import wake_task_coordinator_for_media_job

    task, root, (waiting, other) = await _tree(db_session, count=2, child_phase=P.WAITING_JOB.value, commit=False)
    waiting.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_job", "tool_call_id": "j",
                                             "job_ids": ["job-1"], "deadline_at": "2026-10-09T00:00:00+00:00"}}
    other.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_job", "tool_call_id": "j",
                                           "job_ids": ["job-2"], "deadline_at": "2026-10-09T00:00:00+00:00"}}
    await db_session.commit()

    # Still running: nothing wakes.
    assert await wake_task_coordinator_for_media_job(db_session, _job(task, "job-1", "processing")) is None
    assert await _event(db_session, waiting.id) is None

    # The root run is waiting on its children, not on the job: no root wake.
    assert await wake_task_coordinator_for_media_job(db_session, _job(task, "job-1")) is None
    await db_session.commit()

    assert (await _event(db_session, waiting.id)) is not None
    assert (await _event(db_session, other.id)) is None
    assert (await _fresh(db_session, TaskCoordinatorRun, root.id)).wake_generation is None
    rows = await _journal(db_session, waiting.id)
    assert [(row.kind, row.payload["resolution"]) for row in rows] == [("wake", "media_jobs_settled")]
    assert rows[0].payload["wait"]["kind"] == "waiting_job"


@pytest.mark.asyncio
async def test_a_media_job_leaves_a_child_with_a_live_owner(db_session):
    from packages.core.services.task_coordinator_wakeup import wake_task_coordinator_for_media_job

    task, _root, (kid,) = await _tree(db_session, child_phase=P.WAITING_JOB.value, commit=False)
    kid.checkpoint = {"task_loop_wait": {"version": 1, "kind": "waiting_job", "tool_call_id": "j",
                                         "job_ids": ["job-1"], "deadline_at": "2026-10-09T00:00:00+00:00"}}
    await db_session.flush()
    wake = await stage_task_agent_child_wake(db_session, child_id=kid.id, reason="start")
    await durable_run.claim_run_wake(
        db_session, TASK_AGENT_CHILD_RUN_SPEC, key=kid.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token=generate_ulid(), lease_seconds=120,
    )
    await db_session.commit()

    await wake_task_coordinator_for_media_job(db_session, _job(task, "job-1"))
    await db_session.commit()

    assert (await _fresh(db_session, TaskAgentChildRun, kid.id)).wake_generation == wake.wake_generation
    assert await _journal(db_session, kid.id) == []
