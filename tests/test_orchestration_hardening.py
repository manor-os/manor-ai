"""Three hazards that raising the per-step budget to 6 hours exposed.

Raising ``MAX_MAX_RUNTIME_SECONDS`` to 6h made a plan step legally outlive
every timeout the orchestration layer had quietly been relying on:

1. **Redis re-delivered long tasks.** ``task_acks_late=True`` acks only when a
   task *finishes*, but the Redis transport has no server-side ack — kombu
   emulates one with ``visibility_timeout``, default ONE HOUR. Any step past
   60 minutes was handed to a second worker while the first was still running
   it: duplicated publishes, duplicated media generation, duplicated paid API
   calls, and a late ``complete_lease`` on an already-terminal lease.
2. **One queue for everything.** ``execute_lease`` shared its four concurrency
   slots with ``internal_worker_tick`` / ``cleanup_expired_leases`` /
   ``scheduler.tick``. Four concurrent long steps stalled the whole system.
3. **Re-delivery could still double-execute.** Even with a correct visibility
   timeout, ``task_reject_on_worker_lost`` re-queues deliberately when a worker
   dies mid-flight — and the lease survives (only the heartbeat thread died),
   so the old ``lease.status != "active"`` entry check was a check-then-act
   race two deliveries could both pass.

These tests pin each fix to its contract, not to its current numbers: the
timeout ordering, the queue *registry*, and the database-level execution claim.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from packages.core.celery_app import celery_app
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.queues import (
    CELERY_BUILTIN_TASK_QUEUES,
    TASK_QUEUES,
    UNDECLARED_TASK_QUEUE,
    CeleryQueue,
    queue_for_task,
    route_task,
)
from packages.core.services.step_deadline import (
    CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS,
    CELERY_LEASE_HARD_TIME_LIMIT_SECONDS,
    CELERY_LEASE_SOFT_TIME_LIMIT_SECONDS,
    MAX_MAX_RUNTIME_SECONDS,
)
from packages.core.workers import internal
from packages.core.workers.execution_claim import (
    CLAIM_DENIED_HELD,
    CLAIM_DENIED_NOT_ACTIVE,
    claim_lease_for_execution,
    release_execution_claim,
)


# ══ Hazard 1 — the broker must outlast the task ═══════════════════════

def test_visibility_timeout_outranks_the_celery_limits_and_the_step_ceiling():
    """visibility timeout > hard limit > soft limit > step deadline ceiling.

    Every one of these is derived from ``MAX_MAX_RUNTIME_SECONDS``. If anyone
    reorders them the guarantee inverts and the layer below becomes the binding
    limit:

      * visibility <= hard  → the broker re-delivers a task Celery has not yet
        killed, and it runs twice concurrently;
      * hard <= soft        → SIGKILL before the graceful soft-limit unwind;
      * soft <= ceiling     → the process-level kill beats the step deadline and
        StepDeadlineExceeded becomes dead code.
    """
    assert (
        CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
        > CELERY_LEASE_HARD_TIME_LIMIT_SECONDS
        > CELERY_LEASE_SOFT_TIME_LIMIT_SECONDS
        > MAX_MAX_RUNTIME_SECONDS
    )


def test_celery_app_actually_applies_the_visibility_timeout():
    """The constant is worthless unless the transport is configured with it.

    Kombu's Redis default is 3600s; leaving ``broker_transport_options`` unset
    is precisely the bug — so assert the app carries the derived value, and
    that it is nowhere near the default.
    """
    options = celery_app.conf.broker_transport_options or {}
    assert options.get("visibility_timeout") == CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
    assert options["visibility_timeout"] > 3600

    result_options = celery_app.conf.result_backend_transport_options or {}
    assert (
        result_options.get("visibility_timeout")
        == CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS
    )


# ══ Hazard 2 — work and control plane are different queues ════════════

def _registered_task_names() -> set[str]:
    celery_app.loader.import_default_modules()
    return set(celery_app.tasks)


def test_every_registered_task_resolves_to_exactly_one_declared_queue():
    """The registry is exhaustive over what the app actually registers.

    Asserted against the declared mapping, never by parsing task names: a task
    called ``ops.log_scan`` and a task called ``ops.purge_soft_deleted_users``
    share a prefix and belong on opposite queues, so prefix matching would be
    wrong even where it looks tidy.
    """
    registered = _registered_task_names()
    declared = set(TASK_QUEUES) | set(CELERY_BUILTIN_TASK_QUEUES)

    undeclared = sorted(registered - declared)
    assert not undeclared, (
        "these Celery tasks have no queue declaration — add them to "
        f"packages/core/queues.py::TASK_QUEUES: {undeclared}"
    )

    stale = sorted(declared - registered)
    assert not stale, (
        "packages/core/queues.py declares queues for tasks that no longer "
        f"exist: {stale}"
    )

    for name in registered:
        assert isinstance(queue_for_task(name), CeleryQueue)


def test_control_plane_beats_and_execute_lease_land_in_different_queues():
    """The whole point: a long step cannot occupy a control-plane slot."""
    execute_lease_queue = queue_for_task(
        "packages.core.tasks.ai_tasks.execute_lease"
    )
    control_beats = (
        "packages.core.tasks.ai_tasks.internal_worker_tick",
        "packages.core.tasks.ai_tasks.cleanup_expired_leases",
        "scheduler.tick",
    )

    assert execute_lease_queue is CeleryQueue.WORK
    for beat in control_beats:
        assert queue_for_task(beat) is CeleryQueue.CONTROL
        assert queue_for_task(beat) is not execute_lease_queue


def test_every_beat_entry_names_a_declared_task():
    """A beat entry pointing at an unregistered name would never fire."""
    declared = set(TASK_QUEUES) | set(CELERY_BUILTIN_TASK_QUEUES)
    scheduled = {
        entry["task"] for entry in celery_app.conf.beat_schedule.values()
    }
    assert scheduled <= declared


def test_a_task_without_a_queue_declaration_falls_back_to_work():
    """Policy for the gap: fail the suite, and meanwhile run it on WORK.

    The registry is mandatory — the coverage test above fails on any missing
    entry. But between "someone added a task" and "someone declared it", the
    runtime still has to route it somewhere, and WORK is the safe side: an
    undeclared *heavy* task on the control plane is exactly the stall this
    module exists to prevent, whereas an undeclared *control* task on the work
    queue merely runs with normal work latency.
    """
    unknown = "packages.core.tasks.some_future_module.brand_new_task"
    assert unknown not in TASK_QUEUES
    assert UNDECLARED_TASK_QUEUE is CeleryQueue.WORK
    assert queue_for_task(unknown) is CeleryQueue.WORK
    assert route_task(unknown) == {"queue": CeleryQueue.WORK.value}


def test_declared_queues_are_wired_into_the_app_with_distinct_routing_keys():
    """Redis resolves a direct-exchange message by ROUTING KEY, not queue name.

    Celery's default routing key is "celery"; a ``Queue("work")`` declared
    without an explicit routing key would therefore deliver every work task
    straight back into the control plane's list. This asserts the queues are
    declared, and that the router hands each task a queue whose routing key is
    its own name.
    """
    configured = celery_app.amqp.queues
    for queue in CeleryQueue:
        assert queue.value in configured
        assert configured[queue.value].routing_key == queue.value

    routed = celery_app.amqp.router.route(
        {}, "packages.core.tasks.ai_tasks.execute_lease",
    )["queue"]
    assert (routed.name, routed.routing_key) == (
        CeleryQueue.WORK.value, CeleryQueue.WORK.value,
    )


def test_default_queue_is_the_control_plane_so_a_bare_worker_still_ticks():
    """``celery worker`` with no -Q consumes ``task_default_queue``.

    Keeping that pointed at the historical queue name means an un-flagged
    worker keeps running the orchestration loop after this change; only the
    work tasks moved, and the deploy that consumes ``work`` ships with them.
    """
    assert celery_app.conf.task_default_queue == CeleryQueue.CONTROL.value
    assert CeleryQueue.CONTROL.value == "celery"


# ══ Hazard 3 — execution rights are a database claim ══════════════════

async def _seed_lease(
    db,
    *,
    params: dict | None = None,
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
            risk_level="low", attempt_count=1, max_attempts=3,
            current_lease_id=lease_id, started_at=now,
        ),
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status="active",
            lease_until=now + timedelta(seconds=lease_seconds),
            last_heartbeat_at=now,
        ),
    ])
    await db.commit()
    return {
        "entity_id": entity_id, "plan_id": plan_id, "step_id": step_id,
        "lease_id": lease_id, "worker_id": worker_id,
    }


async def _reload(db, seeded) -> tuple[ExecutionStep, WorkLease]:
    db.expire_all()
    step = await db.get(ExecutionStep, seeded["step_id"])
    lease = await db.get(WorkLease, seeded["lease_id"])
    return step, lease


async def _age_heartbeat(db, lease_id: str, *, seconds: float) -> None:
    """Simulate a claimant whose heartbeat stopped ``seconds`` ago.

    ``last_heartbeat_at`` and ``lease_until`` move together because the
    heartbeat writes them in one statement (``_EXTEND_ACTIVE_LEASE_SQL`` in
    packages/core/workers/internal.py): ``lease_until`` IS the deadline that
    heartbeat was maintaining. Ageing only one of them would model a state the
    heartbeat can never produce.
    """
    await db.execute(
        text(
            """
            UPDATE work_leases
               SET last_heartbeat_at = :beat,
                   lease_until = :until
             WHERE id = :lease_id
            """
        ),
        {
            "lease_id": lease_id,
            "beat": datetime.now(timezone.utc) - timedelta(seconds=seconds),
            "until": datetime.now(timezone.utc) - timedelta(seconds=seconds),
        },
    )
    await db.commit()


@pytest.mark.asyncio
async def test_two_concurrent_deliveries_execute_the_step_exactly_once(
    db_session, monkeypatch,
):
    """The redelivery scenario, end to end: one runs, the other no-ops."""
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})
    runs: list[str] = []

    async def _body(snapshot):
        runs.append(snapshot["lease_id"])
        await asyncio.sleep(0.2)
        return {"result": {"text": "done"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _body)

    first, second = await asyncio.gather(
        internal.execute_lease_inproc(seeded["lease_id"]),
        internal.execute_lease_inproc(seeded["lease_id"]),
    )

    assert runs == [seeded["lease_id"]], "the handler must run exactly once"

    outcomes = [first, second]
    executed = [o for o in outcomes if not o.get("skipped")]
    skipped = [o for o in outcomes if o.get("skipped")]
    assert len(executed) == 1 and len(skipped) == 1
    assert executed[0]["outcome"] == "completed"
    # Denied because a live claimant held it — not because the lease had
    # already gone terminal (that would mean the race merely got lucky on
    # timing rather than being closed).
    assert skipped[0]["reason"] == CLAIM_DENIED_HELD

    step, lease = await _reload(db_session, seeded)
    assert step.step_status == "done"
    assert step.attempt_count == 1, "the skipped delivery must not touch the step"
    assert step.error is None
    assert lease.status == "completed"


@pytest.mark.asyncio
async def test_the_skipped_delivery_leaves_a_failing_step_untouched(
    db_session, monkeypatch,
):
    """A no-op really is a no-op, including on the failure path."""
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})

    async def _body(snapshot):
        await asyncio.sleep(0.2)
        raise RuntimeError("boom")

    monkeypatch.setattr(internal, "_execute_by_kind", _body)

    first, second = await asyncio.gather(
        internal.execute_lease_inproc(seeded["lease_id"]),
        internal.execute_lease_inproc(seeded["lease_id"]),
    )
    skipped = next(o for o in (first, second) if o.get("skipped"))
    assert skipped["reason"] == CLAIM_DENIED_HELD

    step, _lease = await _reload(db_session, seeded)
    # One failure recorded, not two: the step went back to pending with its
    # single attempt consumed by the one execution that held the claim.
    assert step.error["type"] == "RuntimeError"
    assert step.step_status == "pending"
    assert step.attempt_count == 1


@pytest.mark.asyncio
async def test_a_claim_whose_heartbeat_lapsed_is_reclaimable_and_runs_once(
    db_session, monkeypatch,
):
    """Worker lost mid-flight: the lease outlives the process that held it.

    The heartbeat thread dies with the process but the row stays ``active``
    until its TTL, so the redelivery must be able to take over — and must then
    run exactly once, not race the ghost.
    """
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})
    dead_claim = generate_ulid()

    await db_session.execute(
        text(
            "UPDATE work_leases SET execution_claim_id = :claim, "
            "execution_claimed_at = :at WHERE id = :lease_id"
        ),
        {
            "claim": dead_claim,
            "at": datetime.now(timezone.utc) - timedelta(seconds=600),
            "lease_id": seeded["lease_id"],
        },
    )
    await db_session.commit()

    # While the ghost's heartbeat is still fresh, nobody may take over.
    blocked = await internal.execute_lease_inproc(seeded["lease_id"])
    assert blocked == {
        "lease_id": seeded["lease_id"],
        "skipped": True,
        "reason": CLAIM_DENIED_HELD,
    }

    # Heartbeat lapses past the lease TTL → reclaimable.
    await _age_heartbeat(db_session, seeded["lease_id"], seconds=60)

    runs: list[str] = []

    async def _body(snapshot):
        runs.append(snapshot["lease_id"])
        return {"result": {"text": "recovered"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _body)
    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "completed"
    assert runs == [seeded["lease_id"]]

    step, lease = await _reload(db_session, seeded)
    assert step.step_status == "done"
    assert lease.execution_claim_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["success", "exception", "deadline"])
async def test_the_claim_is_released_on_every_exit_path(
    db_session, monkeypatch, exit_path,
):
    """A claim that outlived its execution would deadlock the retry.

    ``execute_lease`` retries through Celery, and ``fail_lease`` puts the step
    back to pending for a fresh checkout — neither can proceed if the previous
    invocation left its claim on the row.
    """
    budget = 0.05 if exit_path == "deadline" else 30

    async def _success(snapshot):
        return {"result": {"text": "ok"}}

    async def _exception(snapshot):
        raise RuntimeError("handler blew up")

    async def _slow(snapshot):
        await asyncio.sleep(30)

    monkeypatch.setattr(internal, "_execute_by_kind", {
        "success": _success, "exception": _exception, "deadline": _slow,
    }[exit_path])

    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": budget})
    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == (
        "completed" if exit_path == "success" else "failed"
    )
    if exit_path == "deadline":
        assert outcome["error"]["type"] == "StepDeadlineExceeded"

    _step, lease = await _reload(db_session, seeded)
    assert lease.execution_claim_id is None
    assert lease.execution_claimed_at is None


@pytest.mark.asyncio
async def test_claim_is_refused_once_the_lease_is_no_longer_active(db_session):
    """A terminal lease grants nobody execution rights."""
    seeded = await _seed_lease(db_session)
    await db_session.execute(
        text("UPDATE work_leases SET status = 'completed' WHERE id = :lease_id"),
        {"lease_id": seeded["lease_id"]},
    )
    await db_session.commit()

    claim = await claim_lease_for_execution(
        db_session, seeded["lease_id"], claim_id=generate_ulid(),
    )
    await db_session.commit()
    assert not claim
    assert claim.reason == CLAIM_DENIED_NOT_ACTIVE


@pytest.mark.asyncio
async def test_release_only_clears_a_claim_we_still_hold(db_session):
    """A straggler must not wipe the reclaiming execution's claim.

    Sequence: A claims → A's heartbeat lapses → B reclaims → A finally unwinds
    and releases. If A's release were unconditional it would hand the row back
    to the world while B is still executing.
    """
    seeded = await _seed_lease(db_session)
    straggler, reclaimer = generate_ulid(), generate_ulid()

    assert await claim_lease_for_execution(
        db_session, seeded["lease_id"], claim_id=straggler,
    )
    await db_session.commit()

    await _age_heartbeat(db_session, seeded["lease_id"], seconds=60)
    assert await claim_lease_for_execution(
        db_session, seeded["lease_id"], claim_id=reclaimer,
    )
    await db_session.commit()

    assert not await release_execution_claim(
        db_session, seeded["lease_id"], claim_id=straggler,
    )
    await db_session.commit()

    _step, lease = await _reload(db_session, seeded)
    assert lease.execution_claim_id == reclaimer

    assert await release_execution_claim(
        db_session, seeded["lease_id"], claim_id=reclaimer,
    )
    await db_session.commit()
    _step, lease = await _reload(db_session, seeded)
    assert lease.execution_claim_id is None
