"""Canonical row-lock ordering for Workspace-scoped Goal mutations."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.goal import Goal
from packages.core.models.workspace import Workspace


async def lock_workspace_for_goal_mutation(
    db: AsyncSession,
    *,
    workspace_id: str | None,
    entity_id: str,
) -> Optional[Workspace]:
    """Lock the owning Workspace before any of its Goal rows."""
    if not workspace_id:
        return None
    return (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        ).with_for_update()
    )).scalar_one_or_none()


async def lock_goal_for_mutation(
    db: AsyncSession,
    goal_id: str,
    *,
    entity_id: str | None = None,
) -> Optional[Goal]:
    """Lock Workspace then Goal, rechecking that the Goal still exists."""
    filters = [Goal.id == goal_id]
    if entity_id is not None:
        filters.append(Goal.entity_id == entity_id)

    # Do not flush a caller's pending Goal changes before the Workspace lock
    # establishes the shared Workspace -> Goal ordering.
    with db.no_autoflush:
        owner = (await db.execute(
            select(Goal.entity_id, Goal.workspace_id).where(*filters)
        )).one_or_none()
        if owner is None:
            return None
        workspace = await lock_workspace_for_goal_mutation(
            db,
            workspace_id=owner.workspace_id,
            entity_id=owner.entity_id,
        )
        if owner.workspace_id and workspace is None:
            return None
        locked = (await db.execute(
            select(Goal)
            .where(*filters)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        return locked
