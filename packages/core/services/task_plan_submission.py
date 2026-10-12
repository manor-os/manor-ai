"""Durable, immutable API requests for a V3 Task coordinator's first Plan."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import PLAN_TERMINAL_STATUSES
from packages.core.constants.task import TaskRuntimeKind, TaskStatus
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.task_plan_submission import (
    TaskPlanSubmission,
    TaskPlanSubmissionKind,
    TaskPlanSubmissionState,
)
from packages.core.plans.coordinator import validate_coordinator_source_plan
from packages.core.plans.schema import Plan
from packages.core.plans.service import bind_task_output_contract, plan_contract_gaps
from packages.core.services.task_execution import (
    resolve_task_execution,
    stage_task_execution_intent,
    task_execution_intent_generation,
)


class TaskPlanSubmissionConflict(ValueError):
    pass


def _fingerprint(*, kind: str, mode: str, approval_required: bool, plan_dag: dict | None) -> str:
    canonical = json.dumps(
        {"kind": kind, "mode": mode, "approval_required": approval_required, "plan": plan_dag},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manual_task_plan_fingerprint(
    *, execution_mode: str, approval_required: bool, plan: Plan,
) -> str:
    return _fingerprint(
        kind=TaskPlanSubmissionKind.MANUAL.value,
        mode=execution_mode,
        approval_required=approval_required,
        plan_dag=plan.model_dump(mode="json"),
    )


async def latest_task_plan_submission(
    db: AsyncSession, *, task_id: str,
) -> TaskPlanSubmission | None:
    """Latest immutable request, including rejected/applied and planner rows."""
    return (await db.execute(
        select(TaskPlanSubmission).where(TaskPlanSubmission.task_id == task_id)
        .order_by(TaskPlanSubmission.created_at.desc(), TaskPlanSubmission.id.desc())
        .limit(1).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()


def _reject_manual_runtime_params(plan: Plan) -> None:
    # Params are task inputs, never a way to set Runtime authorization. The
    # shared source-DAG validator separately rejects coordinator/lease markers.
    forbidden = {"allowed_tool_names", "allowed_tools", "tool_bindings", "tool_profile"}
    for step in plan.steps:
        privileged = sorted(
            str(key) for key in step.params
            if str(key) in forbidden or str(key).startswith(("_runtime_", "runtime_"))
        )
        if privileged:
            raise ValueError(
                f"step {step.key}: params cannot set Runtime tool authority: "
                + ", ".join(privileged)
            )


async def submit_task_plan_request(
    db: AsyncSession,
    *,
    task: Task,
    requested_by_user_id: str,
    kind: TaskPlanSubmissionKind,
    execution_mode: str,
    approval_required: bool = False,
    plan: Plan | None = None,
    source_submission_id: str | None = None,
) -> TaskPlanSubmission:
    """Admit under caller-held Workspace -> Task locks; stage a Task wake atomically.

    The API cannot own a TaskCoordinator claim or create a Plan synchronously.
    During the same in-progress Task generation, repeating the exact request
    returns its original receipt; a changed request cannot overwrite it.
    This is not an all-lifecycle response-idempotency key: once the Task leaves
    in-progress, callers must inspect the Task/Plan status.
    """
    if task.status != TaskStatus.IN_PROGRESS.value or not task.workspace_id:
        raise TaskPlanSubmissionConflict("Task must be an in-progress Workspace Task")
    if execution_mode not in {"live", "sandbox"}:
        raise ValueError("execution_mode must be live or sandbox")
    if kind is TaskPlanSubmissionKind.MANUAL and plan is None:
        raise ValueError("manual Plan submission requires a source DAG")
    if kind is TaskPlanSubmissionKind.PLANNER and plan is not None:
        raise ValueError("Planner submission must not supply a DAG")

    dispatch = await resolve_task_execution(db, task, lock_subscriptions=True)
    if dispatch.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN or dispatch.coordinator is None:
        raise TaskPlanSubmissionConflict("Task has no active V3 coordinator service scope")

    plan_dag = None
    if plan is not None:
        _reject_manual_runtime_params(plan)
        source = bind_task_output_contract(plan, task.expected_output)
        validate_coordinator_source_plan(source, coordinator=dispatch.coordinator)
        gaps = plan_contract_gaps(
            source.topo_order(),
            task_expected_output=task.expected_output,
            require_explicit_agent_outputs=True,
        )
        if gaps:
            from packages.core.plans.service import PlanContractError

            raise PlanContractError(gaps)
        plan_dag = plan.model_dump(mode="json")
        if len(json.dumps(plan_dag, ensure_ascii=False).encode("utf-8")) > 1_000_000:
            raise ValueError("manual Plan DAG exceeds the 1 MB submission limit")

    generation = task_execution_intent_generation(task)
    fingerprint = _fingerprint(
        kind=kind.value, mode=execution_mode,
        approval_required=approval_required, plan_dag=plan_dag,
    )
    existing = (await db.execute(
        select(TaskPlanSubmission).where(
            TaskPlanSubmission.task_id == task.id,
            TaskPlanSubmission.lifecycle_generation == generation,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise TaskPlanSubmissionConflict(
                "This Task start already has a different immutable Plan submission; "
                "finish or explicitly retry the Task before changing it"
            )
        return existing

    active_plans = list((await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.task_id == task.id,
            ExecutionPlan.entity_id == task.entity_id,
            ExecutionPlan.workspace_id == task.workspace_id,
            ExecutionPlan.status.not_in(tuple(status.value for status in PLAN_TERMINAL_STATUSES)),
        ).limit(1)
    )).scalars().all())
    if active_plans:
        raise TaskPlanSubmissionConflict(
            "This Task already has an active Plan; inspect or retry that Plan through its Task"
        )
    run = (await db.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if run is not None:
        expiry = run.claim_expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if run.current_plan_id or run.phase not in {
            TaskCoordinatorPhase.READY.value,
            TaskCoordinatorPhase.WAITING_HUMAN.value,
        } or (run.claim_token and expiry is not None and expiry > datetime.now(timezone.utc)):
            raise TaskPlanSubmissionConflict(
                "Task coordinator has started planning; a new Plan request cannot replace its current work"
            )

    submission = TaskPlanSubmission(
        task_id=task.id,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        lifecycle_generation=generation,
        request_kind=kind.value,
        requested_by_user_id=requested_by_user_id,
        execution_mode=execution_mode,
        approval_required=approval_required,
        plan_dag=plan_dag,
        request_fingerprint=fingerprint,
        source_submission_id=source_submission_id,
        state=TaskPlanSubmissionState.PENDING.value,
    )
    from packages.core.services.task_execution_origin import record_manual_task_execution

    record_manual_task_execution(task, user_id=requested_by_user_id, action="plan_request")
    db.add(submission)
    await db.flush()
    await stage_task_execution_intent(db, task, dispatch)
    return submission


async def pending_task_plan_submission(
    db: AsyncSession, *, task: Task,
) -> TaskPlanSubmission | None:
    """Return only the current generation's unconsumed request."""
    return (await db.execute(
        select(TaskPlanSubmission).where(
            TaskPlanSubmission.task_id == task.id,
            TaskPlanSubmission.entity_id == task.entity_id,
            TaskPlanSubmission.workspace_id == task.workspace_id,
            TaskPlanSubmission.lifecycle_generation == task_execution_intent_generation(task),
            TaskPlanSubmission.state == TaskPlanSubmissionState.PENDING.value,
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def carry_pending_manual_submission_for_retry(db: AsyncSession, *, task: Task) -> None:
    """Explicit Task retry re-pins an unapplied manual DAG to its new generation.

    The old row is immutable. An applied DAG is never copied to replan a Task.
    Caller holds Workspace -> Task and has already advanced the lifecycle clock.
    """
    generation = task_execution_intent_generation(task)
    current = (await db.execute(select(TaskPlanSubmission).where(
        TaskPlanSubmission.task_id == task.id,
        TaskPlanSubmission.lifecycle_generation == generation,
    ))).scalar_one_or_none()
    if current is not None:
        return
    prior = await latest_task_plan_submission(db, task_id=task.id)
    if (
        prior is None
        or prior.request_kind != TaskPlanSubmissionKind.MANUAL.value
        or prior.state != TaskPlanSubmissionState.PENDING.value
        or prior.plan_id is not None
    ):
        return
    db.add(TaskPlanSubmission(
        task_id=task.id,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        lifecycle_generation=generation,
        request_kind=prior.request_kind,
        requested_by_user_id=prior.requested_by_user_id,
        execution_mode=prior.execution_mode,
        approval_required=prior.approval_required,
        plan_dag=prior.plan_dag,
        request_fingerprint=prior.request_fingerprint,
        source_submission_id=prior.id,
        state=TaskPlanSubmissionState.PENDING.value,
    ))
    await db.flush()
