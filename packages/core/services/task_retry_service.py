"""Shared preparation and dispatch for manual task retries."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.constants.execution import (
    PLAN_ACTIVE_STATUSES,
    PLAN_TERMINAL_STATUSES,
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task, TaskLog
from packages.core.services.task_dependencies import (
    dependency_ids_from_details,
    details_with_dependency_state,
)
from packages.core.services.task_service import add_task_log
from packages.core.services.task_state_machine import apply_task_status_transition

logger = logging.getLogger(__name__)


RETRYABLE_EXECUTION_STEP_STATUSES = frozenset({
    ExecutionStepStatus.FAILED.value,
    ExecutionStepStatus.SKIPPED.value,
    ExecutionStepStatus.PAUSED.value,
    ExecutionStepStatus.CANCELLED.value,
})
WAITING_HUMAN_RETRY_DETAIL = (
    "Plan is waiting for a human decision; resolve its approval or input card instead"
)
PLAN_DISPATCH_FAILURE_TYPES = frozenset({
    "PlanContinuationDispatchFailed",
    "TaskRetryDispatchFailed",
})
ACTIVE_PLAN_RETRY_STATUSES = frozenset(
    {
        ExecutionPlanStatus.DRAFT.value,
        ExecutionPlanStatus.PENDING_APPROVAL.value,
    }
    | {status.value for status in PLAN_ACTIVE_STATUSES}
)
ACTIVE_PLAN_RETRY_DETAIL = (
    "Plan execution is active or awaiting approval; wait for that lifecycle "
    "to finish before retrying its Steps"
)
TASK_PLAN_DISPATCH_RECOVERY_DETAIL = (
    "Task-bound Plan dispatch recovery must be resolved from its Task recovery action."
)
TASK_RECOVERY_RETRY_DETAIL = (
    "Task recovery is waiting for a decision; use its Task recovery action instead."
)
STANDALONE_PLAN_DISPATCH_RECOVERY_DETAIL = (
    "Plan dispatch recovery must be resolved from its Plan recovery action."
)


def has_waiting_human_decision(steps: list[ExecutionStep]) -> bool:
    return any(
        step.step_status == ExecutionStepStatus.WAITING_HUMAN.value
        for step in steps
    )


def is_plan_dispatch_recovery(plan: ExecutionPlan) -> bool:
    error_type = (
        str(plan.last_error.get("type") or "")
        if isinstance(plan.last_error, dict)
        else ""
    )
    return (
        plan.status == ExecutionPlanStatus.NEEDS_ATTENTION.value
        and error_type in PLAN_DISPATCH_FAILURE_TYPES
    )


def plan_retry_block_detail(
    plan: ExecutionPlan,
    *,
    allow_standalone_dispatch_recovery: bool,
) -> str | None:
    """Return why a generic Plan/Step retry does not own this transition."""
    if is_plan_dispatch_recovery(plan):
        if plan.task_id:
            return TASK_PLAN_DISPATCH_RECOVERY_DETAIL
        if not allow_standalone_dispatch_recovery:
            return STANDALONE_PLAN_DISPATCH_RECOVERY_DETAIL
    if plan.status in ACTIVE_PLAN_RETRY_STATUSES:
        return ACTIVE_PLAN_RETRY_DETAIL
    return None


async def has_pending_task_recovery(
    db: AsyncSession,
    plan: ExecutionPlan,
) -> bool:
    """Whether an open Task recovery decision owns this Plan's next transition."""
    if not plan.task_id:
        return False
    request_id = (await db.execute(
        select(HitlRequest.id)
        .where(
            HitlRequest.entity_id == plan.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == plan.task_id,
            HitlRequest.action_key == "task.recover",
            HitlRequest.status == ApprovalStatus.PENDING.value,
        )
        .limit(1)
    )).scalar_one_or_none()
    return request_id is not None


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


