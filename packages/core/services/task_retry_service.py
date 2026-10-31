"""Shared preparation and dispatch for manual task retries."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskLogType, TaskRuntimeKind, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.services.task_dependencies import (
    dependency_ids_from_details,
    details_with_dependency_state,
)
from packages.core.services.task_service import add_task_log
from packages.core.services.task_execution import (
    TaskExecutionDispatch,
    resolve_task_execution,
    stage_task_execution_intent,
    task_execution_intent_generation,
)
from packages.core.services.task_state_machine import apply_task_status_transition

logger = logging.getLogger(__name__)

AUTOMATIC_RETRY_COUNT_KEY = "_automatic_retry_count"


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
    checkpoint in its own shape (and the admission would fail later). A run
    that delegates durably is also refused while one of its delegated child
    runs is open.
    """
    from packages.core.ai.runtime.run_state import RUN_STATE_NEWER_RUNTIME_DETAIL, runtime_run_state_is_newer
    from packages.core.services.task_agent_loop_state import DELEGATION_MODE_DURABLE, DELEGATION_MODE_KEY

    checkpoint = (await db.execute(
        select(TaskCoordinatorRun.checkpoint).where(TaskCoordinatorRun.task_id == task.id)
    )).scalar_one_or_none()
    if isinstance(checkpoint, dict) and runtime_run_state_is_newer(checkpoint):
        logger.warning("Task %s retry refused: its checkpoint was saved by a newer runtime", task.id)
        raise TaskRetryError(409, RUN_STATE_NEWER_RUNTIME_DETAIL)
    if isinstance(checkpoint, dict) and checkpoint.get(DELEGATION_MODE_KEY) == DELEGATION_MODE_DURABLE:
        # Only a durable-delegation run has child runs at all.
        await _require_no_open_delegated_runs(db, task)


OPEN_DELEGATED_RUNS_DETAIL = (
    "Agents this Task delegated to are still working on it. Resume the Task to let them "
    "finish, or cancel it, before retrying."
)


async def _require_no_open_delegated_runs(db: AsyncSession, task: Task) -> None:
    """A retry never restarts a Task under delegated child runs that are still open.

    Defensive: a stopped root run stops its children in its own terminal
    write; children stay open only while their Task is paused.
    """

    from packages.core.models.task_agent_child import (
        TASK_AGENT_CHILD_TERMINAL_PHASES,
        TaskAgentChildRun,
    )

    open_child = (await db.execute(
        select(TaskAgentChildRun.id).where(
            TaskAgentChildRun.task_id == task.id,
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        ).limit(1)
    )).scalar_one_or_none()
    if open_child is not None:
        raise TaskRetryError(409, OPEN_DELEGATED_RUNS_DETAIL)


class TaskRetryError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class TaskRetryResult:
    task: Task
    mode: str
    dispatch: TaskExecutionDispatch


async def prepare_task_retry(
    db: AsyncSession,
    *,
    task: Task,
    user_id: Optional[str],
    user_label: str,
    note: Optional[str] = None,
    automatic: bool = False,
) -> TaskRetryResult:
    """Reset retryable state without committing or enqueueing work.

    A Workspace Task's agent loop reopens its stopped run on the staged start
    (``ensure_task_coordinator_run``): it continues its own transcript, or
    starts fresh when the run was closed out from the retired Plan engine. A
    legacy Agent Task re-runs with the note as its human response.

    ``automatic`` is a platform retry (no person asked): it runs every safety
    check of a manual retry but records a system actor and no manual-execution
    receipt, so the Workspace's automatic-running switch still applies.
    """
    if not automatic and not user_id:
        raise ValueError("A manual Task retry requires the requesting user")
    scope_entity_id = task.entity_id
    scope_workspace_id = task.workspace_id
    if scope_workspace_id:
        from packages.core.services.workspace_access import lock_workspace_access_boundary

        # Workspace -> Task, as every Task admission. Chat recovery also owns
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
    if automatic:
        # Host bookkeeping (underscore keys are not execution inputs): bounds
        # how often the platform retries a Task on its own.
        details[AUTOMATIC_RETRY_COUNT_KEY] = int(details.get(AUTOMATIC_RETRY_COUNT_KEY) or 0) + 1

    if dependency_ids_from_details(details):
        details = await details_with_dependency_state(db, task, details)
        if details.get("dependency_status") != "completed":
            raise TaskRetryError(
                409,
                "Task dependencies are not completed yet; waiting for predecessor outputs.",
            )

    restored_runtime_context = False
    if _has_unconsumed_proposal_external_action(details):
        restored_runtime_context = await _restore_compatible_external_runtime_context(
            db,
            task=task,
            details=details,
        )
    # A planner-recorded capability gap from the retired Plan engine no
    # longer describes this run; the loop acquires what it needs itself.
    details.pop("capability_blocker", None)

    execution = await resolve_task_execution(db, task)
    if execution.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        mode = execution.mode
    else:
        raise TaskRetryError(
            409,
            "No active task assignee is available. "
            "Assign an agent to this task before retrying.",
        )

    await apply_task_status_transition(
        task,
        TaskStatus.IN_PROGRESS.value,
        now=now,
        db=db,
        actor_kind=TaskActor.SYSTEM.value if automatic else "user",
        actor_id=user_id,
    )
    task.started_at = now
    task.completed_at = None
    if clean_note:
        # The note belongs to the run this retry starts and to no later one.
        retry_meta["lifecycle_generation"] = task_execution_intent_generation(task)
    task.details = details
    task.actual_output = None
    if not automatic:
        from packages.core.services.task_execution_origin import record_manual_task_execution

        record_manual_task_execution(task, user_id=user_id, action="task_retry")
    await add_task_log(
        db,
        task.id,
        TaskLogType.MANUAL_RETRY,
        ("Automatic retry" if automatic else "Manual retry requested")
        + (f": {clean_note}" if clean_note else ""),
        actor=TaskActor.SYSTEM if automatic else TaskActor.USER,
        created_by=user_label,
        metadata={
            "mode": mode,
            "retry_count": retry_count,
            "requested_by": user_id,
            "automatic": automatic,
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
            "mode": mode,
            "retry_count": retry_count,
            "requested_by": user_id,
            "runtime_context_restored": restored_runtime_context,
        },
        workspace_id=task.workspace_id,
        deliver_after_commit=True,
    )
    if execution.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        await stage_task_execution_intent(db, task, execution)
    await db.flush()
    return TaskRetryResult(task=task, mode=mode, dispatch=execution)


async def mark_task_retry_dispatch_failed(
    db: AsyncSession,
    *,
    result: TaskRetryResult,
    error: Exception,
) -> Optional[Task]:
    """Return a committed manual retry to a visible, retryable state."""
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

    error_message = str(error)[:1000] or error.__class__.__name__
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
            "dispatch_kind": result.dispatch.mode,
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
                    plan_id=None,
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


def dispatch_task_retry(result: TaskRetryResult) -> bool:
    """Enqueue prepared work after its database transaction commits."""
    if result.dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        # ``prepare_task_retry`` staged this start in the same transaction as
        # the Task transition. The durable publisher owns broker delivery.
        return True
    raise ValueError(
        f"Task runtime {result.dispatch.runtime_kind.value!r} is not dispatchable"
    )
