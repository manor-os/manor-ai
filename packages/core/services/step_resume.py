"""Resume or cancel a waiting_human execution step outside the HTTP layer.

Extracted from ``apps/api/routers/workspace_chat.py`` so that non-HTTP
callers (the ``answer_task_blocker`` chat tool) can share the exact same
state transitions as the chat card's resolve endpoint, instead of growing
a second drifting copy. The router now delegates here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.constants.task import TaskStatus

logger = logging.getLogger(__name__)


async def _workspace_allows_step_transition(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: Optional[str],
) -> bool:
    if workspace_id is None:
        return True
    from packages.core.models.workspace import Workspace

    status = await db.scalar(
        select(Workspace.status).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )
    return status == "active"


async def lock_waiting_step_for_decision(
    db: AsyncSession,
    *,
    entity_id: str,
    step_id: str,
    plan_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Optional[Any]:
    """Lock and refresh the exact Step origin a human is deciding.

    Approval surfaces lock their ``HitlRequest`` before calling this helper.
    Refreshing the Step under ``FOR UPDATE`` prevents a retry surface from
    applying a stale ``waiting_human`` snapshot after another transaction has
    already rejected or completed the origin.
    """
    origin = await _lock_waiting_step_origin(
        db,
        entity_id=entity_id,
        step_id=step_id,
        plan_id=plan_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    return origin[0] if origin is not None else None


async def _lock_waiting_step_origin(
    db: AsyncSession,
    *,
    entity_id: str,
    step_id: str,
    plan_id: Optional[str],
    workspace_id: Optional[str],
    task_id: Optional[str],
) -> Optional[tuple[Any, Any]]:
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    origin_filters = [
        ExecutionStep.id == step_id,
        ExecutionStep.entity_id == entity_id,
        ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN.value,
        ExecutionPlan.id == ExecutionStep.plan_id,
        ExecutionPlan.entity_id == entity_id,
    ]
    if plan_id is not None:
        origin_filters.append(ExecutionPlan.id == plan_id)
    if workspace_id is not None:
        origin_filters.extend((
            ExecutionStep.workspace_id == workspace_id,
            ExecutionPlan.workspace_id == workspace_id,
        ))
    if task_id is not None:
        origin_filters.append(ExecutionPlan.task_id == task_id)

    origin = (await db.execute(
        select(ExecutionStep.plan_id, ExecutionPlan.task_id)
        .where(*origin_filters)
    )).one_or_none()
    if origin is None:
        return None
    resolved_plan_id, resolved_task_id = origin

    # Task-bound Plan mutations use the Task row as their shared mutex. Lock it
    # before Plan/Step so HITL decisions cannot invert the Task -> Plan order
    # used by Plan retry and dispatch-failure recovery.
    if resolved_task_id:
        task_filters = [
            Task.id == resolved_task_id,
            Task.entity_id == entity_id,
        ]
        locked_task = (await db.execute(
            select(Task)
            .where(*task_filters)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        # Legacy Plans may retain an origin_task_id after their Task row was
        # removed. With no Task row there is no competing Task mutex or status
        # to update, so Plan -> Step locking remains safe for that orphan.
        if (
            locked_task is not None
            and workspace_id is not None
            and locked_task.workspace_id != workspace_id
        ):
            return None

    plan_filters = [
        ExecutionPlan.id == resolved_plan_id,
        ExecutionPlan.entity_id == entity_id,
        (
            ExecutionPlan.task_id == resolved_task_id
            if resolved_task_id
            else ExecutionPlan.task_id.is_(None)
        ),
    ]
    if workspace_id is not None:
        plan_filters.append(ExecutionPlan.workspace_id == workspace_id)
    plan = (await db.execute(
        select(ExecutionPlan)
        .where(*plan_filters)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if plan is None:
        return None

    step_filters = [
        ExecutionStep.id == step_id,
        ExecutionStep.entity_id == entity_id,
        ExecutionStep.plan_id == plan.id,
        ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN.value,
    ]
    if workspace_id is not None:
        step_filters.append(ExecutionStep.workspace_id == workspace_id)
    step = (await db.execute(
        select(ExecutionStep)
        .where(*step_filters)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if step is None:
        return None
    return step, plan


def apply_step_resume(
    step: Any,
    *,
    params_update: Optional[dict] = None,
    human_input_response: Optional[dict] = None,
) -> None:
    """Mutate a step row to flip from waiting_human back to pending.
    Pure — caller handles DB load, plan reset, and re-enqueue."""
    if params_update:
        # Tool wrapper expects values inside step.params; preserve any
        # unrelated existing keys (cookies path, original args, etc.).
        merged = dict(step.params or {})
        merged.update(params_update)
        step.params = merged

    if human_input_response is not None:
        step.human_input_response = human_input_response

    step.step_status = ExecutionStepStatus.PENDING.value
    step.human_input_prompt = None
    step.current_lease_id = None
    step.error = None
    step.finished_at = None


def apply_step_cancel(
    step: Any,
    reason: str,
    *,
    error_type: str = "UserSkipped",
    human_decision: Optional[dict] = None,
) -> None:
    """Mutate a step row to fail it after a 'skip' / 'cancel'
    resolution. Pure — caller handles DB load + re-enqueue."""
    step.step_status = ExecutionStepStatus.FAILED.value
    error = {"type": error_type, "message": reason}
    if human_decision:
        error["human_decision"] = dict(human_decision)
    step.error = error
    step.human_input_prompt = None
    step.current_lease_id = None
    step.finished_at = datetime.now(timezone.utc)


async def resume_step_for_retry(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    step_id: str,
    plan_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    params_update: Optional[dict] = None,
    human_input_response: Optional[dict] = None,
    enqueue: bool = True,
) -> Optional[str]:
    """Reset a waiting_human step back to pending so PlanExecutor /
    Dispatcher pick it up next cycle. Optionally merges fresh values
    into ``step.params`` (answers / confirm flags) before retry, and
    optionally writes ``human_input_response`` for legacy free-form
    HITL replies.

    Caller commits.
    """
    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import apply_task_status_transition

    if not await _workspace_allows_step_transition(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ):
        return None

    origin = await _lock_waiting_step_origin(
        db,
        entity_id=entity_id,
        step_id=step_id,
        plan_id=plan_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    if origin is None:
        return None
    step, plan = origin

    apply_step_resume(
        step,
        params_update=params_update,
        human_input_response=human_input_response,
    )

    # M9.2 — a resumed step means the awaited human input arrived: fulfil
    # any open commitment rows for this step (best-effort, silent no-op).
    try:
        from packages.core.humans import resolve_commitments_for_step
        await resolve_commitments_for_step(
            db, step.id,
            {"kind": "hitl_response"},
        )
    except Exception:
        logger.warning(
            "human commitment resolve failed for step %s (ignored)",
            step.id, exc_info=True,
        )

    target_plan_id = step.plan_id
    plan.status = ExecutionPlanStatus.RUNNING.value
    plan.completed_at = None
    plan.last_error = None
    if plan.task_id:
        task = (await db.execute(
            select(Task).where(
                Task.id == plan.task_id,
                Task.entity_id == entity_id,
            )
        )).scalar_one_or_none()
        if task and task.status == TaskStatus.WAITING_ON_CUSTOMER:
            await apply_task_status_transition(
                task, "in_progress", db=db, actor_kind="user", actor_id=user_id,
            )

    if enqueue:
        try:
            from packages.core.tasks.ai_tasks import run_plan
            run_plan.delay(target_plan_id)
        except Exception:
            pass  # best-effort — next heartbeat will pick it up
    return target_plan_id


async def cancel_step(
    db: AsyncSession,
    *,
    entity_id: str,
    step_id: str,
    plan_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    reason: str = "user skipped",
    error_type: str = "UserSkipped",
    human_decision: Optional[dict] = None,
    enqueue: bool = True,
) -> Optional[str]:
    """User chose 'skip' / 'cancel' on a pending_action — fail the
    step so the plan can finalize. The PlanExecutor's terminal
    summary handles the failed → replan-or-fail decision on the next
    cycle.

    Caller commits.
    """
    if not await _workspace_allows_step_transition(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ):
        return None

    origin = await _lock_waiting_step_origin(
        db,
        entity_id=entity_id,
        step_id=step_id,
        plan_id=plan_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    if origin is None:
        return None
    step, _plan = origin

    apply_step_cancel(
        step,
        reason,
        error_type=error_type,
        human_decision=human_decision,
    )

    target_plan_id = step.plan_id

    # Re-enqueue the executor so it sees the failed step and decides
    # whether to replan or terminate the plan.
    if enqueue:
        try:
            from packages.core.tasks.ai_tasks import run_plan
            run_plan.delay(target_plan_id)
        except Exception:
            pass
    return target_plan_id
