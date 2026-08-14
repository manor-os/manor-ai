"""Shared preparation and dispatch for manual task retries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task, TaskLog
from packages.core.services.task_dependencies import (
    dependency_ids_from_details,
    details_with_dependency_state,
)
from packages.core.services.task_service import add_task_log
from packages.core.services.task_state_machine import apply_task_status_transition


class TaskRetryError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class TaskRetryResult:
    task: Task
    mode: str
    plan_id: Optional[str]
    reset_steps: int
    dispatch: tuple[str, str]


def _has_unconsumed_proposal_external_action(details: dict) -> bool:
    external_action = details.get("external_action")
    authorization = details.get("proposal_external_authorization")
    return bool(
        isinstance(external_action, dict)
        and external_action
        and isinstance(authorization, dict)
        and authorization.get("authorization_id")
        and authorization.get("consumed_at") is None
        and authorization.get("execution_count") in (None, 0)
    )


def _runtime_context_blocks_external_action(
    runtime_context: object,
    external_action: object,
) -> bool:
    if not isinstance(runtime_context, dict) or not isinstance(external_action, dict):
        return False
    provider = str(external_action.get("provider") or "").strip()
    action = str(external_action.get("action") or "").strip()
    if not action:
        return False

    action_keys = {action, f"{provider}.{action}"}
    verb, separator, resource = action.partition("_")
    if separator and resource:
        action_keys.update({
            f"{resource}.{verb}_{resource}",
            f"{resource}.{verb}_{provider}",
            f"social.{action}",
        })

    for rule in runtime_context.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("rule_type") or "").lower() not in {
            "deny",
            "never_allow",
            "block",
        }:
            continue
        action_patterns = rule.get("action_patterns") or rule.get("actions") or []
        capability_patterns = (
            rule.get("capability_patterns") or rule.get("capabilities") or []
        )
        if isinstance(action_patterns, str):
            action_patterns = [action_patterns]
        if isinstance(capability_patterns, str):
            capability_patterns = [capability_patterns]
        if any(
            isinstance(pattern, str)
            and any(fnmatchcase(action_key, pattern) for action_key in action_keys)
            for pattern in action_patterns
        ):
            return True
        if any(
            isinstance(pattern, str)
            and fnmatchcase("external.social", pattern)
            for pattern in capability_patterns
        ):
            return True
    return False


async def _restore_compatible_external_runtime_context(
    db: AsyncSession,
    *,
    task: Task,
    details: dict,
) -> bool:
    external_action = details.get("external_action")
    if not _runtime_context_blocks_external_action(
        details.get("runtime_context"),
        external_action,
    ):
        return False

    logs = list((await db.execute(
        select(TaskLog).where(
            TaskLog.task_id == task.id,
            TaskLog.log_type == TaskLogType.RUNTIME_CONTEXT.value,
        ).order_by(TaskLog.created_at.desc())
    )).scalars().all())
    for log in logs:
        runtime_context = (log.meta or {}).get("runtime_context")
        if not isinstance(runtime_context, dict):
            continue
        if _runtime_context_blocks_external_action(runtime_context, external_action):
            continue
        details["runtime_context"] = dict(runtime_context)
        return True
    return False


async def prepare_task_retry(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    user_label: str,
    note: Optional[str] = None,
) -> TaskRetryResult:
    """Reset retryable state without committing or enqueueing work."""
    if task.status in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
        raise TaskRetryError(409, f"Task is {task.status} and cannot be retried")
    if task.status == TaskStatus.IN_PROGRESS.value:
        raise TaskRetryError(409, "Task is already in progress")

    clean_note = (note or "").strip() or None
    now = datetime.now(timezone.utc)
    details = dict(task.details or {})
    retry_meta = {"requested_by": user_id, "requested_at": now.isoformat()}
    if clean_note:
        retry_meta["note"] = clean_note
    retry_count = int(details.get("manual_retry_count") or 0) + 1
    details["manual_retry"] = retry_meta
    details["manual_retry_count"] = retry_count

    if dependency_ids_from_details(details):
        details = await details_with_dependency_state(db, task, details)
        if details.get("dependency_status") != "completed":
            raise TaskRetryError(
                409,
                "Task dependencies are not completed yet; waiting for predecessor outputs.",
            )

    mode = ""
    plan_id: str | None = None
    reset_steps = 0
    reset_step_ids: list[str] = []

    plan = (await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.task_id == task.id,
            ExecutionPlan.entity_id == task.entity_id,
        ).order_by(ExecutionPlan.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    start_new_external_plan = _has_unconsumed_proposal_external_action(details)
    restored_runtime_context = False
    if start_new_external_plan:
        details.pop("_replan_context", None)
        restored_runtime_context = await _restore_compatible_external_runtime_context(
            db,
            task=task,
            details=details,
        )

    if plan and plan.status not in {
        ExecutionPlanStatus.COMPLETED.value,
        ExecutionPlanStatus.CANCELLED.value,
    } and not start_new_external_plan:
        mode = "plan"
        plan_id = plan.id
        steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        )).scalars().all())
        retryable_statuses = {
            ExecutionStepStatus.FAILED.value,
            ExecutionStepStatus.SKIPPED.value,
            ExecutionStepStatus.WAITING_HUMAN.value,
            ExecutionStepStatus.PAUSED.value,
            ExecutionStepStatus.CANCELLED.value,
        }
        for step in steps:
            if step.step_status not in retryable_statuses:
                continue
            step.step_status = ExecutionStepStatus.PENDING.value
            step.current_lease_id = None
            step.human_input_prompt = None
            step.human_input_response = (
                {"response": clean_note, "user": user_label}
                if clean_note else None
            )
            step.error = None
            step.finished_at = None
            step.attempt_count = 0
            reset_steps += 1
            reset_step_ids.append(step.id)
        if plan.status in {
            ExecutionPlanStatus.FAILED.value,
            ExecutionPlanStatus.NEEDS_ATTENTION.value,
            ExecutionPlanStatus.PAUSED.value,
        }:
            plan.status = ExecutionPlanStatus.DRAFT.value
            plan.completed_at = None
            plan.last_error = None
        dispatch = ("plan", plan.id)
    elif task.owner_subscription_id or task.owner_service_key:
        mode = "plan_new"
        dispatch = ("plan_new", task.id)
    else:
        from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent

        if task.agent_id or is_master_agent(task.agent_id, task.agent_type):
            mode = "agent"
            if clean_note:
                details["_hitl_response"] = clean_note
                details["_hitl_responded_by"] = user_label
            dispatch = ("agent", task.agent_id or MANOR_AGENT_ID)
        else:
            raise TaskRetryError(
                409,
                "Task has no plan, owner subscription, or assigned agent to retry",
            )

    await apply_task_status_transition(
        task,
        TaskStatus.IN_PROGRESS.value,
        now=now,
        db=db,
        actor_kind="user",
        actor_id=user_id,
    )
    task.started_at = now
    task.completed_at = None
    task.details = details
    task.actual_output = None
    await add_task_log(
        db,
        task.id,
        TaskLogType.MANUAL_RETRY,
        "Manual retry requested" + (f": {clean_note}" if clean_note else ""),
        actor=TaskActor.USER,
        created_by=user_label,
        metadata={
            "mode": mode,
            "plan_id": plan_id,
            "step_ids": reset_step_ids,
            "reset_steps": reset_steps,
            "retry_count": retry_count,
            "requested_by": user_id,
            "runtime_context_restored": restored_runtime_context,
        },
    )
    from packages.core.services import event_emitter

    event_emitter.emit(
        task.entity_id,
        "task.retried",
        source="tasks_api",
        payload={
            "task_id": task.id,
            "plan_id": plan_id,
            "step_ids": reset_step_ids,
            "mode": mode,
            "reset_steps": reset_steps,
            "retry_count": retry_count,
            "requested_by": user_id,
            "runtime_context_restored": restored_runtime_context,
        },
    )
    await db.flush()
    return TaskRetryResult(
        task=task,
        mode=mode,
        plan_id=plan_id,
        reset_steps=reset_steps,
        dispatch=dispatch,
    )


def dispatch_task_retry(result: TaskRetryResult) -> bool:
    """Enqueue prepared work after its database transaction commits."""
    kind, value = result.dispatch
    if kind == "plan":
        from packages.core.tasks.ai_tasks import run_plan

        run_plan.delay(value)
    elif kind == "plan_new":
        from packages.core.tasks.ai_tasks import plan_and_run_task

        plan_and_run_task.delay(value)
    else:
        from packages.core.tasks.ai_tasks import run_agent_task

        run_agent_task.delay(result.task.id, value)
    return True
