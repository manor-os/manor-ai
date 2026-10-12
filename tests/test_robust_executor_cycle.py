"""The executor cycle: adaptive cadence, the undispatchable watchdog, the plan
deadline, coordinator-child hygiene and strict refs for publish consumers.

The failure class these close: a plan that never ends. A step nobody can
lease sat PENDING forever; a plan whose worker died sat RUNNING forever; a
coordinator's delegated child outlived its parent. Each now has a visible,
human-required end state instead of a silent stall.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.constants.execution import (
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    DEFAULT_PLAN_MAX_RUNTIME_SECONDS,
    DISPATCH_SKIP_KEY,
    DISPATCH_SKIPPED_ERROR_TYPE,
    PLAN_DEADLINE_ERROR_TYPE,
    STEP_UNDISPATCHABLE_ERROR_TYPE,
    UNDISPATCHABLE_SOFT_GRACE_SECONDS,
    UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS,
    DispatchSkipReason,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.worker import WorkLease
from packages.core.dispatcher.service import Dispatcher
from packages.core.plans.service import cancel_plan
from packages.core.plans.executor import (
    PENDING_POLL_SECONDS,
    RUNNING_POLL_SECONDS,
    PlanExecutor,
    resolve_plan_max_runtime_seconds,
    undispatchable_step_failure,
)
from packages.core.plans.wakeup import plan_cycle_outbox_dedupe_key


@pytest.fixture
def announced(monkeypatch):
    """Silence chat/task-event side effects; keep what would have been posted."""
    calls: list[dict] = []

    async def record(*_a, **kwargs):
        calls.append(kwargs)

    async def quiet(*_a, **_k):
        return None

    async def fail_llm(*_a, **_k):
        raise AssertionError("no LLM call expected in these cycles")

    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(PlanExecutor, "_announce", staticmethod(record))
    monkeypatch.setattr(PlanExecutor, "_emit_task_event", staticmethod(quiet))
    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fail_llm)
    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: None)
    monkeypatch.setattr(ai_tasks.run_plan, "apply_async", lambda *a, **k: None)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)
    return calls


def _executor(db_session) -> PlanExecutor:
    @asynccontextmanager
    async def factory():
        yield db_session

    return PlanExecutor(session_factory=factory)


async def _seed_plan(
    db, step_specs, *, metadata=None, started_at=None, task_id=None, entity_id=None,
    plan_status=ExecutionPlanStatus.RUNNING.value,
):
    entity_id = entity_id or generate_ulid()
    plan_id = generate_ulid()
    dag_steps, rows = [], []
    for spec in step_specs:
        spec = dict(spec)
        key = spec["key"]
        dag_steps.append({"key": key, "depends_on": spec.get("depends_on", []), **spec.get("dag", {})})
        rows.append(ExecutionStep(
            id=generate_ulid(), plan_id=plan_id, entity_id=entity_id, step_key=key,
            kind=spec.get("kind", "llm"), step_status=spec.get("status", "pending"),
            depends_on=spec.get("depends_on", []),
            params=spec.get("params", {"prompt": f"do {key}"}),
            error=spec.get("error"), result=spec.get("result"),
            service_key=spec.get("service_key"), capability_id=spec.get("capability_id"),
            expected_output_schema=spec.get("schema"),
        ))
    plan_dag = {"steps": dag_steps}
    if metadata:
        plan_dag["metadata"] = metadata
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id, status=plan_status,
        execution_mode="live", started_at=started_at or datetime.now(timezone.utc),
        plan_dag=plan_dag, dispatcher_state={"_response_locale": "en"},
    )
    db.add(plan)
    db.add_all(rows)
    await db.commit()
    return plan, {row.step_key: row for row in rows}


def _skip(reason: DispatchSkipReason, *, structural: bool, age_seconds: float, message="skipped"):
    since = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "type": DISPATCH_SKIPPED_ERROR_TYPE, "message": message,
        DISPATCH_SKIP_KEY: {
            "reason": reason.value, "message": message,
            "since": since.isoformat(), "structural": structural,
        },
    }


# ── adaptive cadence ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_plan_waiting_only_on_running_workers_polls_slowly(db_session, announced):
    plan, _ = await _seed_plan(db_session, [{"key": "work", "status": "running"}])

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["next_action"] == "schedule_self"
    assert result["delay_seconds"] == RUNNING_POLL_SECONDS == 30


@pytest.mark.asyncio
async def test_a_plan_with_pending_steps_polls_every_ten_seconds(db_session, announced):
    plan, _ = await _seed_plan(db_session, [{"key": "work"}])
    before = datetime.now(timezone.utc)

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["next_action"] == "schedule_self"
    assert result["delay_seconds"] == PENDING_POLL_SECONDS == 10
    assert result["continuation_staged"] is True
    wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == plan_cycle_outbox_dedupe_key(plan.id)
        )
    )).scalar_one()
    assert wake.delivered_at is None
    assert wake.available_at >= before + timedelta(seconds=9)


@pytest.mark.asyncio
async def test_a_sleep_step_keeps_its_own_longer_delay(db_session, announced):
    plan, steps = await _seed_plan(db_session, [
        {"key": "nap", "kind": "sleep", "params": {"seconds": 45}},
        {"key": "after", "depends_on": ["nap"]},
    ])

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["delay_seconds"] == 45
    await db_session.refresh(steps["nap"])
    assert steps["nap"].step_status == ExecutionStepStatus.DONE


@pytest.mark.asyncio
async def test_a_plan_parked_on_a_person_does_not_poll(db_session, announced):
    plan, _ = await _seed_plan(db_session, [{"key": "ask", "kind": "human", "params": {"prompt": "ok?"}}])

    result = await _executor(db_session).run_cycle(plan.id)

    assert result == {"plan_id": plan.id, "status": "running", "next_action": "wait", "delay_seconds": 0}


# ── the undispatchable watchdog ───────────────────────────────────────


def _pending(key="draft", *, error=None, service_key="content", capability_id=None, kind="llm"):
    return SimpleNamespace(
        step_key=key, kind=kind, step_status=ExecutionStepStatus.PENDING.value, error=error,
        service_key=service_key, capability_id=capability_id, provider=None, action_key=None,
        risk_level="low",
    )


def test_a_pending_step_with_no_error_is_stamped_as_awaiting_a_worker():
    now = datetime.now(timezone.utc)
    step = _pending()

    assert undispatchable_step_failure(step, now=now) is None

    assert step.error["type"] == DISPATCH_SKIPPED_ERROR_TYPE
    skip = step.error[DISPATCH_SKIP_KEY]
    assert skip["reason"] == DispatchSkipReason.AWAITING_WORKER.value
    assert skip["structural"] is False
    assert skip["since"] == now.isoformat()
    assert skip["message"]


def test_a_real_prior_error_is_left_alone():
    step = _pending(error={"type": "ProviderError", "message": "boom"})

    assert undispatchable_step_failure(step, now=datetime.now(timezone.utc)) is None

    assert step.error == {"type": "ProviderError", "message": "boom"}


def test_a_structural_skip_escalates_only_after_its_grace():
    now = datetime.now(timezone.utc)
    early = _pending(error=_skip(
        DispatchSkipReason.SUBSCRIPTION_UNRESOLVED, structural=True,
        age_seconds=UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS - 60,
    ))
    assert undispatchable_step_failure(early, now=now) is None
    assert early.error["type"] == DISPATCH_SKIPPED_ERROR_TYPE, "still waiting, still stamped"

    late = _pending(error=_skip(
        DispatchSkipReason.SUBSCRIPTION_UNRESOLVED, structural=True,
        age_seconds=UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS + 60,
    ))
    failure = undispatchable_step_failure(late, now=now)

    assert failure["type"] == STEP_UNDISPATCHABLE_ERROR_TYPE
    assert failure["reason"] == DispatchSkipReason.SUBSCRIPTION_UNRESOLVED.value
    assert failure["since"] == late.error[DISPATCH_SKIP_KEY]["since"]
    assert failure["message"] == (
        "No active agent is subscribed to service 'content' in this workspace, "
        "so nothing can run this step."
    )
    assert failure["failure"] == {
        "reason": failure["message"], "retryable": False, "requires_human": False,
        "blockers": [DispatchSkipReason.SUBSCRIPTION_UNRESOLVED.value],
    }


def test_a_soft_skip_waits_the_long_grace():
    now = datetime.now(timezone.utc)
    early = _pending(error=_skip(
        DispatchSkipReason.AWAITING_WORKER, structural=False,
        age_seconds=UNDISPATCHABLE_SOFT_GRACE_SECONDS - 60,
    ))
    assert undispatchable_step_failure(early, now=now) is None

    late = _pending(error=_skip(DispatchSkipReason.AWAITING_WORKER, structural=False, age_seconds=3 * 3600))
    failure = undispatchable_step_failure(late, now=datetime.now(timezone.utc))

    assert failure["type"] == STEP_UNDISPATCHABLE_ERROR_TYPE
    assert failure["message"] == "No worker picked this step up for 3h."
    assert failure["failure"]["requires_human"] is False


@pytest.mark.parametrize(
    "reason, structural, step_kwargs, sentence",
    [
        (DispatchSkipReason.WORKER_NOT_BOUND, True, {}, "The agent for 'content' is not bound to any worker."),
        (
            DispatchSkipReason.CAPABILITY_UNSUPPORTED, True, {"capability_id": "video.render"},
            "No worker supports capability 'video.render'.",
        ),
        (DispatchSkipReason.WORKSPACE_BUDGET, False, {}, "The workspace monthly budget is exhausted."),
    ],
)
def test_each_reason_has_a_plain_sentence(reason, structural, step_kwargs, sentence):
    age = (UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS if structural else UNDISPATCHABLE_SOFT_GRACE_SECONDS) + 60
    step = _pending(error=_skip(reason, structural=structural, age_seconds=age), **step_kwargs)

    failure = undispatchable_step_failure(step, now=datetime.now(timezone.utc))

    assert failure["message"] == sentence
    assert failure["failure"]["blockers"] == [reason.value]


@pytest.mark.asyncio
async def test_run_cycle_stamps_a_fresh_pending_step_and_keeps_it_pending(db_session, announced):
    plan, steps = await _seed_plan(db_session, [{"key": "work"}])

    await _executor(db_session).run_cycle(plan.id)

    await db_session.refresh(steps["work"])
    assert steps["work"].step_status == ExecutionStepStatus.PENDING
    assert steps["work"].error[DISPATCH_SKIP_KEY]["reason"] == DispatchSkipReason.AWAITING_WORKER.value


@pytest.mark.asyncio
async def test_run_cycle_fails_a_step_stuck_past_its_grace_and_the_plan_parks(db_session, announced):
    plan, steps = await _seed_plan(db_session, [
        {
            "key": "work", "service_key": "content",
            "error": _skip(
                DispatchSkipReason.SUBSCRIPTION_UNRESOLVED, structural=True,
                age_seconds=UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS + 120,
            ),
        },
        {"key": "after", "depends_on": ["work"]},
    ])
    runner = _executor(db_session)

    first = await runner.run_cycle(plan.id)
    await db_session.refresh(steps["work"])
    assert steps["work"].step_status == ExecutionStepStatus.FAILED
    assert steps["work"].error["type"] == STEP_UNDISPATCHABLE_ERROR_TYPE
    assert steps["work"].error["failure"]["requires_human"] is False
    assert "service 'content'" in steps["work"].error["message"]
    failed_events = [
        evt for call in announced for evt in call.get("chat_events") or []
        if evt.get("kind") == "step_failed"
    ]
    assert failed_events and failed_events[0]["will_retry"] is False
    assert failed_events[0]["error"]["type"] == STEP_UNDISPATCHABLE_ERROR_TYPE

    # The dependent is skipped on the next pass and the plan ends failed.
    if first["status"] != "failed":
        await runner.run_cycle(plan.id)
    await db_session.refresh(plan)
    await db_session.refresh(steps["after"])
    assert plan.status == ExecutionPlanStatus.FAILED
    assert steps["after"].step_status == ExecutionStepStatus.SKIPPED


# ── coordinator children ──────────────────────────────────────────────


def _child_params(
    parent="coordinate", *, item="research", typed=False, released=False,
):
    return {
        "prompt": "delegated",
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_PARENT_STEP_PARAM_KEY: parent,
        COORDINATOR_CHILD_ITEM_PARAM_KEY: item,
        COORDINATOR_TYPED_CHILD_PARAM_KEY: typed,
        COORDINATOR_CHILD_RELEASED_PARAM_KEY: released,
    }


def test_pick_runnable_only_offers_released_typed_coordinator_children():
    legacy = SimpleNamespace(
        step_key="legacy", step_status=ExecutionStepStatus.PENDING.value,
        depends_on=[], params=_child_params(),
    )
    dormant = SimpleNamespace(
        step_key="dormant", step_status=ExecutionStepStatus.PENDING.value,
        depends_on=[], params=_child_params(typed=True),
    )
    released = SimpleNamespace(
        step_key="released", step_status=ExecutionStepStatus.PENDING.value,
        depends_on=[], params=_child_params(typed=True, released=True),
    )
    other = SimpleNamespace(step_key="o", step_status=ExecutionStepStatus.PENDING.value, depends_on=[], params={})

    steps = [legacy, dormant, released, other]
    assert PlanExecutor._pick_runnable(
        steps, {step.step_key: step for step in steps},
    ) == [released, other]
    assert legacy.step_status == ExecutionStepStatus.PENDING.value
    assert dormant.step_status == ExecutionStepStatus.PENDING.value


def test_only_the_exact_terminal_typed_child_wakes_a_parked_parent():
    parent = SimpleNamespace(
        id="parent-id",
        plan_id="plan-1",
        step_key="coordinate",
        step_status=ExecutionStepStatus.PAUSED.value,
        current_lease_id="old-lease",
        params={
            COORDINATOR_PARAM_KEY: True,
            COORDINATOR_WAITING_CHILD_PARAM_KEY: {
                "child_step_id": "wanted-id",
                "checklist_item_key": "research",
            },
        },
    )
    wanted = SimpleNamespace(
        id="wanted-id",
        plan_id="plan-1",
        step_key="coordinate__research",
        step_status=ExecutionStepStatus.RUNNING.value,
        params=_child_params(typed=True, released=True),
    )
    other = SimpleNamespace(
        id="other-id",
        plan_id="plan-1",
        step_key="coordinate__other",
        step_status=ExecutionStepStatus.DONE.value,
        params=_child_params(item="other", typed=True, released=True),
    )

    assert PlanExecutor._wake_waiting_coordinator_parents(
        [parent, wanted, other],
    ) is False
    assert parent.step_status == ExecutionStepStatus.PAUSED.value

    wanted.step_status = ExecutionStepStatus.DONE.value
    assert PlanExecutor._wake_waiting_coordinator_parents(
        [parent, wanted, other],
    ) is True
    assert parent.step_status == ExecutionStepStatus.PENDING.value
    assert parent.current_lease_id is None
    assert COORDINATOR_WAITING_CHILD_PARAM_KEY in parent.params


@pytest.mark.asyncio
async def test_released_typed_sleep_stays_pending_until_its_durable_wake(
    db_session, announced,
):
    wake_at = datetime.now(timezone.utc) + timedelta(seconds=45)
    plan, steps = await _seed_plan(db_session, [
        {
            "key": "coordinate",
            "kind": "subagent",
            "status": ExecutionStepStatus.PAUSED.value,
            "params": {COORDINATOR_PARAM_KEY: True},
        },
        {
            "key": "coordinate__wait",
            "kind": "sleep",
            "status": ExecutionStepStatus.PENDING.value,
            "params": {
                **_child_params(item="wait", typed=True, released=True),
                COORDINATOR_CHILD_WAKE_AT_PARAM_KEY: wake_at.isoformat(),
            },
        },
    ])
    parent = steps["coordinate"]
    child = steps["coordinate__wait"]
    parent.params = {
        **parent.params,
        COORDINATOR_WAITING_CHILD_PARAM_KEY: {
            "child_step_id": child.id,
            "child_step_key": child.step_key,
            "checklist_item_key": "wait",
        },
    }
    await db_session.commit()
    child_id = child.id
    parent_id = parent.id

    result = await _executor(db_session).run_cycle(plan.id)

    db_session.expire_all()
    child = await db_session.get(ExecutionStep, child_id)
    parent = await db_session.get(ExecutionStep, parent_id)
    assert result["next_action"] == "schedule_self"
    assert result["delay_seconds"] >= 30
    assert child.step_status == ExecutionStepStatus.PENDING.value
    assert child.result is None
    assert child.finished_at is None
    assert parent.step_status == ExecutionStepStatus.PAUSED.value


@pytest.mark.asyncio
async def test_a_running_child_whose_parent_finished_is_cancelled(db_session, announced):
    plan, steps = await _seed_plan(db_session, [
        {"key": "coordinate", "kind": "subagent", "status": "done", "result": {"text": "all done"}},
        {
            "key": "coordinate__research_1",
            "kind": "subagent",
            "status": "running",
            "params": _child_params(typed=True, released=True),
        },
    ])
    child = steps["coordinate__research_1"]
    lease = WorkLease(
        id=generate_ulid(),
        step_id=child.id,
        plan_id=plan.id,
        entity_id=plan.entity_id,
        worker_id="internal",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        status=WorkLeaseStatus.ACTIVE.value,
        execution_claim_id=generate_ulid(),
        execution_claimed_at=datetime.now(timezone.utc),
        credential_leases=[{"vault_lease_id": "secret-lease"}],
    )
    child.current_lease_id = lease.id
    db_session.add(lease)
    await db_session.commit()

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["status"] == "completed"
    await db_session.refresh(child)
    await db_session.refresh(lease)
    assert child.step_status == ExecutionStepStatus.CANCELLED
    assert child.current_lease_id is None
    assert child.finished_at is not None
    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert lease.execution_claim_id is None
    assert lease.execution_claimed_at is None
    assert lease.credential_leases == []


@pytest.mark.asyncio
async def test_orphan_cleanup_serializes_after_child_lease_settlement(
    db_session,
):
    """Lease -> Step settlement wins cleanly over concurrent orphan cleanup."""

    plan, steps = await _seed_plan(db_session, [
        {
            "key": "coordinate",
            "kind": "subagent",
            "status": ExecutionStepStatus.DONE.value,
            "result": {"text": "parent already finished"},
        },
        {
            "key": "coordinate__research",
            "kind": "subagent",
            "status": ExecutionStepStatus.RUNNING.value,
            "params": _child_params(typed=True, released=True),
        },
    ])
    child = steps["coordinate__research"]
    lease = WorkLease(
        id=generate_ulid(),
        step_id=child.id,
        plan_id=plan.id,
        entity_id=plan.entity_id,
        worker_id="internal",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        status=WorkLeaseStatus.ACTIVE.value,
    )
    child.current_lease_id = lease.id
    db_session.add(lease)
    await db_session.commit()
    child_id = child.id
    lease_id = lease.id

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as settle_db:
        await Dispatcher().complete_lease(
            settle_db,
            lease.id,
            result={"text": "child settled"},
        )

        async def cleanup() -> bool:
            async with session_factory() as cleanup_db:
                await cleanup_db.execute(
                    select(ExecutionPlan)
                    .where(ExecutionPlan.id == plan.id)
                    .with_for_update()
                )
                cleanup_steps = list((await cleanup_db.execute(
                    select(ExecutionStep)
                    .where(ExecutionStep.plan_id == plan.id)
                    .order_by(ExecutionStep.created_at)
                )).scalars().all())
                changed = await PlanExecutor._cancel_orphaned_coordinator_children(
                    cleanup_db,
                    cleanup_steps,
                    now=datetime.now(timezone.utc),
                )
                await cleanup_db.commit()
                return changed

        cleanup_task = asyncio.create_task(cleanup())
        await asyncio.sleep(0.05)
        assert not cleanup_task.done(), "cleanup must wait for the lease owner"
        await settle_db.commit()

    assert await asyncio.wait_for(cleanup_task, timeout=2) is False
    db_session.expire_all()
    stored_child = await db_session.get(ExecutionStep, child_id)
    stored_lease = await db_session.get(WorkLease, lease_id)
    assert stored_child is not None
    assert stored_child.step_status == ExecutionStepStatus.DONE.value
    assert stored_lease is not None
    assert stored_lease.status == WorkLeaseStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_cancel_plan_closes_parked_coordinator_rows_and_suspended_lease(
    db_session,
):
    plan, steps = await _seed_plan(db_session, [
        {
            "key": "coordinate",
            "kind": "subagent",
            "status": ExecutionStepStatus.PAUSED.value,
            "params": {COORDINATOR_PARAM_KEY: True},
        },
        {
            "key": "coordinate__research",
            "kind": "subagent",
            "status": ExecutionStepStatus.PAUSED.value,
            "params": _child_params(typed=True, released=False),
        },
    ])
    root = steps["coordinate"]
    child = steps["coordinate__research"]
    lease = WorkLease(
        id=generate_ulid(),
        step_id=root.id,
        plan_id=plan.id,
        entity_id=plan.entity_id,
        worker_id="internal",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        status=WorkLeaseStatus.SUSPENDED.value,
        execution_claim_id=generate_ulid(),
        execution_claimed_at=datetime.now(timezone.utc),
        credential_leases=[{"vault_lease_id": "stale-secret-lease"}],
    )
    root.current_lease_id = lease.id
    db_session.add(lease)
    await db_session.commit()

    await cancel_plan(db_session, plan.id, reason="operator cancelled")
    await db_session.commit()

    for step in (root, child):
        await db_session.refresh(step)
        assert step.step_status == ExecutionStepStatus.CANCELLED.value
        assert step.current_lease_id is None
        assert step.finished_at is not None
    await db_session.refresh(lease)
    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert lease.execution_claim_id is None
    assert lease.execution_claimed_at is None
    assert lease.credential_leases == []


@pytest.mark.asyncio
async def test_a_running_child_under_a_running_parent_is_left_alone(db_session, announced):
    plan, steps = await _seed_plan(db_session, [
        {"key": "coordinate", "kind": "subagent", "status": "running"},
        {"key": "coordinate__research_1", "kind": "subagent", "status": "running", "params": _child_params()},
    ])

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["delay_seconds"] == RUNNING_POLL_SECONDS
    child = steps["coordinate__research_1"]
    await db_session.refresh(child)
    assert child.step_status == ExecutionStepStatus.RUNNING


# ── the plan deadline ─────────────────────────────────────────────────


def test_plan_runtime_budget_resolution():
    assert resolve_plan_max_runtime_seconds(None, None) == DEFAULT_PLAN_MAX_RUNTIME_SECONDS
    assert resolve_plan_max_runtime_seconds({"execution_policy": {"max_plan_runtime_seconds": 3600}}, {}) == 3600
    # Same order as retry_policy: workspace, then plan metadata; later wins.
    assert resolve_plan_max_runtime_seconds(
        {"execution_policy": {"max_plan_runtime_seconds": 3600}},
        {"metadata": {"max_plan_runtime_seconds": 900}},
    ) == 900
    assert resolve_plan_max_runtime_seconds({}, {"metadata": {"max_plan_runtime_seconds": 5}}) == 60
    assert resolve_plan_max_runtime_seconds(
        {"execution_policy": {"max_plan_runtime_seconds": "soon"}},
        {"metadata": {"max_plan_runtime_seconds": None}},
    ) == DEFAULT_PLAN_MAX_RUNTIME_SECONDS
    assert resolve_plan_max_runtime_seconds(
        {"execution_policy": {"max_plan_runtime_seconds": -10}}, {"metadata": {}},
    ) == DEFAULT_PLAN_MAX_RUNTIME_SECONDS


async def _seed_task(db):
    from packages.core.models.task import Task
    from packages.core.models.user import User

    entity_id = generate_ulid()
    operator = User(
        id=generate_ulid(), entity_id=entity_id, email=f"{generate_ulid()}@deadline.example",
        password_hash="test-only", role="owner",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, creator_id=operator.id,
        title="Summarize the meeting notes", status="in_progress", priority=3,
        task_type="general", details={},
    )
    db.add_all([operator, task])
    await db.commit()
    return task


@pytest.mark.asyncio
async def test_a_plan_past_its_budget_revokes_leases_and_fails_every_open_step(db_session, announced, monkeypatch):
    from packages.core.dispatcher.service import Dispatcher
    from packages.core.models.task import Task

    revoked: list[tuple] = []

    async def fake_revoke(self, db, *, plan_id=None, step_ids=None, reason):
        revoked.append((plan_id, step_ids, reason))
        return []

    monkeypatch.setattr(Dispatcher, "revoke_active_leases", fake_revoke, raising=False)
    task = await _seed_task(db_session)
    plan, steps = await _seed_plan(
        db_session,
        [
            {"key": "running", "status": "running"},
            {"key": "pending", "depends_on": ["running"]},
            {"key": "ask", "kind": "human", "status": "waiting_human", "params": {"prompt": "?"}},
            {"key": "done", "status": "done", "result": {"text": "ok"}},
        ],
        metadata={"max_plan_runtime_seconds": 3600},
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        task_id=task.id, entity_id=task.entity_id,
    )

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["status"] == "failed"
    assert revoked == [(plan.id, None, ANY)]
    for key in ("running", "pending", "ask"):
        await db_session.refresh(steps[key])
        assert steps[key].step_status == ExecutionStepStatus.FAILED, key
        assert steps[key].error["type"] == PLAN_DEADLINE_ERROR_TYPE
        assert steps[key].error["failure"]["requires_human"] is False
        assert steps[key].error["failure"]["retryable"] is False
        assert steps[key].error["failure"]["blockers"] == ["plan_deadline"]
        assert steps[key].finished_at is not None
    await db_session.refresh(steps["done"])
    assert steps["done"].step_status == ExecutionStepStatus.DONE
    await db_session.refresh(plan)
    assert plan.status == ExecutionPlanStatus.FAILED
    assert plan.last_error["type"] == PLAN_DEADLINE_ERROR_TYPE
    task = await db_session.get(Task, task.id)
    await db_session.refresh(task)
    assert task.status != "waiting_on_customer", "a runtime deadline is not a user-input request"
    failed_events = [
        evt for call in announced for evt in call.get("chat_events") or []
        if evt.get("kind") == "step_failed"
    ]
    assert {evt["step"].step_key for evt in failed_events} == {"running", "pending", "ask"}


@pytest.mark.asyncio
async def test_a_plan_inside_its_budget_is_untouched(db_session, announced, monkeypatch):
    from packages.core.dispatcher.service import Dispatcher

    async def never(self, db, **_kw):
        raise AssertionError("no revoke inside the budget")

    monkeypatch.setattr(Dispatcher, "revoke_active_leases", never, raising=False)
    plan, steps = await _seed_plan(
        db_session, [{"key": "running", "status": "running"}],
        metadata={"max_plan_runtime_seconds": 3600},
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    result = await _executor(db_session).run_cycle(plan.id)

    assert result["status"] == "running"
    await db_session.refresh(steps["running"])
    assert steps["running"].step_status == ExecutionStepStatus.RUNNING


# ── strict refs for publish consumers ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_publish_step_fails_on_a_missing_ref_field_where_an_ordinary_step_degrades(db_session, announced):
    plan, steps = await _seed_plan(db_session, [
        {"key": "prepare", "kind": "subagent", "status": "done", "result": {"text": "draft"}},
        {
            "key": "publish", "kind": "subagent", "depends_on": ["prepare"],
            "params": {"prompt": "post: ${{ steps.prepare.result.post_text }}"},
            "dag": {"expects": ["publish"]},
        },
        {
            "key": "summarize", "kind": "subagent", "depends_on": ["prepare"],
            "params": {"prompt": "sum: ${{ steps.prepare.result.post_text }}"},
        },
    ])

    await _executor(db_session).run_cycle(plan.id)

    await db_session.refresh(steps["publish"])
    assert steps["publish"].step_status == ExecutionStepStatus.FAILED
    assert steps["publish"].error["type"] == "ReferenceError"
    assert "strict" in steps["publish"].error["message"]
    await db_session.refresh(steps["summarize"])
    assert steps["summarize"].step_status == ExecutionStepStatus.PENDING
    assert "${{" not in steps["summarize"].params["prompt"]
