"""Durable host mechanics of a Task run.

The Task agent loop (``workers.task_agent_loop``) owns its
``TaskCoordinatorRun`` through these helpers: admit the Workspace and Task,
claim the exact wake, renew the claim while provider I/O runs, and re-check
the claim before every model call.

Callers pass their session factory and lease settings explicitly so they keep
their own patch points and constants.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from packages.core.constants.task import TaskRuntimeKind, TaskStatus
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.models.workspace import Workspace
from packages.core.services.durable_run import holds_current_wake
from packages.core.services.task_coordinator_wakeup import (
    TaskCoordinatorClaim,
    claim_task_coordinator_wake,
    renew_task_coordinator_claim,
)
from packages.core.services.task_execution import (
    resolve_task_execution,
    task_execution_intent_generation,
)
from packages.core.services.task_execution_origin import task_has_manual_execution
from packages.core.services.workspace_access import lock_workspace_access_boundary
from packages.core.services.workspace_autonomy import workspace_allows_manual_work
from packages.core.services.workspace_readiness import (
    evaluate_workspace_blocking_setup,
    task_is_authorized_workspace_setup,
)


logger = logging.getLogger(__name__)

TASK_RUN_LEASE_SECONDS = 180.0
TASK_RUN_HEARTBEAT_SECONDS = 45.0

SessionFactory = Callable[[], Any]
# (db, claim, lease_seconds) -> whether this owner still holds its claim.
RenewClaim = Callable[[Any, Any, float], Awaitable[bool]]


def run_engine(run: Any) -> str:
    """The run's frozen engine; rows from before the column are DAG runs."""

    return str(getattr(run, "engine", None) or TaskCoordinatorEngine.PLAN_DAG.value)


