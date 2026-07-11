"""Entity and workspace service — CRUD operations."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_access import settings_with_default_workspace_access


# How many days a soft-deleted workspace stays recoverable before the
# nightly purge task hard-deletes it. Override via env for staging.
WORKSPACE_PURGE_GRACE_DAYS = int(os.getenv("WORKSPACE_PURGE_GRACE_DAYS", "30"))


# ── Entity ──

async def get_entity(db: AsyncSession, entity_id: str) -> Optional[Entity]:
    # Check cache first
    cached = await cache.get(f"entity:{entity_id}")
    if cached is not None:
        return cached

    result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None)))
    entity = result.scalar_one_or_none()
    if entity:
        await cache.set(f"entity:{entity_id}", {
            "id": entity.id,
            "name": entity.name,
            "settings": entity.settings,
        }, ttl=300)
    return entity


async def update_entity(db: AsyncSession, entity_id: str, **fields) -> Optional[Entity]:
    entity = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None)))
    entity = entity.scalar_one_or_none()
    if not entity:
        return None
    for k, v in fields.items():
        if hasattr(entity, k) and v is not None:
            setattr(entity, k, v)
    await db.flush()
    # Invalidate cache
    await cache.delete(f"entity:{entity_id}")
    return entity


# ── Workspace ──

async def list_workspaces(db: AsyncSession, entity_id: str) -> list[Workspace]:
    result = await db.execute(
        select(Workspace)
        .where(Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.created_at.desc())
    )
    return list(result.scalars().all())


async def get_workspace(db: AsyncSession, workspace_id: str, entity_id: str) -> Optional[Workspace]:
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_workspace(
    db: AsyncSession,
    entity_id: str,
    *,
    name: str,
    description: str = "",
    category: str = "",
    address: str = "",
    kind: str = "",
    operating_context: str = "",
    primary_work: str = "",
    **fields,
) -> Workspace:
    workspace_settings = settings_with_default_workspace_access(fields.pop("settings", None))
    ws = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        description=description or None,
        category=category or None,
        address=address or None,
        kind=kind or None,
        operating_context=operating_context or None,
        primary_work=primary_work or None,
        settings=workspace_settings,
    )
    for k, v in fields.items():
        if hasattr(ws, k) and v is not None:
            setattr(ws, k, v)
    db.add(ws)
    await db.flush()
    return ws


async def update_workspace(db: AsyncSession, workspace_id: str, entity_id: str, **fields) -> Optional[Workspace]:
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None
    framing_fields = {"name", "primary_work", "operating_context"}
    framing_touched = False
    for k, v in fields.items():
        if hasattr(ws, k) and v is not None:
            old = getattr(ws, k, None)
            if k in framing_fields and old != v:
                framing_touched = True
            setattr(ws, k, v)
    await db.flush()
    if framing_touched:
        # Subscriptions cache an auto-generated identity blurb. When the
        # workspace's name / primary_work / operating_context changes, those
        # blurbs go stale and the agent introduces itself with the wrong
        # name.
        from packages.core.services.workspace_operation_service import (
            refresh_workspace_subscription_framings,
        )
        await refresh_workspace_subscription_framings(db, ws)
    await db.refresh(ws)
    return ws


async def soft_delete_workspace(
    db: AsyncSession, workspace_id: str, entity_id: str,
) -> bool:
    """Mark a workspace as deleted but keep all rows on disk.

    The nightly ``ops.purge_soft_deleted_workspaces`` task hard-deletes
    workspaces whose ``deleted_at`` is older than
    ``WORKSPACE_PURGE_GRACE_DAYS``. Until then, ``restore_workspace``
    can flip it back to active.
    """
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return False
    ws.deleted_at = datetime.now(timezone.utc)
    from packages.core.services.workspace_runtime import remove_workspace_runtime_schedules
    await remove_workspace_runtime_schedules(db, workspace_id)
    await db.flush()
    return True


async def restore_workspace(
    db: AsyncSession, workspace_id: str, entity_id: str,
) -> Optional[Workspace]:
    """Undo a soft delete — only succeeds if the workspace hasn't been
    purged yet. Returns the restored row or None if not found / already
    purged."""
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_not(None),
        )
    )
    ws = result.scalar_one_or_none()
    if not ws:
        return None
    ws.deleted_at = None
    from packages.core.services.workspace_runtime import sync_workspace_runtime_schedules
    await sync_workspace_runtime_schedules(db, ws)
    await db.flush()
    await db.refresh(ws)
    return ws


async def list_trashed_workspaces(
    db: AsyncSession, entity_id: str,
) -> list[Workspace]:
    """Workspaces in the 30-day soft-delete grace window."""
    result = await db.execute(
        select(Workspace)
        .where(
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_not(None),
        )
        .order_by(Workspace.deleted_at.desc())
    )
    return list(result.scalars().all())


async def list_workspaces_due_for_purge(
    db: AsyncSession, *, grace_days: int = WORKSPACE_PURGE_GRACE_DAYS,
) -> list[Workspace]:
    """All soft-deleted workspaces whose grace window has expired.
    Used by the nightly purge Celery task."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    result = await db.execute(
        select(Workspace).where(
            Workspace.deleted_at.is_not(None),
            Workspace.deleted_at < cutoff,
        )
    )
    return list(result.scalars().all())


