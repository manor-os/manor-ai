"""Concurrency regressions for reusable resources during Workspace purge."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import Skill
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.agent_subscription_service import (
    ensure_default_subscription,
)
from packages.core.services.entity_service import purge_workspace, restore_workspace
from packages.core.services.reusable_resource_locks import (
    ReusableResourceUnavailableError,
    lock_reusable_resource_lifecycle,
    reusable_resource_reference_delta,
    reusable_resource_ids_from_payload,
)
from packages.core.services.task_service import create_task
from packages.core.services.scheduler_service import create_scheduled_job
from packages.core.services.workflow_service import update_binding


async def _wait_until_lock_blocked(
    observer: AsyncSession,
    *,
    backend_pid: int,
) -> None:
    for _ in range(100):
        wait_event_type = await observer.scalar(
            text(
                "SELECT wait_event_type FROM pg_stat_activity "
                "WHERE pid = :backend_pid"
            ),
            {"backend_pid": backend_pid},
        )
        if wait_event_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Concurrent resource operation never waited on the row lock")


async def _seed_workspace_agent(db: AsyncSession) -> tuple[str, str, str, str]:
    entity_id = generate_ulid()
    home = Workspace(
        entity_id=entity_id,
        name="Purged resource home",
        settings={},
        deleted_at=datetime.now(timezone.utc),
    )
    consumer = Workspace(
        entity_id=entity_id,
        name="Concurrent resource consumer",
        settings={},
    )
    db.add_all([home, consumer])
    await db.flush()
    agent = Agent(
        entity_id=entity_id,
        workspace_id=home.id,
        visibility="workspace",
        name="Concurrently reused Agent",
        system_prompt="Stay consistent while the home Workspace is purged.",
        status="active",
    )
    db.add(agent)
    await db.commit()
    return entity_id, home.id, consumer.id, agent.id


async def _seed_workspace_workflows(
    db: AsyncSession,
) -> tuple[str, str, str, str, str]:
    entity_id = generate_ulid()
    home = Workspace(
        entity_id=entity_id,
        name="Purged workflow home",
        settings={},
        deleted_at=datetime.now(timezone.utc),
    )
    consumer = Workspace(
        entity_id=entity_id,
        name="Concurrent workflow consumer",
        settings={},
    )
    db.add_all([home, consumer])
    await db.flush()
    owned = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=home.id,
        name="Home Flow",
        steps=[],
    )
    retained = WorkflowDefinition(
        entity_id=entity_id,
        name="Entity Flow",
        steps=[],
    )
    db.add_all([owned, retained])
    await db.flush()
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=retained.id,
        workspace_id=consumer.id,
        trigger_type="manual",
    )
    db.add(binding)
    await db.commit()
    return entity_id, home.id, owned.id, binding.id, retained.id


async def _seed_workspace_skill(
    db: AsyncSession,
) -> tuple[str, str, str, str]:
    entity_id = generate_ulid()
    home = Workspace(
        entity_id=entity_id,
        name="Purged skill home",
        settings={},
        deleted_at=datetime.now(timezone.utc),
    )
    consumer = Workspace(
        entity_id=entity_id,
        name="Concurrent skill consumer",
        settings={},
    )
    db.add_all([home, consumer])
    await db.flush()
    skill = Skill(
        entity_id=entity_id,
        workspace_id=home.id,
        name="Concurrently scheduled Skill",
        slug="concurrently-scheduled-skill",
        system_prompt="Remain valid while a schedule is created.",
        status="active",
    )
    db.add(skill)
    await db.commit()
    return entity_id, home.id, consumer.id, skill.id


@pytest.mark.asyncio
async def test_purge_wins_before_concurrent_agent_subscription(db_session):
    entity_id, home_id, consumer_id, agent_id = await _seed_workspace_agent(
        db_session
    )
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as purge_db, sessions() as writer_db, sessions() as observer:
        await lock_reusable_resource_lifecycle(
            purge_db,
            entity_id=entity_id,
        )
        writer_pid = int(await writer_db.scalar(text("SELECT pg_backend_pid()")))
        writer = asyncio.create_task(
            ensure_default_subscription(
                writer_db,
                entity_id=entity_id,
                agent_id=agent_id,
                workspace_id=consumer_id,
            )
        )
        await _wait_until_lock_blocked(observer, backend_pid=writer_pid)

        assert await purge_workspace(purge_db, home_id) is True
        await purge_db.commit()
        with pytest.raises(
            ReusableResourceUnavailableError,
            match="Agent is no longer available",
        ):
            await writer
        await writer_db.rollback()

    assert await db_session.get(Agent, agent_id) is None
    subscriptions = list((await db_session.execute(
        select(AgentSubscription).where(AgentSubscription.agent_id == agent_id)
    )).scalars().all())
    assert subscriptions == []


@pytest.mark.asyncio
async def test_concurrent_agent_subscription_wins_before_purge(db_session):
    entity_id, home_id, consumer_id, agent_id = await _seed_workspace_agent(
        db_session
    )
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as purge_db, sessions() as writer_db, sessions() as observer:
        subscription = await ensure_default_subscription(
            writer_db,
            entity_id=entity_id,
            agent_id=agent_id,
            workspace_id=consumer_id,
        )
        subscription_id = subscription.id
        purge_pid = int(await purge_db.scalar(text("SELECT pg_backend_pid()")))
        purge = asyncio.create_task(purge_workspace(purge_db, home_id))
        await _wait_until_lock_blocked(observer, backend_pid=purge_pid)

        await writer_db.commit()
        assert await purge is True
        await purge_db.commit()

    retained_agent = await db_session.get(Agent, agent_id)
    retained_subscription = await db_session.get(
        AgentSubscription,
        subscription_id,
    )
    assert retained_agent is not None
    assert retained_agent.workspace_id is None
    assert retained_subscription is not None
    assert retained_subscription.workspace_id == consumer_id


@pytest.mark.asyncio
async def test_purge_wins_before_concurrent_direct_task_reference(db_session):
    entity_id, home_id, consumer_id, agent_id = await _seed_workspace_agent(
        db_session
    )
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as purge_db, sessions() as writer_db, sessions() as observer:
        await lock_reusable_resource_lifecycle(purge_db, entity_id=entity_id)
        writer_pid = int(await writer_db.scalar(text("SELECT pg_backend_pid()")))
        writer = asyncio.create_task(
            create_task(
                writer_db,
                entity_id,
                title="Must not retain a purged Agent",
                workspace_id=consumer_id,
                agent_id=agent_id,
            )
        )
        await _wait_until_lock_blocked(observer, backend_pid=writer_pid)

        assert await purge_workspace(purge_db, home_id) is True
        await purge_db.commit()
        with pytest.raises(
            ReusableResourceUnavailableError,
            match="Agent is no longer available",
        ):
            await writer
        await writer_db.rollback()

    task_ids = list((await db_session.execute(
        select(Task.id).where(Task.agent_id == agent_id)
    )).scalars().all())
    assert task_ids == []


@pytest.mark.asyncio
async def test_purge_wins_before_concurrent_workflow_binding_reassignment(
    db_session,
):
    entity_id, home_id, owned_id, binding_id, retained_id = (
        await _seed_workspace_workflows(db_session)
    )
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as purge_db, sessions() as writer_db, sessions() as observer:
        await lock_reusable_resource_lifecycle(purge_db, entity_id=entity_id)
        writer_pid = int(await writer_db.scalar(text("SELECT pg_backend_pid()")))
        writer = asyncio.create_task(
            update_binding(
                writer_db,
                binding_id,
                entity_id,
                workflow_id=owned_id,
            )
        )
        await _wait_until_lock_blocked(observer, backend_pid=writer_pid)

        assert await purge_workspace(purge_db, home_id) is True
        await purge_db.commit()
        with pytest.raises(
            ReusableResourceUnavailableError,
            match="Workflow is no longer available",
        ):
            await writer
        await writer_db.rollback()

    binding = await db_session.get(WorkflowBinding, binding_id)
    assert binding is not None
    assert binding.workflow_id == retained_id
    assert await db_session.get(WorkflowDefinition, owned_id) is None


@pytest.mark.asyncio
async def test_purge_wins_before_concurrent_nested_scheduled_skill_reference(
    db_session,
):
    entity_id, home_id, consumer_id, skill_id = await _seed_workspace_skill(
        db_session
    )
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as purge_db, sessions() as writer_db, sessions() as observer:
        await lock_reusable_resource_lifecycle(purge_db, entity_id=entity_id)
        writer_pid = int(await writer_db.scalar(text("SELECT pg_backend_pid()")))
        writer = asyncio.create_task(
            create_scheduled_job(
                writer_db,
                entity_id,
                f"concurrent-skill:{skill_id}",
                "Concurrent nested Skill schedule",
                execution_type="skill",
                execution_target={"config": {"skill_id": skill_id}},
                workspace_id=consumer_id,
            )
        )
        await _wait_until_lock_blocked(observer, backend_pid=writer_pid)

        assert await purge_workspace(purge_db, home_id) is True
        await purge_db.commit()
        with pytest.raises(
            ReusableResourceUnavailableError,
            match="Skill is no longer available",
        ):
            await writer
        await writer_db.rollback()

    jobs = list((await db_session.execute(
        select(ScheduledJob.id).where(
            ScheduledJob.execution_target["config"]["skill_id"].astext
            == skill_id
        )
    )).scalars().all())
    assert jobs == []
    assert await db_session.get(Skill, skill_id) is None


@pytest.mark.asyncio
async def test_restore_wins_before_stale_purge_candidate(db_session):
    entity_id, home_id, _consumer_id, _agent_id = await _seed_workspace_agent(
        db_session
    )
    deleted_at = (await db_session.get(Workspace, home_id)).deleted_at
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as restore_db, sessions() as purge_db, sessions() as observer:
        restored = await restore_workspace(restore_db, home_id, entity_id)
        assert restored is not None
        purge_pid = int(await purge_db.scalar(text("SELECT pg_backend_pid()")))
        purge = asyncio.create_task(
            purge_workspace(
                purge_db,
                home_id,
                expected_deleted_at=deleted_at,
            )
        )
        await _wait_until_lock_blocked(observer, backend_pid=purge_pid)

        await restore_db.commit()
        assert await purge is False
        await purge_db.rollback()

    workspace = await db_session.get(Workspace, home_id)
    assert workspace is not None
    assert workspace.deleted_at is None


def test_payload_resource_ids_ignore_portable_historical_slugs():
    agent_id = generate_ulid()
    skill_id = generate_ulid()
    workflow_id = generate_ulid()
    assert reusable_resource_ids_from_payload({
        "agent_id": agent_id,
        "nested": [
            {"skill_id": skill_id},
            {"workflow_id": workflow_id},
            {"skill": "historical-skill-slug"},
            {"workflow_id": "portable-flow-slug"},
        ],
    }) == ({agent_id}, {skill_id}, {workflow_id})


def test_payload_delta_preserves_historical_missing_ids():
    historical_missing_id = generate_ulid()
    added_id = generate_ulid()
    assert reusable_resource_reference_delta(
        {"steps": [{"config": {"workflow_id": historical_missing_id}}]},
        {
            "steps": [
                {"config": {"workflow_id": historical_missing_id}},
                {"config": {"workflow_id": added_id}},
            ]
        },
    ) == (set(), set(), {added_id})
