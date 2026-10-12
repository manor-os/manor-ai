"""New service bindings serialize with the V3 current-selection boundary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.services.task_coordinator_fence import lock_task_coordinator_admission
from packages.core.services.task_execution import resolve_task_execution
from packages.core.services.workspace_service import map_agent_to_service
from test_task_coordinator_executor_handoff import _seed
from test_task_coordinator_submission_fence import committed_scopes as committed_scopes


async def _scope(db, committed):
    seeded = await _seed(db, step_status="done", run_phase="running")
    # Existing workspace folder: no lazy file/folder initialization in this
    # concurrency test. Exercise a genuinely new subscription, not row edits.
    seeded.workspace.artifact_folder_id = generate_ulid()
    newcomer = Agent(id=generate_ulid(), entity_id=seeded.task.entity_id, name="New service", status="active")
    db.add(newcomer)
    await db.flush()
    committed.append((seeded.task.id, seeded.plan.id, seeded.task.entity_id))
    await db.commit()
    return seeded, newcomer


@pytest.mark.parametrize("delete_while_waiting", [False, True])
async def test_new_binding_waits_for_submission_boundary(db_session, committed_scopes, delete_while_waiting):
    seeded, newcomer = await _scope(db_session, committed_scopes)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    task, selection = await lock_task_coordinator_admission(db_session, task_id=seeded.task.id)

    async def map_new():
        async with sessions() as db:
            subscription = await map_agent_to_service(
                db, seeded.workspace.id, seeded.task.entity_id, "research", newcomer.id,
            )
            await db.commit()
            return subscription.id

    pending = asyncio.create_task(map_new())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), timeout=0.2)
        if delete_while_waiting:
            seeded.workspace.deleted_at = datetime.now(timezone.utc)
            await db_session.commit()
            with pytest.raises(ValueError, match="Workspace not found"):
                await asyncio.wait_for(pending, timeout=3)
        else:
            await db_session.commit()
            assert await asyncio.wait_for(pending, timeout=3)
            current = await resolve_task_execution(db_session, task)
            assert current.coordinator.stable_fingerprint() != selection.stable_fingerprint()
    finally:
        await db_session.rollback()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("concurrency", [2, 8, 32])
async def test_parallel_same_service_mapping_creates_one_subscription(db_session, committed_scopes, concurrency):
    seeded, newcomer = await _scope(db_session, committed_scopes)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    start = asyncio.Event()

    async def contender():
        await start.wait()
        async with sessions() as db:
            subscription = await map_agent_to_service(
                db, seeded.workspace.id, seeded.task.entity_id, "research", newcomer.id,
            )
            await db.commit()
            return subscription.id

    tasks = [asyncio.create_task(contender()) for _ in range(concurrency)]
    start.set()
    identities = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    assert len(set(identities)) == 1
    stored = (await db_session.execute(select(AgentSubscription.id).where(
        AgentSubscription.workspace_id == seeded.workspace.id,
        AgentSubscription.service_key == "research",
    ))).scalars().all()
    assert stored == [identities[0]]
