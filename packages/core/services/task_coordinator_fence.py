"""Workspace-first admission shared by V3 coordinator mutations.

This is a short database boundary, not execution authority from a checkpoint.
Callers retain the locks through their mutation, then release them before any
model/provider work. Legacy Plan/Supervisor locking remains unchanged.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskRuntimeKind
from packages.core.models.task import Task
from packages.core.models.workspace import AgentSubscription
from packages.core.services.task_execution import CoordinatorRuntimeSelection, resolve_task_execution
from packages.core.services.workspace_access import lock_workspace_access_boundary
from packages.core.services.workspace_autonomy import workspace_allows_manual_work


async def lock_task_coordinator_admission(
    db: AsyncSession, *, task_id: str,
) -> tuple[Task, CoordinatorRuntimeSelection]:
    """Lock Workspace -> Task -> subscriptions and resolve today's factory.

    The caller next locks Plan -> Lease -> Step -> Run, and compares the
    returned selection against that locked Run's fingerprint and root IDs.
    """
    scope = (await db.execute(
        select(Task.entity_id, Task.workspace_id).where(Task.id == task_id)
    )).one_or_none()
    if scope is None or not scope.workspace_id:
        raise ValueError("Task coordinator Workspace is unavailable")
    workspace = await lock_workspace_access_boundary(
        db, workspace_id=scope.workspace_id, entity_id=scope.entity_id,
    )
    if not workspace_allows_manual_work(workspace):
        raise ValueError("Task coordinator Workspace no longer allows execution")
    task = (await db.execute(
        select(Task).where(Task.id == task_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.entity_id != scope.entity_id or task.workspace_id != scope.workspace_id:
        raise ValueError("Task coordinator Task scope changed during admission")
    # Lock inactive rows too: a paused deployment becoming active can make
    # the factory ambiguous. New binding creation must serialize on the same
    # Workspace boundary; row locks alone do not prevent phantom inserts.
    await db.execute(
        select(AgentSubscription.id).where(
            AgentSubscription.entity_id == task.entity_id,
            AgentSubscription.workspace_id == task.workspace_id,
        ).order_by(AgentSubscription.id).with_for_update()
    )
    execution = await resolve_task_execution(db, task, lock_subscriptions=True)
    if execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN or execution.coordinator is None:
        raise ValueError("Task coordinator factory selection is no longer available")
    return task, execution.coordinator
