"""Explicit per-step runtime deadline.

Before this, the binding limit on any plan step was the Celery
``soft_time_limit=1800`` on ``execute_lease``: a step that legitimately needed
more than 30 minutes was SIGKILL'd, the lease heartbeat died with the process,
``cleanup_expired_leases`` reclaimed the lease minutes later, and the step
retried into the same wall until its attempts burned — with a failure message
that said nothing about the real cause.

These tests pin the replacement:

  * ``max_runtime_seconds`` resolves step → plan → workspace → default, the
    same layering ``retry_policy`` uses;
  * the worker enforces that budget in-process and reports a structured
    ``StepDeadlineExceeded`` through ``fail_lease``;
  * the heartbeat keeps a live step's lease alive past the lease TTL, so
    "still running" never means "dead";
  * a blown deadline is an ordinary step failure that flows through the retry
    policy;
  * the Celery limits stay strictly above the maximum step deadline — the
    invariant whose absence caused the original bug.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import Workspace
from packages.core.services.step_deadline import (
    DEFAULT_MAX_RUNTIME_SECONDS,
    MAX_MAX_RUNTIME_SECONDS,
    merge_max_runtime_configs,
    resolve_step_deadline,
)
from packages.core.workers import internal


# ── resolution order ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("workspace_cfg", "plan_cfg", "step_cfg", "expected_seconds", "expected_source"),
    [
        (None, None, None, DEFAULT_MAX_RUNTIME_SECONDS, "default"),
        (900, None, None, 900, "workspace"),
        (900, 1800, None, 1800, "plan"),
        (900, 1800, 60, 60, "step"),
        (None, None, 60, 60, "step"),
        # Unusable values fall through to the next-lower layer.
        ("nonsense", 1800, None, 1800, "plan"),
        (900, None, 0, 900, "workspace"),
        (None, None, -5, DEFAULT_MAX_RUNTIME_SECONDS, "default"),
        # Configured values above the ceiling are clamped, which is what
        # keeps the celery backstop strictly above every step deadline.
        (None, None, 99 * 3600, MAX_MAX_RUNTIME_SECONDS, "step"),
    ],
)
def test_max_runtime_resolution_order(
    workspace_cfg, plan_cfg, step_cfg, expected_seconds, expected_source
):
    deadline = merge_max_runtime_configs(
        ("workspace", workspace_cfg),
        ("plan", plan_cfg),
        ("step", step_cfg),
    )
    assert deadline.max_runtime_seconds == expected_seconds
    assert deadline.source == expected_source


@pytest.mark.asyncio
async def test_resolve_step_deadline_reads_workspace_plan_and_step_rows(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add_all([
        Workspace(
            id=workspace_id, entity_id=entity_id, name="Long runs", status="active",
            settings={"execution_policy": {"max_runtime_seconds": 900}},
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
            status="running", execution_mode="live", approval_required=False,
            plan_dag={"steps": [], "metadata": {"max_runtime_seconds": 1800}},
        ),
    ])
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
        workspace_id=workspace_id, step_key="render_video", kind="subagent",
        params={}, depends_on=[], step_status="pending",
    )
    db_session.add(step)
    await db_session.flush()

    # plan beats workspace
    resolved = await resolve_step_deadline(db_session, step)
    assert (resolved.max_runtime_seconds, resolved.source) == (1800, "plan")

    # step beats plan
    step.params = {"max_runtime_seconds": 120}
    await db_session.flush()
    resolved = await resolve_step_deadline(db_session, step)
    assert (resolved.max_runtime_seconds, resolved.source) == (120, "step")


# ── in-process enforcement ────────────────────────────────────────────

async def _seed_lease(
    db,
    *,
    params: dict | None = None,
    attempt_count: int = 1,
    max_attempts: int = 3,
    lease_seconds: float = 300,
) -> dict:
    """One active lease on a running step, ready to be executed in-process."""
    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    now = datetime.now(timezone.utc)

    db.add_all([
        Worker(
            id=worker_id, entity_id=entity_id, kind="internal",
            display_name="Internal worker",
            capabilities={"supported_kinds": ["llm"], "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
            status="active",
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, status="running",
            execution_mode="live", approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id,
            step_key="long_running", kind="llm",
            params=params or {}, depends_on=[], step_status="running",
            risk_level="low", attempt_count=attempt_count,
            max_attempts=max_attempts, current_lease_id=lease_id,
            started_at=now,
        ),
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status="active",
            lease_until=now + timedelta(seconds=lease_seconds),
        ),
    ])
    await db.commit()
    return {
        "entity_id": entity_id, "plan_id": plan_id, "step_id": step_id,
        "lease_id": lease_id, "worker_id": worker_id,
        "lease_until": now + timedelta(seconds=lease_seconds),
    }


async def _reload(db, seeded) -> tuple[ExecutionStep, WorkLease]:
    db.expire_all()
    step = await db.get(ExecutionStep, seeded["step_id"])
    lease = await db.get(WorkLease, seeded["lease_id"])
    return step, lease


@pytest.mark.asyncio
async def test_step_body_past_its_deadline_fails_with_step_deadline_exceeded(
    db_session, monkeypatch
):
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 0.1})
    cancelled: list[bool] = []

    async def _slow_body(snapshot):
        assert snapshot["max_runtime_seconds"] == 0.1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return {"result": {"text": "never"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _slow_body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    assert cancelled == [True], "the body must be cancelled, never left running"

    error = outcome["error"]
    assert error["type"] == "StepDeadlineExceeded"
    assert error["message"] == "step exceeded its 0.1-second runtime budget"
    assert error["max_runtime_seconds"] == 0.1
    assert error["elapsed_seconds"] >= 0.1
    assert "max_runtime_seconds" in error["hint"]

    step, lease = await _reload(db_session, seeded)
    # The lease is terminal, not dangling — cleanup_expired_leases has
    # nothing left to reclaim.
    assert lease.status == "failed"
    assert lease.error["type"] == "StepDeadlineExceeded"
    assert step.error["type"] == "StepDeadlineExceeded"
    assert step.error["elapsed_seconds"] >= 0.1


@pytest.mark.asyncio
async def test_timeout_raised_inside_the_body_is_not_misreported_as_the_deadline(
    db_session, monkeypatch
):
    """A socket/client timeout inside a tool is an ordinary failure."""
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 300})

    async def _body_with_inner_timeout(snapshot):
        raise TimeoutError("upstream API read timed out")

    monkeypatch.setattr(internal, "_execute_by_kind", _body_with_inner_timeout)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    step, lease = await _reload(db_session, seeded)
    assert step.error["type"] == "TimeoutError"
    assert lease.error["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_heartbeat_keeps_lease_alive_past_ttl_for_a_step_inside_its_deadline(
    db_session, monkeypatch
):
    # A lease TTL far shorter than the step body: without the heartbeat the
    # lease would be reclaimable long before the step finishes.
    seeded = await _seed_lease(
        db_session,
        params={"max_runtime_seconds": 30},
        lease_seconds=0.3,
    )
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_EXTEND_SECONDS", 5)

    # The heartbeat runs off-loop on its own thread with a SYNC session, so the
    # spy goes on the sync extend, not ``Dispatcher.extend_lease``.
    # See tests/test_lease_heartbeat.py for why.
    extends: list[str] = []
    original_extend = internal.extend_active_lease_sync

    def _spy_extend(lease_id, **kwargs):
        extends.append(lease_id)
        return original_extend(lease_id, **kwargs)

    monkeypatch.setattr(internal, "extend_active_lease_sync", _spy_extend)
    internal._reset_lease_heartbeat_engine()

    async def _slow_but_within_budget(snapshot):
        await asyncio.sleep(0.55)
        return {"result": {"text": "done"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _slow_but_within_budget)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "completed"
    assert extends.count(seeded["lease_id"]) >= 2

    step, lease = await _reload(db_session, seeded)
    assert step.step_status == "done"
    assert lease.status == "completed"
    assert lease.heartbeat_count >= 2
    assert lease.lease_until > seeded["lease_until"]


@pytest.mark.asyncio
async def test_deadline_failure_flows_through_the_retry_policy(db_session, monkeypatch):
    async def _slow_body(snapshot):
        await asyncio.sleep(30)

    monkeypatch.setattr(internal, "_execute_by_kind", _slow_body)

    # Attempts remaining → back to pending for another try.
    retryable = await _seed_lease(
        db_session,
        params={"max_runtime_seconds": 0.05},
        attempt_count=1,
        max_attempts=3,
    )
    await internal.execute_lease_inproc(retryable["lease_id"])
    step, _lease = await _reload(db_session, retryable)
    assert step.step_status == "pending"
    assert step.current_lease_id is None
    assert step.error["type"] == "StepDeadlineExceeded"
    assert step.error["retry_policy"]["max_attempts"] == 3

    # A real re-checkout increments the attempt, so the budget is per attempt.
    worker = await db_session.get(Worker, retryable["worker_id"])
    leases = await Dispatcher().checkout_steps_for_worker(db_session, worker, max_n=1)
    await db_session.commit()
    assert [lease.step_id for lease, _ in leases] == [retryable["step_id"]]
    step, _lease = await _reload(db_session, retryable)
    assert step.attempt_count == 2

    # Attempts exhausted → terminal failure.
    exhausted = await _seed_lease(
        db_session,
        params={"max_runtime_seconds": 0.05, "retry_policy": {"max_attempts": 1}},
        attempt_count=1,
        max_attempts=1,
    )
    await internal.execute_lease_inproc(exhausted["lease_id"])
    step, _lease = await _reload(db_session, exhausted)
    assert step.step_status == "failed"
    assert step.finished_at is not None
    assert step.error["type"] == "StepDeadlineExceeded"

    # Attempts exhausted under auto_human_on_exhausted → waiting_human.
    to_human = await _seed_lease(
        db_session,
        params={
            "max_runtime_seconds": 0.05,
            "retry_policy": {"max_attempts": 1, "auto_human_on_exhausted": True},
        },
        attempt_count=1,
        max_attempts=1,
    )
    await internal.execute_lease_inproc(to_human["lease_id"])
    step, _lease = await _reload(db_session, to_human)
    assert step.step_status == "waiting_human"
    assert "StepDeadlineExceeded" in (step.human_input_prompt or "")


@pytest.mark.asyncio
async def test_deadline_failure_is_diagnosable_from_task_log_meta(db_session, monkeypatch):
    """The Task detail page must show the budget and the elapsed time."""
    from packages.core.dispatcher.service import _step_log_meta

    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 0.05})

    async def _slow_body(snapshot):
        await asyncio.sleep(30)

    monkeypatch.setattr(internal, "_execute_by_kind", _slow_body)
    await internal.execute_lease_inproc(seeded["lease_id"])

    step, lease = await _reload(db_session, seeded)
    meta = _step_log_meta(step, lease)
    assert meta["error_type"] == "StepDeadlineExceeded"
    assert meta["max_runtime_seconds"] == 0.05
    assert meta["elapsed_seconds"] >= 0.05
    assert meta["max_runtime_source"] == "step"
    # Alongside the retry diagnostics that were already there.
    assert meta["max_attempts"] == 3
    assert meta["attempt_count"] == 1
    assert meta["lease_id"] == seeded["lease_id"]
    assert meta["worker_id"] == seeded["worker_id"]


# ── the invariant that caused the original bug ────────────────────────

def test_celery_lease_limits_stay_above_the_step_deadline():
    """celery limit > step deadline, forever.

    ``execute_lease``'s own soft/hard limits override the app-level
    ``task_soft_time_limit`` / ``task_time_limit``, so these are the numbers
    that actually apply. If someone lowers them back under the step deadline,
    the process-level kill becomes the binding limit again and the whole
    StepDeadlineExceeded mechanism is dead code — that is exactly the
    regression this guard exists to catch.
    """
    from packages.core.celery_app import celery_app
    from packages.core.tasks.ai_tasks import execute_lease

    assert execute_lease.soft_time_limit > MAX_MAX_RUNTIME_SECONDS
    assert execute_lease.time_limit > execute_lease.soft_time_limit
    assert execute_lease.soft_time_limit > DEFAULT_MAX_RUNTIME_SECONDS

    # Per-task values must win over the global config, which is far lower.
    assert celery_app.conf.task_soft_time_limit < execute_lease.soft_time_limit
    assert celery_app.conf.task_time_limit < execute_lease.time_limit