async def purge_workspace(db: AsyncSession, workspace_id: str) -> bool:
    """Hard-delete a workspace and cascade-clean ALL related rows.

    Called by the nightly purge task once the grace window elapses, or
    by tests / admin tools that need an immediate hard delete. Bypasses
    entity scoping intentionally — by the time we reach here, the
    workspace is already marked deleted.

    Deletion order matters for FK constraints — children first, then
    parents. Group by dependency level:
      1. Leaf rows (logs, messages, steps, leases, sub-worker bindings)
      2. Mid-level (tasks, conversations, plans, subscriptions, channels)
      3. Top-level (workspace itself)
    """
    from sqlalchemy import delete as sa_delete, select as sa_select
    from packages.core.models.workspace import AgentSubscription, WorkspaceStaff, WorkspaceActivity
    from packages.core.models.task import Task, TaskLog, Conversation, Message
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob, AgentExecution
    from packages.core.models.memory import AgentMemory
    from packages.core.models.document import DocumentGroup, Channel
    from packages.core.models.channel import ChannelConfig, Announcement
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import WorkLease, SubscriptionWorker
    from packages.core.models.governance import GovernancePolicy, GovernanceRevision

    ws = await db.get(Workspace, workspace_id)
    if not ws:
        return False

    # ── Level 1: Leaf rows that FK into mid-level tables ──

    # Task logs (FK → task_id)
    task_ids_q = sa_select(Task.id).where(Task.workspace_id == workspace_id)
    await db.execute(sa_delete(TaskLog).where(TaskLog.task_id.in_(task_ids_q)))

    # Messages (FK → conversation_id)
    conv_ids_q = sa_select(Conversation.id).where(Conversation.workspace_id == workspace_id)
    await db.execute(sa_delete(Message).where(Message.conversation_id.in_(conv_ids_q)))

    # Execution steps (FK → plan_id)
    plan_ids_q = sa_select(ExecutionPlan.id).where(ExecutionPlan.workspace_id == workspace_id)
    await db.execute(sa_delete(ExecutionStep).where(ExecutionStep.plan_id.in_(plan_ids_q)))

    # SubscriptionWorker bindings (FK → subscription_id)
    sub_ids_q = sa_select(AgentSubscription.id).where(AgentSubscription.workspace_id == workspace_id)
    await db.execute(sa_delete(SubscriptionWorker).where(SubscriptionWorker.subscription_id.in_(sub_ids_q)))

    # Work leases (workspace_id)
    await db.execute(sa_delete(WorkLease).where(WorkLease.workspace_id == workspace_id))

    # ── Level 2: Mid-level rows with workspace_id ──
    for model in [
        Task, Conversation, ExecutionPlan,
        GovernancePolicy, GovernanceRevision, AgentExecution,
        WorkspaceStaff, AgentSubscription, WorkspaceActivity,
        Goal, ScheduledJob, AgentMemory,
        DocumentGroup, Channel, ChannelConfig, Announcement,
    ]:
        await db.execute(sa_delete(model).where(model.workspace_id == workspace_id))

    # ── Level 3: Workspace itself ──
    await db.delete(ws)
    await db.flush()
    return True


# Back-compat alias: a few callers import ``delete_workspace``. Route
# them to the soft-delete path so behaviour stays consistent across
# the codebase. New callers should pick the explicit name.
delete_workspace = soft_delete_workspace