def _persisted_plan_replan_context(
    plan: ExecutionPlan,
    *,
    task_expected_output: object = None,
    task_details: object = None,
) -> dict | None:
    """Return context when a stored DAG must be replaced, not replayed.

    This is intentionally shape-aware: legacy unshaped agent steps are
    repaired to the StepResult envelope, while an explicit bare payload using
    ``result.outputs.*`` is rejected and sent through a fresh planner pass.
    A DAG that requires artifact writes forbidden by the user's task binding
    is likewise structural and cannot be repaired by resetting attempts.
    """
    snapshot = plan.plan_dag
    from packages.core.plans.task_constraints import (
        binding_constraints_forbid_artifact_writes,
        plan_requires_artifact_write,
    )

    if (
        binding_constraints_forbid_artifact_writes(task_details)
        and plan_requires_artifact_write(snapshot)
    ):
        issue = (
            "The persisted Plan requires a saved artifact, but USER CONSTRAINTS "
            "prohibit file/artifact writes. Create a fresh Plan that returns the "
            "result inline and contains no file.write step, ArtifactResult or "
            "DocumentResult output, or expects=['files']."
        )
        return {
            "prior_plan_id": plan.id,
            "reason": "task_constraint_contract_conflict",
            "issue": issue,
            "contract_gaps": issue,
            "error_type": "TaskConstraintContractConflict",
            "retry_mode": "task_constraint_replan",
        }
    if snapshot == {} or (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("steps"), list)
        and not snapshot["steps"]
    ):
        # Early materialized Plans did not persist a replayable DAG snapshot;
        # their ExecutionStep rows remain the source of truth for retry. Treat
        # only a present, non-empty snapshot as a contract migration input.
        return None

    from packages.core.plans.service import persisted_plan_contract_gaps

    gaps = persisted_plan_contract_gaps(
        snapshot,
        task_expected_output=task_expected_output,
    )
    if not gaps:
        return None
    return {
        "prior_plan_id": plan.id,
        "reason": "persisted_plan_contract_gaps",
        "contract_gaps": "; ".join(
            f"{getattr(gap, 'step_key', 'plan')}: {getattr(gap, 'detail', gap)}"
            for gap in gaps
        ),
    }


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
    locked_task = (await db.execute(
        select(Task)
        .where(Task.id == task.id, Task.entity_id == task.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked_task is None:
        raise TaskRetryError(404, "Task not found")
    task = locked_task
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

    plan_steps: list[ExecutionStep] = []
    replan_context: dict | None = None
    if plan:
        plan_steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        )).scalars().all())
        replan_context = _persisted_plan_replan_context(
            plan,
            task_expected_output=getattr(task, "expected_output", None),
            task_details=details,
        )
        if replan_context is not None and has_waiting_human_decision(plan_steps):
            raise TaskRetryError(409, WAITING_HUMAN_RETRY_DETAIL)

        if (
            plan.status == ExecutionPlanStatus.PENDING_APPROVAL.value
            or plan.approval_required
        ):
            raise TaskRetryError(409, WAITING_HUMAN_RETRY_DETAIL)

    pending_plan_dispatch = details.get("_pending_plan_dispatch")
    pending_plan_id = (
        str(pending_plan_dispatch.get("plan_id") or "")
        if isinstance(pending_plan_dispatch, dict)
        else ""
    )
    plan_error_type = (
        str(plan.last_error.get("type") or "")
        if plan is not None and isinstance(plan.last_error, dict)
        else ""
    )
    can_resume_pending_plan_dispatch = bool(
        plan is not None
        and pending_plan_id == plan.id
        and plan.status == ExecutionPlanStatus.NEEDS_ATTENTION.value
        and not plan.approval_required
        and plan_error_type in PLAN_DISPATCH_FAILURE_TYPES
    )
    if pending_plan_id and not can_resume_pending_plan_dispatch:
        details.pop("_pending_plan_dispatch", None)

    start_new_external_plan = _has_unconsumed_proposal_external_action(details)
    restored_runtime_context = False
    if start_new_external_plan:
        details.pop("_replan_context", None)
        restored_runtime_context = await _restore_compatible_external_runtime_context(
            db,
            task=task,
            details=details,
        )

    if plan and can_resume_pending_plan_dispatch:
        # The human decision and Step mutation already committed; only the
        # post-commit enqueue failed. Resume the executor without resetting the
        # Step or discarding review guidance stored on its error payload.
        mode = "plan_dispatch"
        plan_id = plan.id
        plan.status = ExecutionPlanStatus.RUNNING.value
        plan.completed_at = None
        plan.last_error = None
        details.pop("_pending_plan_dispatch", None)
        dispatch = ("plan", plan.id)
    elif plan and plan.status not in {
        ExecutionPlanStatus.COMPLETED.value,
        ExecutionPlanStatus.CANCELLED.value,
        ExecutionPlanStatus.REPLANNED.value,
    } and not start_new_external_plan and replan_context is None:
        mode = "plan"
        plan_id = plan.id
        has_waiting_human = has_waiting_human_decision(plan_steps)
        if has_waiting_human:
            raise TaskRetryError(409, WAITING_HUMAN_RETRY_DETAIL)
        for step in plan_steps:
            if step.step_status not in RETRYABLE_EXECUTION_STEP_STATUSES:
                continue
            step.step_status = ExecutionStepStatus.PENDING.value
            step.current_lease_id = None
            step.human_input_prompt = None
            step.human_input_response = None
            if clean_note:
                step.human_input_response = {
                    "response": clean_note,
                    "user": user_label,
                }
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
        if (
            replan_context is not None
            and plan is not None
            and not start_new_external_plan
        ):
            # Preserve the old plan as immutable evidence and let the normal
            # Planner create a fresh ExecutionPlan with parent_plan_id.  This
            # avoids silently rewriting refs that are valid only for legacy
            # StepResult envelopes.
            plan.status = ExecutionPlanStatus.REPLANNED.value
            plan.completed_at = now
            plan.last_error = {
                "type": replan_context.get("error_type", "PlanContractMigrationRequired"),
                "message": replan_context["contract_gaps"],
            }
            existing_replan_context = (
                dict(details.get("_replan_context"))
                if isinstance(details.get("_replan_context"), dict)
                else {}
            )
            if replan_context.get("reason") == "task_constraint_contract_conflict":
                existing_replan_context.pop("artifact_recovery", None)
            details["_replan_context"] = {
                **existing_replan_context,
                **{
                    key: value
                    for key, value in replan_context.items()
                    if key not in {"error_type", "retry_mode"}
                },
            }
        dispatch = ("plan_new", task.id)
    elif replan_context is not None:
        # Replaying a stale DAG is unsafe when the task has no Planner owner:
        # the old result representation is precisely what the linker rejected.
        # Fail with an actionable contract error instead of silently routing
        # around the migration gate through an assigned-agent retry.
        raise TaskRetryError(
            409,
            "Stored execution plan has an invalid output contract and the task "
            "has no Planner owner; assign an owner or create a new plan before retrying.",
        )
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

    await event_emitter.emit_in_session(
        db,
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
        workspace_id=task.workspace_id,
        deliver_after_commit=True,
    )
    await db.flush()
    return TaskRetryResult(
        task=task,
        mode=mode,
        plan_id=plan_id,
        reset_steps=reset_steps,
        dispatch=dispatch,
    )


