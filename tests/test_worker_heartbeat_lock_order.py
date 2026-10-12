"""Heartbeat lock ordering and worker-scoped lease admission regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.routers.workers import (
    HeartbeatCapacity,
    HeartbeatRequest,
    _heartbeat_inner,
    _heartbeat_requires_worker_write,
    _merge_worker_capabilities,
)
from packages.core.constants.execution import WorkLeaseStatus
from packages.core.workers.protocol import LEASE_ATTEMPT_TOKEN_CAPABILITY
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import Workspace


async def _seed_worker_capacity_scenario(
    db: AsyncSession,
    *,
    step_count: int = 2,
) -> tuple[str, list[str], list[str]]:
    entity_id = generate_ulid()
    from tests.approval_settings_test_utils import action_rule, initialize_test_account_approval

    await initialize_test_account_approval(db, entity_id=entity_id,
        user_rules=[action_rule("unclassified.action")])
    worker_id = generate_ulid()
    db.add(
        Worker(
            id=worker_id,
            entity_id=entity_id,
            kind="custom_http",
            display_name="Capacity worker",
            capabilities={
                "supported_kinds": ["llm"],
                "max_concurrent_leases": 1,
                "max_risk_level": "high",
            },
            preferences={},
            status="active",
            consecutive_failures=0,
            monthly_spent_usd=Decimal("0"),
            auto_pause_on_budget=True,
        )
    )
    step_ids: list[str] = []
    plan_ids: list[str] = []
    for index in range(step_count):
        plan_id = generate_ulid()
        step_id = generate_ulid()
        plan_ids.append(plan_id)
        step_ids.append(step_id)
        db.add_all(
            [
                ExecutionPlan(
                    id=plan_id,
                    entity_id=entity_id,
                    status="running",
                    execution_mode="live",
                    approval_required=False,
                    plan_dag={"steps": []},
                ),
                ExecutionStep(
                    id=step_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key=f"step_{index}",
                    kind="llm",
                    params={"prompt": f"work {index}"},
                    depends_on=[],
                    step_status="pending",
                    risk_level="low",
                    attempt_count=0,
                    max_attempts=3,
                ),
            ]
        )
    await db.commit()
    return worker_id, step_ids, plan_ids


async def _checkout_and_commit(
    sessions: async_sessionmaker[AsyncSession],
    worker_id: str,
    *,
    requested: int,
    plan_id: str | None = None,
    checked_out: asyncio.Event | None = None,
    release_checkout: asyncio.Event | None = None,
) -> list[str]:
    async with sessions() as db:
        worker = await db.get(Worker, worker_id)
        assert worker is not None
        leases = await Dispatcher().checkout_steps_for_worker(
            db,
            worker,
            max_n=requested,
            plan_id=plan_id,
        )
        lease_ids = [lease.id for lease, _step in leases]
        if checked_out is not None:
            checked_out.set()
        if release_checkout is not None:
            await release_checkout.wait()
        await db.commit()
        return lease_ids


_NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _snapshot(**changes):
    return SimpleNamespace(**{
        "id": "worker-1",
        "status": "active",
        "version": "1.2.0",
        "last_heartbeat_at": _NOW - timedelta(seconds=5),
        "capabilities": {"supported_kinds": ["llm"], "runtime": {"version": "1.2.0"}},
        **changes,
    })


def _beat(**changes):
    return HeartbeatRequest(**{
        "version": "1.2.0",
        "capabilities": {"supported_kinds": ["llm"]},
        "capacity": HeartbeatCapacity(can_accept_leases=1),
        **changes,
    })


def test_unchanged_fresh_heartbeat_skips_the_worker_row_write():
    assert not _heartbeat_requires_worker_write(_beat(), _snapshot(), now=_NOW)


@pytest.mark.parametrize("beat_changes, worker_changes", [
    ({}, {"last_heartbeat_at": _NOW - timedelta(seconds=15)}),
    ({}, {"last_heartbeat_at": None}),
    ({}, {"status": "offline"}),
    ({}, {"status": "pairing"}),
    ({"version": "1.3.0"}, {}),
    ({"capabilities": {"supported_kinds": ["llm", "action"]}}, {}),
    ({"capabilities": {LEASE_ATTEMPT_TOKEN_CAPABILITY: 1}}, {}),
])
def test_changed_or_stale_heartbeat_still_writes_the_worker_row(beat_changes, worker_changes):
    assert _heartbeat_requires_worker_write(
        _beat(**beat_changes), _snapshot(**worker_changes), now=_NOW,
    )


def test_heartbeat_capability_report_cannot_raise_registered_lease_limit():
    merged = _merge_worker_capabilities(
        {
            "supported_kinds": ["llm"],
            "max_concurrent_leases": 1,
        },
        {
            "supported_kinds": ["llm", "action"],
            "max_concurrent_leases": 64,
        },
    )

    assert merged["supported_kinds"] == ["llm", "action"]
    assert merged["max_concurrent_leases"] == 1


@pytest.mark.asyncio
async def test_stale_heartbeat_cannot_erase_persisted_attempt_fencing_capability(
    db_session,
):
    """Two authenticated requests may hold stale copies of one Worker row."""

    worker_id = generate_ulid()
    entity_id = generate_ulid()
    db_session.add(
        Worker(
            id=worker_id,
            entity_id=entity_id,
            kind="custom_http",
            display_name="Capability race worker",
            capabilities={
                "supported_kinds": ["llm"],
                LEASE_ATTEMPT_TOKEN_CAPABILITY: 0,
            },
            preferences={},
            status="active",
            consecutive_failures=0,
            monthly_spent_usd=Decimal("0"),
            auto_pause_on_budget=True,
        )
    )
    await db_session.commit()
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as stale_db, sessions() as upgrade_db:
        stale_worker = await stale_db.get(Worker, worker_id)
        upgrade_worker = await upgrade_db.get(Worker, worker_id)
        assert stale_worker is not None
        assert upgrade_worker is not None

        await _heartbeat_inner(
            HeartbeatRequest(
                capabilities={LEASE_ATTEMPT_TOKEN_CAPABILITY: 1},
                capacity=HeartbeatCapacity(can_accept_leases=0),
            ),
            upgrade_worker,
            upgrade_db,
        )
        # This ORM instance was authenticated before the upgrade committed.
        assert stale_worker.capabilities[LEASE_ATTEMPT_TOKEN_CAPABILITY] == 0

        await _heartbeat_inner(
            HeartbeatRequest(
                capabilities={LEASE_ATTEMPT_TOKEN_CAPABILITY: 0},
                capacity=HeartbeatCapacity(can_accept_leases=0),
            ),
            stale_worker,
            stale_db,
        )

    async with sessions() as verify_db:
        stored = await verify_db.get(Worker, worker_id)
        assert stored is not None
        assert stored.capabilities[LEASE_ATTEMPT_TOKEN_CAPABILITY] == 1


@pytest.mark.asyncio
async def test_heartbeat_commits_worker_before_workspace_checkout(
    db_session,
    monkeypatch,
):
    """A completion can lock Workspace then Worker while checkout waits.

    The probe uses two real PostgreSQL sessions.  The heartbeat session first
    reaches the checkout boundary; a completion-shaped session then owns the
    Workspace row, lets checkout wait on it, and locks the same Worker.  This
    can finish only when heartbeat bookkeeping committed before checkout.  If
    the heartbeat still owns Worker through autoflush, the two sessions form
    Worker -> Workspace / Workspace -> Worker and the short lock timeout trips.
    """

    entity_id = generate_ulid()
    worker_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add_all(
        [
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="custom_http",
                display_name="Heartbeat lock-order worker",
                version="before",
                capabilities={"supported_kinds": ["action"]},
                preferences={},
                status="active",
                consecutive_failures=0,
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Heartbeat lock-order workspace",
                status="active",
                operating_model={},
                settings={},
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
        ]
    )
    await db_session.commit()
    worker = await db_session.get(Worker, worker_id)
    assert worker is not None

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    observed: dict[str, object] = {}

    async def checkout_probe(
        _dispatcher,
        heartbeat_db,
        current_worker,
        *,
        max_n=1,
        plan_id=None,
    ):
        del max_n, plan_id
        # Match checkout's first SELECT/autoflush.  Before the fix this flush
        # takes the Worker row lock; after the boundary it has nothing to do.
        await heartbeat_db.flush()

        async with sessions() as completion_db:
            committed_worker = await completion_db.get(Worker, current_worker.id)
            observed["heartbeat_committed"] = bool(
                committed_worker is not None
                and committed_worker.last_heartbeat_at is not None
                and committed_worker.version == "after"
            )

            await completion_db.execute(text("SET LOCAL lock_timeout = '750ms'"))
            await completion_db.execute(
                select(Workspace)
                .where(Workspace.id == workspace_id)
                .with_for_update()
            )

            workspace_waiter = asyncio.create_task(
                heartbeat_db.execute(
                    select(Workspace)
                    .where(Workspace.id == workspace_id)
                    .with_for_update()
                )
            )
            try:
                # Prove checkout is actually waiting on the completion-owned
                # Workspace before the latter asks for Worker.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(workspace_waiter),
                        timeout=0.1,
                    )

                worker_lock_error: Exception | None = None
                try:
                    await completion_db.execute(
                        select(Worker)
                        .where(Worker.id == current_worker.id)
                        .with_for_update()
                    )
                except Exception as exc:  # pragma: no cover - old lock order
                    worker_lock_error = exc
                    await completion_db.rollback()
                else:
                    await completion_db.commit()

                await asyncio.wait_for(workspace_waiter, timeout=2)
                observed["worker_lock_error"] = worker_lock_error
            finally:
                if completion_db.in_transaction():
                    await completion_db.rollback()
                if not workspace_waiter.done():
                    workspace_waiter.cancel()
                    await asyncio.gather(workspace_waiter, return_exceptions=True)

        return []

    monkeypatch.setattr(Dispatcher, "checkout_steps_for_worker", checkout_probe)

    response = await _heartbeat_inner(
        HeartbeatRequest(
            version="after",
            capacity=HeartbeatCapacity(can_accept_leases=1),
        ),
        worker,
        db_session,
    )

    assert response.new_leases == []
    assert observed["heartbeat_committed"] is True
    assert observed["worker_lock_error"] is None


@pytest.mark.asyncio
async def test_concurrent_checkout_respects_persisted_worker_capacity(db_session):
    worker_id, _step_ids, plan_ids = await _seed_worker_capacity_scenario(db_session)
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Hold the first scoped checkout open after its grant.  The second session
    # targets a different Plan, so Step-level SKIP LOCKED cannot hide a missing
    # worker-capacity fence.  It must wait for the worker admission lane, then
    # observe the committed ACTIVE lease and receive nothing.
    checked_out = asyncio.Event()
    release_checkout = asyncio.Event()
    first_checkout = asyncio.create_task(
        _checkout_and_commit(
            sessions,
            worker_id,
            requested=32,
            plan_id=plan_ids[0],
            checked_out=checked_out,
            release_checkout=release_checkout,
        )
    )
    await asyncio.wait_for(checked_out.wait(), timeout=2)
    second_checkout = asyncio.create_task(
        _checkout_and_commit(
            sessions,
            worker_id,
            requested=32,
            plan_id=plan_ids[1],
        )
    )
    second_waited = False
    try:
        await asyncio.wait_for(asyncio.shield(second_checkout), timeout=0.1)
    except asyncio.TimeoutError:
        second_waited = True
    finally:
        release_checkout.set()
    granted = await asyncio.gather(first_checkout, second_checkout)

    async with sessions() as verify_db:
        active = (await verify_db.execute(
            select(WorkLease).where(
                WorkLease.worker_id == worker_id,
                WorkLease.status == WorkLeaseStatus.ACTIVE.value,
            )
        )).scalars().all()

    assert second_waited is True
    assert sum(len(batch) for batch in granted) == 1
    assert len(active) == 1


@pytest.mark.asyncio
async def test_existing_active_lease_exhausts_worker_capacity(db_session):
    worker_id, step_ids, _plan_ids = await _seed_worker_capacity_scenario(db_session)
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    first = await _checkout_and_commit(sessions, worker_id, requested=1)
    assert len(first) == 1

    granted = await asyncio.gather(
        _checkout_and_commit(sessions, worker_id, requested=32),
        _checkout_and_commit(sessions, worker_id, requested=32),
    )

    async with sessions() as verify_db:
        active = (await verify_db.execute(
            select(WorkLease).where(
                WorkLease.worker_id == worker_id,
                WorkLease.status == WorkLeaseStatus.ACTIVE.value,
            )
        )).scalars().all()
        remaining = (await verify_db.execute(
            select(ExecutionStep).where(ExecutionStep.id.in_(step_ids))
        )).scalars().all()

    assert granted == [[], []]
    assert len(active) == 1
    assert sum(step.step_status == "pending" for step in remaining) == 1
