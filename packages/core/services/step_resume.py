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


def apply_step_cancel(step: Any, reason: str) -> None:
    """Mutate a step row to fail it after a 'skip' / 'cancel'
    resolution. Pure — caller handles DB load + re-enqueue."""
    step.step_status = ExecutionStepStatus.FAILED.value
    step.error = {"type": "UserSkipped", "message": reason}
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
    params_update: Optional[dict] = None,
    human_input_response: Optional[dict] = None,
) -> None:
    """Reset a waiting_human step back to pending so PlanExecutor /
    Dispatcher pick it up next cycle. Optionally merges fresh values
    into ``step.params`` (answers / confirm flags) before retry, and
    optionally writes ``human_input_response`` for legacy free-form
    HITL replies.

    Caller commits.
    """
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import apply_task_status_transition

    step = (await db.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one_or_none()
    if step is None:
        return

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

    target_plan_id = plan_id or step.plan_id
    if not target_plan_id:
        return

    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == target_plan_id)
    )).scalar_one_or_none()
    if plan:
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

    try:
        from packages.core.tasks.ai_tasks import run_plan
        run_plan.delay(target_plan_id)
    except Exception:
        pass  # best-effort — next heartbeat will pick it up


async def cancel_step(
    db: AsyncSession,
    *,
    step_id: str,
    plan_id: Optional[str] = None,
    reason: str = "user skipped",
) -> None:
    """User chose 'skip' / 'cancel' on a pending_action — fail the
    step so the plan can finalize. The PlanExecutor's terminal
    summary handles the failed → replan-or-fail decision on the next
    cycle.

    Caller commits.
    """
    from packages.core.models.execution import ExecutionStep

    step = (await db.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one_or_none()
    if step is None:
        return

    apply_step_cancel(step, reason)

    target_plan_id = plan_id or step.plan_id
    if not target_plan_id:
        return

    # Re-enqueue the executor so it sees the failed step and decides
    # whether to replan or terminate the plan.
    try:
        from packages.core.tasks.ai_tasks import run_plan
        run_plan.delay(target_plan_id)
    except Exception:
        pass
