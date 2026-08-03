"""The lease heartbeat must survive a blocked event loop.

The heartbeat used to be an ``asyncio`` task sharing the step body's event
loop. That is fine only while every step yields: a body that blocks the loop —
CPU-bound work, a synchronous vendor SDK, blocking IO — starves the
heartbeat's ``await asyncio.sleep`` for exactly as long as it blocks. The lease
then sails past its 300s TTL, ``expire_leases`` reclaims it, and a step that is
alive and working gets retried underneath a running worker. With the per-step
budget now up to 6 hours, "the body blocks for longer than the lease TTL" stops
being exotic.

These tests pin the replacement — a dedicated OS thread with its own SYNC DB
session (the async engine's asyncpg connections belong to the main loop and
must not be used from another thread):

  * a body that blocks the loop past the lease TTL still gets its lease
    extended, and ``expire_leases`` finds nothing to reclaim;
  * the thread is joined on every exit path — normal return, exception, and
    deadline cancellation — so nothing leaks a thread per lease;
  * a lease that stops being ``active`` mid-flight stops the heartbeat quietly;
  * ``interval <= 0`` still disables the heartbeat entirely;
  * a blocked loop is *diagnosable*: the thread says so in the log.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.workers import internal


# ── fixtures / helpers ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_heartbeat_engine():
    """The heartbeat's sync engine is process-wide and lazily built.

    Drop it around every test so it is always built against the database this
    run is actually using, and never left holding connections afterwards.
    """
    internal._reset_lease_heartbeat_engine()
    yield
    internal._reset_lease_heartbeat_engine()


def _live_heartbeat_threads() -> list[threading.Thread]:
    return [
        t for t in threading.enumerate()
        if t.is_alive() and t.name.startswith("lease-heartbeat-")
    ]


async def _seed_lease(
    db,
    *,
    params: dict | None = None,
    lease_seconds: float = 300,
    status: str = "active",
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
            step_key="blocking_step", kind="llm",
            params=params or {}, depends_on=[], step_status="running",
            risk_level="low", attempt_count=1, max_attempts=3,
            current_lease_id=lease_id, started_at=now,
        ),
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status=status,
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


def _compress_heartbeat(monkeypatch, *, interval: float = 0.1, extend: float = 5.0):
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", interval)
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_EXTEND_SECONDS", extend)


def _spy_on_extends(monkeypatch) -> list[str]:
    """Record every sync extend the heartbeat thread performs."""
    extends: list[str] = []
    real_extend = internal.extend_active_lease_sync

    def _spy(lease_id: str, *, extra_seconds: float) -> bool:
        ok = real_extend(lease_id, extra_seconds=extra_seconds)
        extends.append(lease_id)
        return ok

    monkeypatch.setattr(internal, "extend_active_lease_sync", _spy)
    return extends


def _capture_heartbeats(monkeypatch) -> list:
    """Hold on to every heartbeat started, so leaks are provable."""
    started: list = []
    real_start = internal._start_lease_heartbeat

    def _spy(lease_id: str):
        heartbeat = real_start(lease_id)
        started.append(heartbeat)
        return heartbeat

    monkeypatch.setattr(internal, "_start_lease_heartbeat", _spy)
    return started


# ── the regression that matters ───────────────────────────────────────

@pytest.mark.asyncio
async def test_a_body_that_blocks_the_event_loop_still_keeps_its_lease(
    db_session, monkeypatch
):
    """A blocking step body must not lose its lease.

    ``time.sleep`` inside the async handler is the whole point: it is what a
    CPU-bound tool or a synchronous SDK call looks like to the event loop. The
    lease TTL here (0.3s) expires long before the body (0.7s) finishes, so an
    in-loop heartbeat cannot possibly run — this test fails against the old
    ``asyncio.create_task`` heartbeat and passes against the thread.
    """
    seeded = await _seed_lease(
        db_session, params={"max_runtime_seconds": 30}, lease_seconds=0.3,
    )
    _compress_heartbeat(monkeypatch, interval=0.1, extend=5.0)
    extends = _spy_on_extends(monkeypatch)
    reclaimable_while_running: list[bool] = []

    async def _loop_blocking_body(snapshot):
        # Blocks the loop well past the 0.3s lease TTL.
        time.sleep(0.7)
        # Still mid-step, still holding the lease: evaluate the dispatcher's
        # own reclaim predicate (``expire_leases``) against THIS lease.
        async with async_session() as db:
            hit = (await db.execute(
                select(WorkLease.id).where(
                    WorkLease.id == snapshot["lease_id"],
                    WorkLease.status == "active",
                    WorkLease.lease_until < datetime.now(timezone.utc),
                )
            )).scalar_one_or_none()
            reclaimable_while_running.append(hit is not None)
        return {"result": {"text": "done"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _loop_blocking_body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert extends.count(seeded["lease_id"]) >= 2, (
        "the heartbeat must keep extending while the event loop is blocked"
    )
    assert reclaimable_while_running == [False], (
        "cleanup_expired_leases would have reclaimed a live step's lease "
        "while its worker was still working"
    )
    assert outcome["outcome"] == "completed"

    step, lease = await _reload(db_session, seeded)
    assert step.step_status == "done"
    assert lease.status == "completed"
    assert lease.heartbeat_count >= 2
    assert lease.lease_until > seeded["lease_until"]


@pytest.mark.asyncio
async def test_a_blocked_event_loop_is_reported_not_silent(
    db_session, monkeypatch, caplog
):
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})
    _compress_heartbeat(monkeypatch, interval=0.1, extend=5.0)

    async def _loop_blocking_body(snapshot):
        time.sleep(0.7)
        return {"result": {"text": "done"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _loop_blocking_body)

    with caplog.at_level(logging.WARNING, logger=internal.logger.name):
        await internal.execute_lease_inproc(seeded["lease_id"])

    assert any(
        "event loop appears blocked" in record.getMessage()
        for record in caplog.records
    ), "a starved loop must be diagnosable from the log"


# ── no thread outlives its step ───────────────────────────────────────

@pytest.mark.asyncio
async def test_heartbeat_thread_is_joined_when_the_body_returns(
    db_session, monkeypatch
):
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})
    _compress_heartbeat(monkeypatch, interval=0.05)
    started = _capture_heartbeats(monkeypatch)
    before = threading.active_count()

    async def _quick_body(snapshot):
        await asyncio.sleep(0.12)
        return {"result": {"text": "ok"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _quick_body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "completed"
    assert len(started) == 1 and started[0] is not None
    assert started[0].thread.is_alive() is False
    assert _live_heartbeat_threads() == []
    assert threading.active_count() <= before


@pytest.mark.asyncio
async def test_heartbeat_thread_is_joined_when_the_body_raises(
    db_session, monkeypatch
):
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})
    _compress_heartbeat(monkeypatch, interval=0.05)
    started = _capture_heartbeats(monkeypatch)
    before = threading.active_count()

    async def _exploding_body(snapshot):
        await asyncio.sleep(0.12)
        raise RuntimeError("tool blew up")

    monkeypatch.setattr(internal, "_execute_by_kind", _exploding_body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    assert started[0].thread.is_alive() is False
    assert _live_heartbeat_threads() == []
    assert threading.active_count() <= before


@pytest.mark.asyncio
async def test_heartbeat_thread_is_joined_when_the_deadline_cancels_the_body(
    db_session, monkeypatch
):
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 0.15})
    _compress_heartbeat(monkeypatch, interval=0.05)
    started = _capture_heartbeats(monkeypatch)
    before = threading.active_count()

    async def _slow_body(snapshot):
        await asyncio.sleep(30)

    monkeypatch.setattr(internal, "_execute_by_kind", _slow_body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    assert outcome["error"]["type"] == "StepDeadlineExceeded"
    assert started[0].thread.is_alive() is False
    assert _live_heartbeat_threads() == []
    assert threading.active_count() <= before


# ── the heartbeat's own semantics ─────────────────────────────────────

@pytest.mark.asyncio
async def test_heartbeat_stops_when_the_lease_stops_being_active(db_session):
    """A lease that goes non-active mid-flight ends the heartbeat quietly."""
    seeded = await _seed_lease(db_session, lease_seconds=5)
    heartbeat = internal.LeaseHeartbeat(
        seeded["lease_id"], interval_seconds=0.05, extend_seconds=5,
    )
    heartbeat.start()
    try:
        await asyncio.sleep(0.15)
        _step, lease = await _reload(db_session, seeded)
        assert lease.heartbeat_count >= 1

        lease.status = "completed"
        await db_session.commit()

        # The thread notices on its next tick and returns on its own —
        # no exception, no stop() needed.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not heartbeat.thread.is_alive():
                break
        assert heartbeat.thread.is_alive() is False
    finally:
        heartbeat.stop()

    assert _live_heartbeat_threads() == []


@pytest.mark.asyncio
async def test_extend_active_lease_sync_is_a_no_op_on_a_dead_lease(db_session):
    seeded = await _seed_lease(db_session, status="expired")
    assert internal.extend_active_lease_sync(
        seeded["lease_id"], extra_seconds=60,
    ) is False
    assert internal.extend_active_lease_sync(
        generate_ulid(), extra_seconds=60,
    ) is False


@pytest.mark.asyncio
async def test_zero_interval_disables_the_heartbeat(db_session, monkeypatch):
    seeded = await _seed_lease(db_session, params={"max_runtime_seconds": 30})
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 0)
    started = _capture_heartbeats(monkeypatch)
    extends = _spy_on_extends(monkeypatch)

    async def _quick_body(snapshot):
        await asyncio.sleep(0.15)
        return {"result": {"text": "ok"}}

    monkeypatch.setattr(internal, "_execute_by_kind", _quick_body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "completed"
    assert started == [None]
    assert extends == []
    assert _live_heartbeat_threads() == []

    _step, lease = await _reload(db_session, seeded)
    assert (lease.heartbeat_count or 0) == 0
