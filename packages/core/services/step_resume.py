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
    return status in {"active", "paused"}


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
    from packages.core.contracts.step_execution import STEP_EXECUTION_CONTRACT_PARAM

    if isinstance(params_update, dict) and STEP_EXECUTION_CONTRACT_PARAM in params_update:
        raise ValueError("Step execution contract is server-owned and cannot change during input resume")
    structured_answer = (
        isinstance(params_update, dict) and "answers" in params_update
    )
    if structured_answer and human_input_response is not None:
        raise ValueError(
            "a step resume must use exactly one human-input answer channel"
        )

    if params_update or structured_answer or human_input_response is not None:
        # Tool wrapper expects values inside step.params; preserve any
        # unrelated existing keys (cookies path, original args, etc.).
        merged = dict(step.params or {})
        if params_update:
            merged.update(params_update)

        # ``answers`` (structured cards) and ``human_input_response`` (legacy
        # free-form cards) are alternative wire formats for one current human
        # response.  Replace the other channel in the same row mutation so a
        # later coordinator resume can never combine a prior response with the
        # newly submitted one.  The response column is projected into params
        # only when the worker snapshots the step; a stale params copy must not
        # survive either direction.
        if structured_answer:
            merged.pop("human_input_response", None)
            step.human_input_response = None
        elif human_input_response is not None:
            merged.pop("answers", None)
            merged.pop("human_input_response", None)
        step.params = merged

    if human_input_response is not None:
        step.human_input_response = human_input_response

    # ``runtime_checkpoint`` is deliberately kept: it is what lets the next
    # attempt resume the parked subagent at the answered call.
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
    if hasattr(step, "runtime_checkpoint"):
        step.runtime_checkpoint = None


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
    if not await _workspace_allows_step_transition(
        db, entity_id=entity_id, workspace_id=plan.workspace_id,
    ):
        return None
    from packages.core.services.task_execution_origin import record_manual_plan_execution

    record_manual_plan_execution(plan, user_id=user_id, action="step_resume")

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

    plan.status = ExecutionPlanStatus.RUNNING.value
    plan.completed_at = None
    plan.last_error = None
    # The worker that owns this job checks the pending Step out again.
    return step.plan_id


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
    if not await _workspace_allows_step_transition(
        db, entity_id=entity_id, workspace_id=_plan.workspace_id,
    ):
        return None

    # The paused lease (needs_human) or a still-active one must not outlive
    # the step: revoke first so a worker that resumes cannot report through
    # it onto the cancelled step.
    from packages.core.dispatcher.service import Dispatcher

    await Dispatcher().revoke_active_leases(
        db, step_ids=[step.id], reason=reason,
    )

    apply_step_cancel(
        step,
        reason,
        error_type=error_type,
        human_decision=human_decision,
    )

    return step.plan_id
