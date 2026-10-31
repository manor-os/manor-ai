"""Concurrent service bindings for one Workspace service converge on one row."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workspace_service import map_agent_to_service


@pytest.fixture
async def committed_entities(db_session):
    entity_ids: list[str] = []
    yield entity_ids
    await db_session.rollback()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with sessions() as db:
        for entity_id in entity_ids:
            for model in (AgentSubscription, Agent, Workspace):
                await db.execute(delete(model).where(model.entity_id == entity_id))
        await db.commit()


async def _scope(db, committed):
    entity_id = generate_ulid()
    # Existing workspace folder: no lazy file/folder initialization in this
    # concurrency test. Exercise a genuinely new subscription, not row edits.
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Mapping fence",
        status="active", artifact_folder_id=generate_ulid(),
    )
    newcomer = Agent(id=generate_ulid(), entity_id=entity_id, name="New service", status="active")
    db.add_all([workspace, newcomer])
    await db.flush()
    committed.append(entity_id)
    await db.commit()
    return workspace, newcomer


@pytest.mark.parametrize("concurrency", [2, 8, 32])
async def test_parallel_same_service_mapping_creates_one_subscription(db_session, committed_entities, concurrency):
    workspace, newcomer = await _scope(db_session, committed_entities)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    start = asyncio.Event()

    async def contender():
        await start.wait()
        async with sessions() as db:
            subscription = await map_agent_to_service(
                db, workspace.id, workspace.entity_id, "research", newcomer.id,
            )
            await db.commit()
            return subscription.id

    tasks = [asyncio.create_task(contender()) for _ in range(concurrency)]
    start.set()
    identities = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    assert len(set(identities)) == 1
    stored = (await db_session.execute(select(AgentSubscription.id).where(
        AgentSubscription.workspace_id == workspace.id,
        AgentSubscription.service_key == "research",
    ))).scalars().all()
    assert stored == [identities[0]]