async def admit_task_scope(db: Any, task_id: str) -> tuple[Task | None, str]:
    """Admit Workspace -> Task for one wake and lock the Task.

    Locks the Workspace access boundary (a Task assigned directly to an
    Agent has none), then the Task, in the canonical order. Returns the
    locked in-progress Task, or None and the reason the wake is deferred.
    Each run kind's claim follows inside the same transaction.
    """

    scope = (await db.execute(
        select(Task.entity_id, Task.workspace_id).where(Task.id == task_id)
    )).one_or_none()
    if scope is None:
        return None, "missing_task"
    workspace = None
    if scope.workspace_id:
        workspace = await lock_workspace_access_boundary(
            db, workspace_id=scope.workspace_id, entity_id=scope.entity_id,
        )
        if workspace is None or not workspace_allows_manual_work(workspace):
            return None, "workspace_unavailable"
    task = (await db.execute(
        select(Task).where(Task.id == task_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS.value:
        return None, "task_inactive"
    if workspace is None:
        # A Task assigned directly to an Agent has no Workspace autonomy or
        # setup gate.
        return task, "admitted"
    if workspace.status == "paused" and not task_has_manual_execution(task):
        # Pausing autonomy holds automatic Tasks (Strategist, schedules);
        # recovery redelivers the wake once the Workspace runs again.
        return None, "workspace_autonomy_paused"
    setup = await evaluate_workspace_blocking_setup(db, workspace)
    if setup is not None and setup.blocks_work and not await task_is_authorized_workspace_setup(db, task):
        # Blueprint setup must finish (through its authorized setup job)
        # before ordinary Tasks run; recovery redelivers the wake.
        return None, "workspace_setup_incomplete"
    return task, "admitted"


async def admit_and_claim(
    *,
    event_id: str,
    task_id: str,
    wake_generation: str,
    engine: str,
    lease_seconds: float,
    session_factory: SessionFactory,
) -> tuple[TaskCoordinatorClaim | None, str]:
    """Admit Workspace -> Task, then claim this exact wake for ``engine``.

    A run belongs to the engine frozen on its row. A stale or misrouted
    delivery for the other engine is deferred without touching the claim.
    """

    async with session_factory() as db:
        task, reason = await admit_task_scope(db, task_id)
        if task is None:
            return None, reason
        execution = await resolve_task_execution(db, task, lock_subscriptions=True)
        if execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN or execution.coordinator is None:
            return None, "factory_selection_unavailable"
        run = (await db.execute(
            select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
        )).scalar_one_or_none()
        if (
            run is None
            or getattr(run, "lifecycle_generation", None)
            != task_execution_intent_generation(task)
        ):
            return None, "stale_task_lifecycle"
        if run_engine(run) != engine:
            return None, "engine_mismatch"
        claim, reason = await claim_task_coordinator_wake(
            db,
            task_id=task_id,
            event_id=event_id,
            wake_generation=wake_generation,
            claim_token=generate_ulid(),
            selection=execution.coordinator,
            lease_seconds=lease_seconds,
        )
        if claim is not None:
            await db.commit()
        return claim, reason


async def claim_is_current(
    claim: TaskCoordinatorClaim,
    *,
    session_factory: SessionFactory,
) -> bool:
    """Whether this owner still holds the live claim on an admissible Task."""

    async with session_factory() as db:
        run = (await db.execute(
            select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == claim.run_id)
        )).scalar_one_or_none()
        task = (await db.execute(
            select(Task).where(Task.id == claim.task_id)
        )).scalar_one_or_none()
        if task is None or task.status != TaskStatus.IN_PROGRESS.value or run is None:
            return False
        if task.workspace_id:
            workspace = (await db.execute(
                select(Workspace).where(
                    Workspace.id == task.workspace_id,
                    Workspace.entity_id == task.entity_id,
                )
            )).scalar_one_or_none()
            if not workspace_allows_manual_work(workspace):
                return False
        execution = await resolve_task_execution(db, task)
        if (
            execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
            or execution.coordinator is None
            or execution.coordinator.stable_fingerprint() != run.selection_fingerprint
            or getattr(run, "lifecycle_generation", None)
            != task_execution_intent_generation(task)
        ):
            return False
        return bool(
            run.task_id == claim.task_id
            and holds_current_wake(run, claim.claim_token, claim.wake_generation)
            and run.phase not in TaskCoordinatorPhase.terminal()
        )


async def renew_task_coordinator_lease(
    db: Any, claim: TaskCoordinatorClaim, lease_seconds: float,
) -> bool:
    """The Task coordinator run's ``RenewClaim``."""

    return await renew_task_coordinator_claim(
        db,
        task_id=claim.task_id,
        claim_token=claim.claim_token,
        claim_generation=claim.wake_generation,
        lease_seconds=lease_seconds,
    )


async def heartbeat(
    claim: Any,
    lost: asyncio.Event,
    *,
    interval_seconds: float,
    lease_seconds: float,
    session_factory: SessionFactory,
    label: str = "Task coordinator",
    renew: RenewClaim | None = None,
) -> None:
    """Renew the exact claim until ``lost`` is set; a failed renewal sets it.

    ``renew`` renews this run kind's claim (default: the Task coordinator
    run's), so every durable run shares one heartbeat.
    """

    renew_claim = renew or renew_task_coordinator_lease
    try:
        while not lost.is_set():
            await asyncio.sleep(interval_seconds)
            async with session_factory() as db:
                renewed = await renew_claim(db, claim, lease_seconds)
                if renewed:
                    await db.commit()
                else:
                    await db.rollback()
                    lost.set()
                    return
    except asyncio.CancelledError:
        raise
    except Exception:
        lost.set()
        # A generic DurableRunClaim carries ``key``, not ``task_id``.
        logger.exception(
            "%s claim heartbeat failed for %s", label,
            getattr(claim, "task_id", None) or getattr(claim, "key", None),
        )


async def fence_before_model(
    claim: TaskCoordinatorClaim,
    lost: asyncio.Event,
    is_current: Callable[[TaskCoordinatorClaim], Awaitable[bool]],
    *,
    label: str = "Task coordinator",
) -> None:
    """Refuse provider I/O once the claim, Task lifecycle or factory changed.

    Runtime's cancellation checker is advisory and model callbacks turn
    exceptions into a safe stop. This explicit check prevents a superseded
    owner from reaching the provider.
    """

    try:
        current = await is_current(claim)
    except Exception:
        lost.set()
        raise
    if not current:
        lost.set()
        raise RuntimeError(f"{label} claim changed before model I/O")