async def mark_task_retry_dispatch_failed(
    db: AsyncSession,
    *,
    result: TaskRetryResult,
    error: Exception,
) -> Optional[Task]:
    """Return a committed manual retry to a visible, retryable state."""
    dispatch_kind, _dispatch_value = result.dispatch
    # Every Task-bound recovery surface owns the Task row first. Keeping the
    # Task -> Plan order aligned with ``prepare_task_retry`` prevents a queue
    # failure repair from deadlocking a concurrent manual retry.
    task = (await db.execute(
        select(Task).where(
            Task.id == result.task.id,
            Task.entity_id == result.task.entity_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        return None

    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    if task.status in TERMINAL_STATUSES:
        return task

    plan = None
    if dispatch_kind == "plan" and result.plan_id:
        plan = (await db.execute(
            select(ExecutionPlan).where(
                ExecutionPlan.id == result.plan_id,
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.entity_id == task.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()

    error_message = str(error)[:1000] or error.__class__.__name__
    details = dict(task.details or {})
    terminal_plan_statuses = {status.value for status in PLAN_TERMINAL_STATUSES}
    if plan is not None and plan.status not in terminal_plan_statuses:
        plan.status = ExecutionPlanStatus.NEEDS_ATTENTION.value
        plan.completed_at = None
        plan.last_error = {
            "type": "TaskRetryDispatchFailed",
            "message": error_message,
        }
        details["_pending_plan_dispatch"] = {
            "plan_id": plan.id,
            "reason": "task_retry_dispatch_failed",
            "mode": result.mode,
        }
    task.details = details
    await apply_task_status_transition(
        task,
        TaskStatus.WAITING_ON_CUSTOMER.value,
        db=db,
        actor_kind=TaskActor.SYSTEM.value,
    )
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_EXECUTION_FAILED,
        "The retry was saved, but execution could not be queued. Retry again when the worker queue is available.",
        actor=TaskActor.SYSTEM,
        created_by="system",
        metadata={
            "mode": result.mode,
            "plan_id": result.plan_id,
            "dispatch_kind": dispatch_kind,
            "error": error_message,
        },
    )
    if task.workspace_id:
        from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

        try:
            async with db.begin_nested():
                await ensure_task_recovery_hitl(
                    db,
                    task,
                    plan_id=plan.id if plan is not None else result.plan_id,
                    prompt="The retry could not be queued.",
                    issue=error_message,
                )
        except Exception:
            # The state repair is the durable safety boundary. A failed Chat
            # projection must not roll it back and strand the Task in_progress.
            logger.exception(
                "Could not create Task retry recovery card: task=%s",
                task.id,
            )
    await db.flush()
    return task


async def mark_plan_continuation_dispatch_failed(
    db: AsyncSession,
    *,
    plan_id: str,
    user_id: str,
    reason: str,
    is_retry: bool = False,
) -> bool:
    """Make a committed Plan continuation queue failure retryable."""
    plan_snapshot = (await db.execute(
        select(ExecutionPlan)
        .where(ExecutionPlan.id == plan_id)
    )).scalar_one_or_none()
    if plan_snapshot is None:
        return False

    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    task = None
    if plan_snapshot.task_id:
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == plan_snapshot.task_id,
                Task.entity_id == plan_snapshot.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is not None and task.status in TERMINAL_STATUSES:
            return False

    plan_filters = [
        ExecutionPlan.id == plan_id,
        ExecutionPlan.entity_id == plan_snapshot.entity_id,
    ]
    if task is not None:
        plan_filters.append(ExecutionPlan.task_id == task.id)
    plan = (await db.execute(
        select(ExecutionPlan)
        .where(*plan_filters)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    terminal_plan_statuses = {status.value for status in PLAN_TERMINAL_STATUSES}
    if plan is None or plan.status in terminal_plan_statuses:
        return False

    plan.status = ExecutionPlanStatus.NEEDS_ATTENTION.value
    plan.completed_at = None
    saved_action = "retry" if is_retry else "human decision"
    plan.last_error = {
        "type": "PlanContinuationDispatchFailed",
        "message": f"The {saved_action} was saved, but the Plan runner was not queued.",
    }
    if task is None:
        await db.flush()
        return True

    details = dict(task.details or {})
    details["_pending_plan_dispatch"] = {
        "plan_id": plan.id,
        "reason": reason,
    }
    task.details = details
    await apply_task_status_transition(
        task,
        TaskStatus.WAITING_ON_CUSTOMER.value,
        db=db,
        actor_kind=TaskActor.USER.value,
        actor_id=user_id,
    )
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_EXECUTION_FAILED,
        f"The {saved_action} was saved, but Plan continuation was not queued.",
        actor=TaskActor.SYSTEM,
        created_by="system",
        metadata={
            "plan_id": plan.id,
            "mode": "plan_continuation_dispatch",
            "reason": reason,
        },
    )
    if task.workspace_id:
        from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

        try:
            async with db.begin_nested():
                await ensure_task_recovery_hitl(
                    db,
                    task,
                    plan_id=plan.id,
                    prompt="Execution could not resume.",
                    issue=plan.last_error["message"],
                )
        except Exception:
            logger.exception(
                "Could not create Plan continuation recovery card: task=%s",
                task.id,
            )
    await db.flush()
    return True


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
