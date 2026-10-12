"""Shared preparation and dispatch for manual task retries."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.constants.execution import (
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    PLAN_ACTIVE_STATUSES,
    PLAN_TERMINAL_STATUSES,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    WorkLeaseStatus,
    is_coordinator_child_params,
)
from packages.core.dispatcher.effect_safety import (
    ambiguous_effect_reconciliation_snapshot,
    retry_effect_block_detail,
    task_execution_effect_block_detail,
)
from packages.core.constants.task import TaskLogType, TaskRuntimeKind, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message, Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.worker import WorkLease
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.plans.step_reset import reset_step_to_retry_ready
from packages.core.services.task_dependencies import (
    dependency_ids_from_details,
    details_with_dependency_state,
)
from packages.core.services.task_service import add_task_log
from packages.core.services.task_execution import (
    TaskExecutionDispatch,
    dispatch_task_execution,
    plan_resume_dispatch,
    resolve_task_execution,
    stage_task_execution_intent,
)
from packages.core.services.task_state_machine import apply_task_status_transition

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from packages.core.plans.wakeup import PlanCycleWake


async def _task_thread_guidance_since_plan(
    db: AsyncSession, *, task: Task, plan: ExecutionPlan, before: datetime,
) -> str | None:
    if plan.created_at is None:
        return None
    messages = (await db.execute(
        select(Message.content)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == task.entity_id,
            Conversation.workspace_id == task.workspace_id,
            Conversation.scope == "workspace_thread",
            Conversation.thread_ref_kind == "task",
            Conversation.thread_ref_id == task.id,
            Message.role == "user",
            Message.author_kind == "user",
            Message.message_kind == "text",
            Message.created_at > plan.created_at,
            Message.created_at <= before,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(3)
    )).scalars().all()
    guidance = "\n\n".join(
        str(message).strip()[:1200]
        for message in reversed(messages)
        if str(message or "").strip()
    )[:2400]
    return guidance or None


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
_ACCEPTANCE_RETRY_KEY = "task_acceptance_retry"
_RETRYABLE_ACCEPTANCE_CODES = frozenset({
    "artifact_busy", "review_invalid", "review_timeout",
})


def _retryable_acceptance_checkpoint(checkpoint: object) -> bool:
    """Authenticate the last saved V3 control result for a legacy failed Plan.

    The model-facing repeated-result notice is only context compression. Its
    digest must identify an earlier complete, host-produced submit result;
    truncated previews are not recovery evidence.
    """

    if not isinstance(checkpoint, dict) or checkpoint.get("task_coordinator_control"):
        return False
    if checkpoint.get("pending_tool_call") or checkpoint.get("pending_tool_result"):
        return False
    messages = checkpoint.get("messages")
    if not isinstance(messages, list):
        return False
    call_names: dict[str, str] = {}
    last_call_index = -1
    results: list[tuple[int, str, str, dict]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return False
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    return False
                call_id = str(call.get("id") or "")
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                if not call_id or not isinstance(name, str) or call_id in call_names:
                    return False
                call_names[call_id] = name
                last_call_index = index
        elif message.get("role") == "tool":
            name = call_names.get(str(message.get("tool_call_id") or ""))
            content = message.get("content")
            if name is None or not isinstance(content, str):
                return False
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                return False
            if not isinstance(payload, dict):
                return False
            results.append((index, name, content, payload))
    if not results or results[-1][0] < last_call_index:
        return False
    _, name, _, payload = results[-1]
    if name != "submit_task":
        return False
    if payload.get("repeated_tool_result") is True:
        if payload.get("tool") != "submit_task":
            return False
        digest = payload.get("result_digest")
        if not isinstance(digest, str) or len(digest) != 16:
            return False
        original = next((
            candidate
            for _, candidate_name, content, candidate in reversed(results[:-1])
            if candidate_name == "submit_task"
            and candidate.get("repeated_tool_result") is not True
            and hashlib.sha256(content.encode("utf-8", errors="replace"))
            .hexdigest()[:16] == digest
        ), None)
        if original is None:
            return False
        payload = original
    return (
        payload.get("status") == "retryable"
        and payload.get("code") in _RETRYABLE_ACCEPTANCE_CODES
    )


def _reset_acceptance_retry_checkpoint(
    checkpoint: object, *, plan_id: str, create: bool = False,
) -> dict | None:
    if not isinstance(checkpoint, dict):
        return None
    marker = checkpoint.get(_ACCEPTANCE_RETRY_KEY)
    if not create and not (
        isinstance(marker, dict)
        and marker.get("version") == 1
        and marker.get("plan_id") == plan_id
    ):
        return None
    updated = dict(checkpoint)
    updated[_ACCEPTANCE_RETRY_KEY] = {
        "version": 1,
        "plan_id": plan_id,
        "attempts": 0,
        "code": "manual_recovery",
    }
    return updated


def _reset_coordinator_completion_checkpoint(checkpoint: object, *, plan_id: str) -> dict | None:
    """Renew only an explicitly retried, host-recorded completion window.

    The original transcript, usage, observations and terminal diagnostic remain
    evidence. A normal wake or a pending continuation cannot obtain new budget.
    Caller still owns lifecycle, claim, HITL and effect-safety admission.
    """
    if not isinstance(checkpoint, dict):
        return None
    outcome = checkpoint.get("coordinator_outcome")
    if not isinstance(outcome, dict):
        return None
    details = outcome.get("details")
    if (
        outcome.get("contract") != "tool_error"
        or outcome.get("status") != "error"
        or outcome.get("tool") != "submit_task"
        # An unconfirmed stop gets the same renewal; the caller's effect gate
        # (ambiguous_effect_reconciliation_snapshot) still runs before it.
        or outcome.get("error") not in {
            "coordinator_acceptance_exhausted",
            "coordinator_tool_outcome_unconfirmed",
        }
        or not isinstance(details, dict)
        or details.get("plan_id") != plan_id
        or checkpoint.get("task_coordinator_control")
        or checkpoint.get("pending_tool_call")
        or checkpoint.get("pending_tool_result")
    ):
        return None
    return {**checkpoint, "rounds": 0, "non_submit_retries": 0}


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


def require_workspace_plan_resume_dispatch(
    *,
    task: Task,
    plan: ExecutionPlan,
    steps: list[ExecutionStep],
    execution: TaskExecutionDispatch,
) -> TaskExecutionDispatch:
    """Admit an existing Workspace Plan through the current runtime factory.

    A persisted Plan is not authority to bypass today's Workspace service
    scope.  The Task factory must still select coordinated execution, and any
    subscription identities already pinned on the Plan/Steps must match that
    exact selection.  This makes retry fail closed when a service is removed,
    becomes ambiguous, or is replaced while the old Plan is parked.

    Standalone pre-coordinator Plans retain their compatibility resume path.
    """

    if not task.workspace_id:
        return plan_resume_dispatch(task_id=task.id, plan_id=plan.id)
    selection = execution.coordinator
    if (
        execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
        or selection is None
    ):
        raise TaskRetryError(
            409,
            "The Workspace Plan cannot resume because its current Task runtime "
            "service scope is unavailable or ambiguous.",
        )
    if (
        plan.agent_subscription_id
        and str(plan.agent_subscription_id) != selection.root.subscription_id
    ):
        raise TaskRetryError(
            409,
            "The Workspace Plan cannot resume because its coordinator service "
            "binding changed; create a replacement Plan through the Task runtime.",
        )
    subscriptions_by_service = {
        binding.service_key: binding.subscription_id
        for binding in selection.bindings
    }
    drifted_steps = sorted({
        step.step_key
        for step in steps
        if step.service_key
        and step.resolved_subscription_id
        and subscriptions_by_service.get(str(step.service_key))
        != str(step.resolved_subscription_id)
    })
    if drifted_steps:
        raise TaskRetryError(
            409,
            "The Workspace Plan cannot resume because its pinned service "
            "binding changed for Step(s): " + ", ".join(drifted_steps),
        )
    return plan_resume_dispatch(task_id=task.id, plan_id=plan.id)


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
    dispatch: TaskExecutionDispatch
    plan_wake: PlanCycleWake | None = None


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


async def _require_current_runtime_checkpoint(db: AsyncSession, task: Task) -> None:
    """Refuse, with a clear answer, to retry a run a newer runtime saved.

    During a rolling deploy an older API could otherwise rebuild the newer
    checkpoint in its own shape (and the admission would fail later).
    """
    from packages.core.ai.runtime.run_state import RUN_STATE_NEWER_RUNTIME_DETAIL, runtime_run_state_is_newer

    checkpoint = (await db.execute(
        select(TaskCoordinatorRun.checkpoint).where(TaskCoordinatorRun.task_id == task.id)
    )).scalar_one_or_none()
    if isinstance(checkpoint, dict) and runtime_run_state_is_newer(checkpoint):
        logger.warning("Task %s retry refused: its checkpoint was saved by a newer runtime", task.id)
        raise TaskRetryError(409, RUN_STATE_NEWER_RUNTIME_DETAIL)


async def prepare_task_retry(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    user_label: str,
    note: Optional[str] = None,
) -> TaskRetryResult:
    """Reset retryable state without committing or enqueueing work."""
    scope_entity_id = task.entity_id
    scope_workspace_id = task.workspace_id
    if scope_workspace_id:
        from packages.core.services.workspace_access import lock_workspace_access_boundary

        # Match planning's Workspace -> Task order. Chat recovery also owns
        # this Workspace boundary before taking its Task and Message locks.
        workspace = await lock_workspace_access_boundary(
            db, workspace_id=scope_workspace_id, entity_id=scope_entity_id,
        )
        from packages.core.services.workspace_autonomy import workspace_allows_manual_work

        if not workspace_allows_manual_work(workspace):
            raise TaskRetryError(409, "Workspace is unavailable; restore it before retrying.")
    locked_task = (await db.execute(
        select(Task)
        .where(Task.id == task.id, Task.entity_id == task.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked_task is None:
        raise TaskRetryError(404, "Task not found")
    task = locked_task
    if task.entity_id != scope_entity_id or task.workspace_id != scope_workspace_id:
        raise TaskRetryError(409, "Task scope changed during retry admission; refresh and retry.")
    if task.status in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
        raise TaskRetryError(409, f"Task is {task.status} and cannot be retried")
    if task.status == TaskStatus.IN_PROGRESS.value:
        raise TaskRetryError(409, "Task is already in progress")
    await _require_current_runtime_checkpoint(db, task)

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
            ExecutionPlan.workspace_id == task.workspace_id,
        ).order_by(
            ExecutionPlan.created_at.desc(),
            ExecutionPlan.id.desc(),
        ).limit(1)
    )).scalar_one_or_none()

    plan_steps: list[ExecutionStep] = []
    replan_context: dict | None = None
    if plan:
        plan_steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.created_at, ExecutionStep.id)
        )).scalars().all())
        replan_context = _persisted_plan_replan_context(
            plan,
            task_expected_output=getattr(task, "expected_output", None),
            task_details=details,
        )
        recorded_effects = [
            {"step_key": step.step_key, "evidence_refs": step.evidence_refs,
             "result_summary": json.dumps(step.result, ensure_ascii=False, default=str)[:4000]}
            for step in plan_steps
            if step.step_status in RETRYABLE_EXECUTION_STEP_STATUSES
            and any(isinstance(ref, dict) and ref.get("kind") == "tool_effect"
                    for ref in (step.evidence_refs or []))
        ]
        if recorded_effects and replan_context is not None:
            replan_context = {
                **replan_context,
                "prior_plan_id": plan.id,
                "recorded_effects": recorded_effects,
                "contract_gaps": (
                    "A prior attempt recorded external effects but failed its result contract. "
                    "Verify/reconcile the saved receipts; do not repeat those effects merely "
                    "to repair the output format."
                ),
                "error_type": "ActionOutcomeReconciliationRequired",
            }
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

    plan_state = plan.dispatcher_state if plan is not None and isinstance(plan.dispatcher_state, dict) else {}
    v3_active_plan = bool(
        plan is not None
        and plan.status not in {status.value for status in PLAN_TERMINAL_STATUSES}
        and plan_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION
    )
    v3_failed_acceptance_plan = bool(
        plan is not None
        and plan.status == ExecutionPlanStatus.FAILED.value
        and plan_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION
        and isinstance(plan.last_error, dict)
        and plan_error_type == "ActionOutcomeReconciliationRequired"
        and "reconciliation" in plan.last_error
        and plan.last_error["reconciliation"] is None
        and task.status == TaskStatus.WAITING_ON_CUSTOMER.value
        and replan_context is None
        and not start_new_external_plan
    )
    execution = await resolve_task_execution(
        db, task, lock_subscriptions=v3_active_plan or v3_failed_acceptance_plan,
    )
    if plan is None and details.get("capability_blocker"):
        from packages.core.services.task_capability_recovery import validate_capability_retry

        await validate_capability_retry(db, task=task, execution=execution)
        details.pop("capability_blocker", None)

    if v3_active_plan or v3_failed_acceptance_plan:
        # V3 Step status/evidence is owned by the Task-level AgentLoop. A
        # manual Task retry may resume its exact parked Plan, but must never
        # run the V2 Step-reset/PLAN_RESUME path over that checkpoint.
        # Acquire Plan -> leases -> Steps before the coordinator Run, including
        # a stopped completion loop whose Plan remains active. Budget renewal
        # must not race an admitted writer or invert the settlement lock order.
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan.id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one()
        plan_state = (
            plan.dispatcher_state
            if isinstance(plan.dispatcher_state, dict) else {}
        )
        leases = list((await db.execute(
            select(WorkLease).where(WorkLease.plan_id == plan.id)
            .order_by(WorkLease.id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        plan_steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        if v3_failed_acceptance_plan:
            # An older coordinator could incorrectly finalize a Plan after a
            # transient read-only submit_task review error. Reopen only this
            # exact, fully settled Plan; never admit a replacement Plan merely
            # because the ambiguity list is empty.
            other_active_plan = (await db.execute(
                select(ExecutionPlan.id).where(
                    ExecutionPlan.task_id == task.id,
                    ExecutionPlan.id != plan.id,
                    ExecutionPlan.status.not_in(
                        tuple(status.value for status in PLAN_TERMINAL_STATUSES)
                    ),
                ).limit(1)
            )).scalar_one_or_none()
            if (
                plan.status != ExecutionPlanStatus.FAILED.value
                or plan_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY)
                != TASK_COORDINATOR_PLAN_VERSION
                or not isinstance(plan.last_error, dict)
                or plan.last_error.get("type") != "ActionOutcomeReconciliationRequired"
                or plan.last_error.get("reconciliation", object()) is not None
                or other_active_plan is not None
                or not plan_steps
                or any(step.step_status != ExecutionStepStatus.DONE.value for step in plan_steps)
                or any(lease.status not in {
                    WorkLeaseStatus.COMPLETED.value,
                    WorkLeaseStatus.FAILED.value,
                    WorkLeaseStatus.EXPIRED.value,
                    WorkLeaseStatus.CANCELLED.value,
                } for lease in leases)
                or await ambiguous_effect_reconciliation_snapshot(
                    db, plan_steps, plan_id=plan.id,
                ) is not None
            ):
                raise TaskRetryError(
                    409,
                    "The original Plan is not safely settled for a read-only "
                    "completion check; resolve its active or uncertain action first.",
                )
        run = (await db.execute(
            select(TaskCoordinatorRun)
            .where(TaskCoordinatorRun.task_id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        expiry = run.claim_expires_at if run is not None else None
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if (
            execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
            or execution.coordinator is None
            or run is None
            or plan_state.get(TASK_COORDINATOR_RUN_ID_KEY) != run.id
            or run.current_plan_id != plan.id
            or run.entity_id != task.entity_id
            or run.workspace_id != task.workspace_id
            or run.phase not in {
                TaskCoordinatorPhase.WAITING_HUMAN.value,
                TaskCoordinatorPhase.FAILED.value,
            }
            or (
                run.claim_token
                and expiry is not None
                and expiry > now
            )
            or run.selection_fingerprint
            != execution.coordinator.stable_fingerprint()
        ):
            raise TaskRetryError(
                409,
                "Task coordinator is not safely stopped on this exact Plan; "
                "resolve the active decision or wait for its owner to stop.",
            )
        if v3_failed_acceptance_plan:
            if not _retryable_acceptance_checkpoint(run.checkpoint):
                raise TaskRetryError(
                    409,
                    "The stopped coordinator has no saved retryable completion "
                    "review for this Plan; its prior actions must be reviewed first.",
                )
            marker = (
                run.checkpoint.get(_ACCEPTANCE_RETRY_KEY)
                if isinstance(run.checkpoint, dict) else None
            )
            refreshed = _reset_acceptance_retry_checkpoint(
                run.checkpoint, plan_id=plan.id, create=marker is None,
            )
            if refreshed is None:
                raise TaskRetryError(409, "Task completion review checkpoint changed; refresh and retry.")
            run.checkpoint = refreshed
            plan.status = ExecutionPlanStatus.RUNNING.value
            plan.started_at = now
            plan.completed_at = None
            plan.last_error = None
        elif isinstance(run.checkpoint, dict) and _ACCEPTANCE_RETRY_KEY in run.checkpoint:
            refreshed = _reset_acceptance_retry_checkpoint(
                run.checkpoint, plan_id=plan.id,
            )
            if refreshed is None:
                raise TaskRetryError(409, "Task completion review checkpoint changed; refresh and retry.")
            run.checkpoint = refreshed
        if (
            task.status == TaskStatus.BLOCKED.value
            and run.phase == TaskCoordinatorPhase.FAILED.value
            and v3_active_plan
        ):
            refreshed = _reset_coordinator_completion_checkpoint(run.checkpoint, plan_id=plan.id)
            if refreshed is not None:
                if (
                    plan.status in {status.value for status in PLAN_TERMINAL_STATUSES}
                    or plan.status == ExecutionPlanStatus.PENDING_APPROVAL.value
                    or plan.approval_required
                    or has_waiting_human_decision(plan_steps)
                    or any(lease.status not in {
                        WorkLeaseStatus.COMPLETED.value,
                        WorkLeaseStatus.FAILED.value,
                        WorkLeaseStatus.EXPIRED.value,
                        WorkLeaseStatus.CANCELLED.value,
                    } for lease in leases)
                ):
                    raise TaskRetryError(409, "The completion loop still has an active action or human decision.")
                pending_input = (await db.execute(
                    select(HitlRequest.id).where(
                        HitlRequest.entity_id == task.entity_id,
                        HitlRequest.origin_task_id == task.id,
                        HitlRequest.status == ApprovalStatus.PENDING.value,
                    ).limit(1)
                )).scalar_one_or_none()
                if pending_input is not None:
                    raise TaskRetryError(409, WAITING_HUMAN_RETRY_DETAIL)
                if await ambiguous_effect_reconciliation_snapshot(db, plan_steps, plan_id=plan.id) is not None:
                    raise TaskRetryError(409, "The completion loop has an uncertain action; resolve its existing continuation first.")
                run.checkpoint = refreshed
        mode = execution.mode
        plan_id = plan.id
        dispatch = execution
    elif plan and can_resume_pending_plan_dispatch:
        # The human decision and Step mutation already committed; only the
        # post-commit enqueue failed. Resume the executor without resetting the
        # Step or discarding review guidance stored on its error payload.
        dispatch = require_workspace_plan_resume_dispatch(
            task=task,
            plan=plan,
            steps=plan_steps,
            execution=execution,
        )
        mode = "plan_dispatch"
        plan_id = plan.id
        plan.status = ExecutionPlanStatus.RUNNING.value
        # This is a new admitted execution window, not a continuation of the
        # stale wall-clock budget that may have caused the prior run to stop.
        plan.started_at = now
        plan.completed_at = None
        plan.last_error = None
        details.pop("_pending_plan_dispatch", None)
    elif plan and plan.status not in {
        ExecutionPlanStatus.COMPLETED.value,
        ExecutionPlanStatus.CANCELLED.value,
        ExecutionPlanStatus.REPLANNED.value,
    } and not start_new_external_plan and replan_context is None:
        dispatch = require_workspace_plan_resume_dispatch(
            task=task,
            plan=plan,
            steps=plan_steps,
            execution=execution,
        )
        mode = "plan"
        plan_id = plan.id
        if not clean_note:
            discussion_guidance = await _task_thread_guidance_since_plan(
                db, task=task, plan=plan, before=now,
            )
            if discussion_guidance:
                retry_meta["plan_id"] = plan.id
                retry_meta["discussion_guidance"] = discussion_guidance
        has_waiting_human = has_waiting_human_decision(plan_steps)
        if has_waiting_human:
            raise TaskRetryError(409, WAITING_HUMAN_RETRY_DETAIL)
        retryable_steps = [
            step
            for step in plan_steps
            if step.step_status in RETRYABLE_EXECUTION_STEP_STATUSES
        ]
        effect_block = await retry_effect_block_detail(db, retryable_steps)
        if effect_block:
            raise TaskRetryError(409, effect_block)
        retried_parent_keys = {
            step.step_key
            for step in retryable_steps
            if not is_coordinator_child_params(step.params)
        }
        for step in retryable_steps:
            if is_coordinator_child_params(step.params):
                parent_key = str(
                    (step.params or {}).get(COORDINATOR_PARENT_STEP_PARAM_KEY)
                    or ""
                )
                if parent_key not in retried_parent_keys:
                    continue
                # Only the parent-owned admission changes lifecycle fields;
                # result/checkpoint/evidence remain for stable nested resume.
            from packages.core.plans.step_reset import renew_step_approval_intent
            renew_step_approval_intent(step)
            reset_step_to_retry_ready(step)
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
    elif execution.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        effect_block = await task_execution_effect_block_detail(db, task.id)
        if effect_block:
            raise TaskRetryError(409, effect_block)
        mode = execution.mode
        if plan is not None and not start_new_external_plan:
            from packages.core.plans.executor import _succeeded_step_contexts

            prior_context = details.get("_replan_context")
            details["_replan_context"] = {
                **(prior_context if isinstance(prior_context, dict) else {}),
                "prior_plan_id": plan.id,
                "succeeded_steps": _succeeded_step_contexts(plan_steps),
                "issue": (
                    "The prior attempt did not confirm the Task objective. "
                    "Reuse its recorded outputs. A done step alone does not prove an "
                    "external action happened; verify the outcome before repeating it."
                ),
            }
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
        dispatch = execution
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
        if execution.runtime_kind is TaskRuntimeKind.LEGACY_AGENT:
            effect_block = await task_execution_effect_block_detail(db, task.id)
            if effect_block:
                raise TaskRetryError(409, effect_block)
            mode = execution.mode
            if clean_note:
                details["_hitl_response"] = clean_note
                details["_hitl_responded_by"] = user_label
            dispatch = execution
        else:
            raise TaskRetryError(
                409,
                "No reusable execution plan or active task assignee is available. "
                "Assign an agent to this task before retrying.",
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
    from packages.core.services.task_execution_origin import record_manual_task_execution

    record_manual_task_execution(task, user_id=user_id, action="task_retry")
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
            "owner_service_key": task.owner_service_key,
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
    plan_wake = None
    if dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        if plan is None:
            from packages.core.services.task_plan_submission import (
                carry_pending_manual_submission_for_retry,
            )

            await carry_pending_manual_submission_for_retry(db, task=task)
        await stage_task_execution_intent(db, task, dispatch)
    elif dispatch.runtime_kind is TaskRuntimeKind.PLAN_RESUME:
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        plan_wake = await stage_plan_cycle_wake(
            db,
            dispatch.target_id,
            reason="task_retry_plan_resume",
        )
    await db.flush()
    return TaskRetryResult(
        task=task,
        mode=mode,
        plan_id=plan_id,
        reset_steps=reset_steps,
        dispatch=dispatch,
        plan_wake=plan_wake,
    )


async def mark_task_retry_dispatch_failed(
    db: AsyncSession,
    *,
    result: TaskRetryResult,
    error: Exception,
) -> Optional[Task]:
    """Return a committed manual retry to a visible, retryable state."""
    dispatch_kind = result.dispatch.mode
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

    if result.dispatch.runtime_kind is TaskRuntimeKind.PLAN_RESUME:
        # A Plan resume is already represented by its durable cycle outbox.
        # Re-arm that intent if this compatibility repair path is reached, but
        # never turn broker-hint failure into Plan/Task business failure.
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        await stage_plan_cycle_wake(
            db,
            result.plan_id,
            reason="task_retry_hint_recovery",
        )
        await db.flush()
        return task

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
    """Compatibility shim: re-arm durable continuation without state damage."""

    del user_id, is_retry
    from packages.core.plans.wakeup import stage_plan_cycle_wake

    wake = await stage_plan_cycle_wake(
        db,
        plan_id,
        reason=f"legacy_hint_failure:{reason}"[:160],
    )
    if wake is not None:
        logger.warning(
            "Plan %s broker hint failed; durable continuation was re-armed",
            plan_id,
        )
    return wake is not None


def dispatch_task_retry(result: TaskRetryResult) -> bool:
    """Enqueue prepared work after its database transaction commits."""
    if result.dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        # ``prepare_task_retry`` staged this start in the same transaction as
        # the Task transition. The durable publisher owns broker delivery.
        return True
    if result.dispatch.runtime_kind is TaskRuntimeKind.PLAN_RESUME:
        from packages.core.plans.wakeup import dispatch_plan_cycle_wake_hint

        if result.plan_wake is None:
            return False
        dispatch_plan_cycle_wake_hint(
            result.plan_wake,
            reason="task_retry_post_commit_hint",
        )
        # User-facing dispatch means the intent is durable; hint publication is
        # deliberately best-effort and recovered by the outbox sweep.
        return True
    return dispatch_task_execution(result.task.id, result.dispatch)
