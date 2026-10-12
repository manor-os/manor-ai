"""ExecutionPlan API.

Endpoints:
  GET    /api/v1/plans                      list plans for the entity
  GET    /api/v1/plans/{id}                 plan detail (DAG + step rows)
  POST   /api/v1/plans                      manually create a plan from
                                             a Pydantic Plan body (devs)
  POST   /api/v1/plans/from-task/{task_id}  invoke Planner for a task
  POST   /api/v1/plans/{id}/approve         flip pending_approval→running
                                             and dispatch the executor
  POST   /api/v1/plans/{id}/cancel          stop a non-terminal plan
  POST   /api/v1/plans/{id}/retry-failed-steps
                                             reset retryable failed steps
  POST   /api/v1/plans/steps/{step_id}/retry
                                             reset one retryable step
  GET    /api/v1/plans/{id}/steps           list steps under a plan
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, JsonValue
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import (
    get_current_user,
    require_workspace_authority,
    require_workspace_readable,
    require_workspace_writable,
)
from packages.core.constants.task import TaskLogType, TaskRuntimeKind, TaskStatus
from packages.core.constants.execution import (
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    ExecutionPlanStatus,
    PLAN_TERMINAL_STATUSES,
    is_coordinator_child_params,
)
from packages.core.dispatcher.effect_safety import (
    retry_effect_block_detail,
    task_execution_effect_block_detail,
)
from packages.core.constants.task_actors import TaskActor
from packages.core.database import get_db
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.plans import (
    cancel_plan,
    create_plan_from_dag,
    get_plan,
    list_plan_steps,
)
from packages.core.plans.step_reset import (
    never_ran_downstream,
    reset_never_ran_downstream_steps,
    reset_step_to_retry_ready,
)
from packages.core.plans.planner import (
    CapabilityError,
    PlannerError,
    TaskPlanClaimHeldError,
    TaskPlanOwnedByCoordinatorError,
    plan_task_and_commit,
    task_plan_admission_error,
)
from packages.core.plans.schema import Plan
from packages.core.plans.service import (
    ActiveTaskPlanError,
    PlanContractError,
    TaskEffectReplayBlockedError,
)
from packages.core.services.task_service import add_task_log
from packages.core.services.workflow_run_execution_claim import (
    TaskPlanExecutionClaimLost,
)
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    workspace_resource_not_soft_deleted,
)
from packages.core.services.task_retry_service import (
    RETRYABLE_EXECUTION_STEP_STATUSES,
    TASK_RECOVERY_RETRY_DETAIL,
    WAITING_HUMAN_RETRY_DETAIL,
    TaskRetryError,
    has_pending_task_recovery,
    has_waiting_human_decision,
    is_plan_dispatch_recovery,
    plan_retry_block_detail,
    _persisted_plan_replan_context,
    require_workspace_plan_resume_dispatch,
)


router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


# ── Schemas ────────────────────────────────────────────────────────────

class PlanResponse(BaseModel):
    id: str
    entity_id: str
    workspace_id: Optional[str]
    task_id: Optional[str]
    task_status: Optional[str] = None
    task_title: Optional[str] = None
    agent_subscription_id: Optional[str]
    status: str
    execution_mode: str
    approval_required: bool
    plan_dag: dict
    planner_version: Optional[str]
    parent_plan_id: Optional[str]
    cost_tracking: dict
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    last_error: Optional[dict]
    created_at: datetime
    updated_at: Optional[datetime]


class StepResponse(BaseModel):
    id: str
    plan_id: str
    step_key: str
    kind: str
    service_key: Optional[str]
    resolved_subscription_id: Optional[str] = None
    resolved_agent_id: Optional[str] = None
    resolved_subscription_name: Optional[str] = None
    resolved_agent_name: Optional[str] = None
    resolved_agent_avatar: Optional[str] = None
    provider: Optional[str]
    action_key: Optional[str]
    integration_id: Optional[str]
    params: dict
    result: JsonValue | None
    depends_on: list[str]
    step_status: str
    risk_level: str
    requires_approval: bool
    attempt_count: int
    max_attempts: int
    cost: dict
    error: Optional[dict]
    human_input_prompt: Optional[str]
    human_input_response: Optional[dict]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class PlanCreateRequest(BaseModel):
    task_id: Optional[str] = None
    workspace_id: Optional[str] = None
    agent_subscription_id: Optional[str] = None
    execution_mode: str = "live"
    approval_required: bool = False
    plan: Plan


class PlanFromTaskRequest(BaseModel):
    execution_mode: str = "live"


class TaskPlanAdmissionResponse(BaseModel):
    submission_id: str
    task_id: str
    status: str
    plan_id: Optional[str]
    runtime: str = "task_coordinator_v3"


class RetryRequest(BaseModel):
    note: Optional[str] = None


class RetryPlanResponse(BaseModel):
    plan: PlanResponse
    reset_steps: int
    dispatched: bool


class RetryStepResponse(BaseModel):
    plan: PlanResponse
    step: StepResponse
    dispatched: bool


# ── Helpers ────────────────────────────────────────────────────────────

def _to_plan(p: ExecutionPlan, task_status: str | None = None, task_title: str | None = None) -> PlanResponse:
    return PlanResponse(
        id=p.id, entity_id=p.entity_id, workspace_id=p.workspace_id,
        task_id=p.task_id, task_status=task_status, task_title=task_title,
        agent_subscription_id=p.agent_subscription_id,
        status=p.status, execution_mode=p.execution_mode,
        approval_required=p.approval_required,
        plan_dag=p.plan_dag or {},
        planner_version=p.planner_version,
        parent_plan_id=p.parent_plan_id,
        cost_tracking=p.cost_tracking or {},
        started_at=p.started_at, completed_at=p.completed_at,
        last_error=p.last_error,
        created_at=p.created_at, updated_at=p.updated_at,
    )


def _to_step(
    s: ExecutionStep,
    *,
    subscriptions_by_id: dict[str, AgentSubscription] | None = None,
    subscriptions_by_service: dict[str, AgentSubscription] | None = None,
    agents_by_id: dict[str, Agent] | None = None,
) -> StepResponse:
    subscriptions_by_id = subscriptions_by_id or {}
    subscriptions_by_service = subscriptions_by_service or {}
    agents_by_id = agents_by_id or {}
    subscription = None
    if s.resolved_subscription_id:
        subscription = subscriptions_by_id.get(s.resolved_subscription_id)
    if subscription is None and s.service_key:
        subscription = subscriptions_by_service.get(s.service_key)
    agent_id = s.resolved_agent_id or (subscription.agent_id if subscription else None)
    agent = agents_by_id.get(agent_id) if agent_id else None
    return StepResponse(
        id=s.id, plan_id=s.plan_id, step_key=s.step_key, kind=s.kind,
        service_key=s.service_key, provider=s.provider,
        resolved_subscription_id=s.resolved_subscription_id or (subscription.id if subscription else None),
        resolved_agent_id=agent_id,
        resolved_subscription_name=subscription.name if subscription else None,
        resolved_agent_name=agent.name if agent else None,
        resolved_agent_avatar=getattr(agent, "avatar_url", None) if agent else None,
        action_key=s.action_key, integration_id=s.integration_id,
        params=s.params or {}, result=s.result,
        depends_on=list(s.depends_on or []),
        step_status=s.step_status, risk_level=s.risk_level,
        requires_approval=s.requires_approval,
        attempt_count=s.attempt_count, max_attempts=s.max_attempts,
        cost=s.cost or {}, error=s.error,
        human_input_prompt=s.human_input_prompt,
        human_input_response=s.human_input_response,
        started_at=s.started_at, finished_at=s.finished_at,
    )


async def _step_display_lookups(
    db: AsyncSession,
    steps: list[ExecutionStep],
    *,
    entity_id: str,
    workspace_id: str | None,
) -> tuple[dict[str, AgentSubscription], dict[str, AgentSubscription], dict[str, Agent]]:
    """Resolve step subscription/agent display data using workspace-chat semantics."""

    subscription_ids = {
        s.resolved_subscription_id
        for s in steps
        if s.resolved_subscription_id
    }
    service_keys = {
        s.service_key
        for s in steps
        if s.service_key
    }

    subscriptions: list[AgentSubscription] = []
    filters = []
    if subscription_ids:
        filters.append(AgentSubscription.id.in_(subscription_ids))
    if workspace_id and service_keys:
        filters.append(
            (AgentSubscription.workspace_id == workspace_id)
            & AgentSubscription.service_key.in_(service_keys)
        )
    if filters:
        subscriptions = list((await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.status == "active",
                or_(*filters),
            )
        )).scalars().all())

    subscriptions_by_id = {s.id: s for s in subscriptions}
    subscriptions_by_service: dict[str, AgentSubscription] = {}
    for subscription in subscriptions:
        if subscription.service_key and subscription.service_key not in subscriptions_by_service:
            subscriptions_by_service[subscription.service_key] = subscription

    agent_ids = {
        s.resolved_agent_id
        for s in steps
        if s.resolved_agent_id
    }
    agent_ids.update(
        subscription.agent_id
        for subscription in subscriptions
        if subscription.agent_id
    )
    agents: dict[str, Agent] = {}
    if agent_ids:
        agents = {
            agent.id: agent
            for agent in (await db.execute(
                select(Agent).where(
                    Agent.id.in_(agent_ids),
                    or_(Agent.entity_id == entity_id, Agent.entity_id.is_(None)),
                )
            )).scalars().all()
        }

    return subscriptions_by_id, subscriptions_by_service, agents


_RESETTABLE_PLAN_STATUSES = {"failed", "needs_attention", "paused", "cancelled", "completed"}


def _reject_replanned_plan_retry(plan: ExecutionPlan) -> None:
    if plan.status == ExecutionPlanStatus.REPLANNED.value:
        raise HTTPException(
            409,
            "Replanned execution history cannot be retried; retry the current Task or Plan instead.",
        )


def _reject_task_coordinator_step_retry(plan: ExecutionPlan) -> None:
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
    )

    state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
    if state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION:
        raise HTTPException(
            409,
            "This Plan is owned by the Task coordinator; retry the Task or "
            "let its coordinator replan instead of resetting a Step directly.",
        )


def _reject_task_coordinator_plan_cancel(plan: ExecutionPlan) -> None:
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
    )

    state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
    if state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION:
        raise HTTPException(
            409,
            "This Plan is owned by the Task coordinator; cancel the Task instead.",
        )


async def _lock_plan_mutation_origin(
    db: AsyncSession,
    *,
    plan_id: str,
    user: User,
) -> ExecutionPlan | None:
    """Serialize every user mutation surface for one Plan.

    Workspace-scoped mutations take the lifecycle row first. Task-bound Plans
    then share the Task row lock already used by direct Task retry and Workspace
    Chat recovery; taskless Plans use their Plan row as the final mutex. The
    Plan is reloaded after locking so a follower evaluates committed state.
    """
    plan_scope = (await db.execute(
        select(
            ExecutionPlan.id,
            ExecutionPlan.task_id,
            ExecutionPlan.workspace_id,
        ).where(
            ExecutionPlan.id == plan_id,
            ExecutionPlan.entity_id == user.entity_id,
        )
    )).one_or_none()
    if plan_scope is None:
        return None

    await _lock_writable_workspace_for_plan(
        db,
        user=user,
        workspace_id=plan_scope.workspace_id,
    )

    if plan_scope.task_id:
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == plan_scope.task_id,
                Task.entity_id == user.entity_id,
                Task.workspace_id == plan_scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            return None
        task_plans = list((await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.entity_id == user.entity_id,
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.workspace_id == plan_scope.workspace_id,
            )
            .order_by(ExecutionPlan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        return next((candidate for candidate in task_plans if candidate.id == plan_id), None)

    return (await db.execute(
        select(ExecutionPlan)
        .where(
            ExecutionPlan.id == plan_id,
            ExecutionPlan.entity_id == user.entity_id,
            ExecutionPlan.task_id.is_(None),
            ExecutionPlan.workspace_id == plan_scope.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _require_authoritative_task_plan_retry(
    db: AsyncSession,
    plan: ExecutionPlan,
) -> None:
    """Reject stale history before a retry can create a second active Plan."""

    if not plan.task_id:
        return
    task_plans = list((await db.execute(
        select(ExecutionPlan)
        .where(
            ExecutionPlan.task_id == plan.task_id,
            ExecutionPlan.entity_id == plan.entity_id,
            ExecutionPlan.workspace_id == plan.workspace_id,
        )
        .order_by(ExecutionPlan.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    latest = max(
        task_plans,
        key=lambda candidate: (candidate.created_at, candidate.id),
        default=None,
    )
    terminal_statuses = {status.value for status in PLAN_TERMINAL_STATUSES}
    active_others = [
        candidate
        for candidate in task_plans
        if candidate.id != plan.id and candidate.status not in terminal_statuses
    ]
    if latest is None or latest.id != plan.id or active_others:
        raise HTTPException(
            409,
            "Only the Task's latest Plan can be retried, and no other Plan may be active.",
        )

    # A historical Plan is not an execution-engine capability. Re-enter the
    # same closed Task factory used by first starts and Task-level retry, then
    # verify that every persisted subscription pin still belongs to the exact
    # current service selection before any Step is reset.
    task = (await db.execute(
        select(Task).where(
            Task.id == plan.task_id,
            Task.entity_id == plan.entity_id,
            Task.workspace_id == plan.workspace_id,
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(409, "The Plan's Task no longer exists in this Workspace.")
    steps = list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id == plan.id)
        .order_by(ExecutionStep.created_at, ExecutionStep.id)
        .execution_options(populate_existing=True)
    )).scalars().all())
    from packages.core.services.task_execution import resolve_task_execution

    execution = await resolve_task_execution(db, task)
    try:
        require_workspace_plan_resume_dispatch(
            task=task,
            plan=plan,
            steps=steps,
            execution=execution,
        )
    except TaskRetryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


async def _validate_manual_plan_scope(
    db: AsyncSession,
    *,
    user: User,
    request: PlanCreateRequest,
):
    """Authorize and bind every caller-supplied Plan owner reference."""
    await _lock_writable_workspace_for_plan(
        db,
        user=user,
        workspace_id=request.workspace_id,
    )

    task: Task | None = None
    rejected_receipt = None
    if request.task_id:
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == request.task_id,
                Task.entity_id == user.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            raise HTTPException(404, "task not found")
        if task.workspace_id != request.workspace_id:
            raise HTTPException(400, "task workspace does not match plan workspace")
        if task.status == TaskStatus.BLOCKED.value and task.workspace_id:
            from packages.core.models.task_plan_submission import (
                TaskPlanSubmissionKind,
                TaskPlanSubmissionState,
            )
            from packages.core.services.task_plan_submission import latest_task_plan_submission

            candidate = await latest_task_plan_submission(db, task_id=task.id)
            if (
                candidate is not None
                and candidate.request_kind == TaskPlanSubmissionKind.MANUAL.value
                and candidate.state == TaskPlanSubmissionState.REJECTED.value
                and candidate.plan_id is None
            ):
                rejected_receipt = candidate
            else:
                _require_task_plan_entry(task)
        else:
            _require_task_plan_entry(task)

    subscription: AgentSubscription | None = None
    if request.agent_subscription_id:
        subscription = (await db.execute(
            select(AgentSubscription)
            .where(
                AgentSubscription.id == request.agent_subscription_id,
                AgentSubscription.entity_id == user.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if subscription is None:
            raise HTTPException(404, "agent subscription not found")
        if subscription.workspace_id != request.workspace_id:
            raise HTTPException(
                400,
                "agent subscription workspace does not match plan workspace",
            )
        if subscription.status != "active":
            raise HTTPException(400, "agent subscription is not active")

    if task is not None:
        coordinator = None
        if task.workspace_id:
            from packages.core.services.task_execution import resolve_task_execution

            execution = await resolve_task_execution(db, task)
            coordinator = execution.coordinator
            if (
                execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
                or coordinator is None
            ):
                raise HTTPException(
                    409,
                    f"Task runtime {execution.runtime_kind.value!r} cannot create "
                    "a new Plan",
                )
            allowed_service_keys = coordinator.service_keys
        else:
            # Preserve the legacy entity-level Plan boundary. Coordinator
            # factory/fold is a Workspace Task invariant.
            allowed_service_keys = frozenset(
                str(service_key)
                for service_key in (
                    task.owner_service_key,
                    *(task.delegate_service_keys or []),
                )
                if service_key
            )
        requested_service_keys = {
            str(step.service_key)
            for step in request.plan.steps
            if step.service_key
        }
        if not requested_service_keys.issubset(allowed_service_keys):
            raise HTTPException(
                400,
                "plan service keys exceed the task owner/delegate scope",
            )
        if (
            coordinator is not None
            and
            subscription is not None
            and subscription.id != coordinator.root.subscription_id
        ):
            raise HTTPException(
                400,
                "agent subscription does not match the Task runtime's "
                "coordinator root",
            )
    return rejected_receipt


def _require_task_plan_entry(task: Task) -> None:
    """Keep non-runnable Tasks out of background Plan execution."""
    if admission_error := task_plan_admission_error(task):
        raise HTTPException(409, admission_error)


async def _lock_writable_workspace_for_plan(
    db: AsyncSession,
    *,
    user: User,
    workspace_id: str | None,
) -> None:
    """Serialize Plan admission/mutation with Workspace lifecycle changes."""
    normalized_workspace_id = str(workspace_id or "").strip()
    if not normalized_workspace_id:
        return
    workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=normalized_workspace_id,
        entity_id=user.entity_id,
    )
    from packages.core.services.workspace_autonomy import workspace_allows_manual_work

    if not workspace_allows_manual_work(workspace):
        raise HTTPException(403, "You do not have write access to this workspace")
    await require_workspace_writable(db, user, normalized_workspace_id)


async def _lock_task_for_plan(
    db: AsyncSession,
    *,
    user: User,
    task_id: str,
) -> Task:
    """Lock Workspace before Task, then revalidate the Task admission snapshot."""
    task_scope = (await db.execute(
        select(Task.id, Task.workspace_id).where(
            Task.id == task_id,
            Task.entity_id == user.entity_id,
        )
    )).one_or_none()
    if task_scope is None:
        raise HTTPException(404, "task not found")
    await _lock_writable_workspace_for_plan(
        db,
        user=user,
        workspace_id=task_scope.workspace_id,
    )
    task = (await db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.entity_id == user.entity_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "task not found")
    if task.workspace_id != task_scope.workspace_id:
        raise HTTPException(409, "task workspace changed during Plan admission")
    _require_task_plan_entry(task)
    return task


async def _authorize_task_for_planner(
    db: AsyncSession,
    *,
    user: User,
    task_id: str,
) -> None:
    """Authorize under locks, then release them before provider execution."""
    savepoint = await db.begin_nested()
    try:
        await _lock_task_for_plan(db, user=user, task_id=task_id)
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


async def _maybe_replan_stale_plan(
    db: AsyncSession,
    plan: ExecutionPlan,
    *,
    user: User,
    note: str | None,
    has_waiting_human: bool,
) -> bool:
    """Move a structurally stale or task-conflicting DAG to a fresh Planner."""
    if not plan.task_id:
        return False

    task = (await db.execute(
        select(Task).where(Task.id == plan.task_id, Task.entity_id == plan.entity_id)
    )).scalar_one_or_none()
    if task is None:
        return False

    replan_context = _persisted_plan_replan_context(
        plan,
        task_expected_output=getattr(task, "expected_output", None),
        task_details=getattr(task, "details", None),
    )
    if replan_context is None:
        return False
    effect_block = await task_execution_effect_block_detail(db, plan.task_id)
    if effect_block:
        raise HTTPException(409, effect_block)
    if has_waiting_human:
        raise HTTPException(409, WAITING_HUMAN_RETRY_DETAIL)

    from packages.core.services.task_execution import (
        resolve_task_execution,
        stage_task_execution_intent,
    )

    execution = await resolve_task_execution(db, task)
    if execution.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN:
        raise HTTPException(
            409,
            "Stored execution plan needs structural replanning, but the Task "
            "does not have an executable coordinated Workspace service scope.",
        )

    detail = str(replan_context["contract_gaps"])
    now = datetime.now(timezone.utc)
    details = dict(task.details or {})
    existing = (
        dict(details.get("_replan_context"))
        if isinstance(details.get("_replan_context"), dict)
        else {}
    )
    if replan_context.get("reason") == "task_constraint_contract_conflict":
        existing.pop("artifact_recovery", None)
    details["_replan_context"] = {
        **existing,
        **{
            key: value
            for key, value in replan_context.items()
            if key not in {"error_type", "retry_mode"}
        },
    }
    task.details = details
    plan.status = ExecutionPlanStatus.REPLANNED.value
    plan.completed_at = now
    plan.last_error = {
        "type": replan_context.get("error_type", "PlanContractMigrationRequired"),
        "message": detail,
    }
    await _record_retry_on_task(
        db,
        plan,
        user=user,
        mode=replan_context.get("retry_mode", "plan_contract_replan"),
        reset_step_ids=[],
        note=note,
    )
    # Persist the replacement-planning intent in the same transaction as the
    # old Plan's terminal state and Task replan context.  A broker outage can
    # delay delivery, but cannot strand a committed replan between those two
    # state changes.
    await stage_task_execution_intent(db, task, execution)
    return True


async def _dispatch_task_replan(
    db: AsyncSession,
    plan: ExecutionPlan,
) -> bool:
    """Report a replan whose durable Task intent was staged before commit.

    The name is retained for API response compatibility.  Broker publication
    belongs exclusively to the Runtime outbox; doing it here after commit
    would recreate the delivery-loss window the outbox closes.
    """
    if not plan.task_id:
        return False
    if plan.task_id:
        from packages.core.services.realtime import broadcast_task_runtime_update

        await broadcast_task_runtime_update(
            plan.entity_id,
            task_id=plan.task_id,
            workspace_id=plan.workspace_id,
            plan_id=plan.id,
            event="task_replan_dispatched",
        )
    return True


def _reset_step_for_retry(
    step: ExecutionStep,
    *,
    user: User,
    note: str | None = None,
    preserve_result: bool = False,
) -> None:
    from packages.core.plans.step_reset import renew_step_approval_intent
    renew_step_approval_intent(step)
    reset_step_to_retry_ready(step)
    step.error = None
    step.finished_at = None
    step.started_at = None
    step.attempt_count = 0
    step.human_input_prompt = None
    if not preserve_result:
        step.result = None
    step.human_input_response = None
    if note:
        step.human_input_response = {
            "response": note,
            "user": user.display_name or user.email,
            "user_id": user.id,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }


def _is_coordinator_child_step(step: ExecutionStep) -> bool:
    return is_coordinator_child_params(step.params)


def _never_ran_downstream(step: ExecutionStep) -> bool:
    """Skipped, or failed at the executor's boundary gate without running."""
    return never_ran_downstream(step)


def _reset_skipped_downstream_steps_for_retry(
    steps: list[ExecutionStep],
    *,
    retried_step_keys: set[str],
    user: User,
    note: str | None = None,
) -> list[ExecutionStep]:
    """Reset never-ran descendants unblocked by the retried step.

    The algorithm lives in ``packages.core.plans.step_reset`` — the supervisor's
    step re-run revives the same chain — and this surface adds the operator
    note to every revived step.
    """
    return reset_never_ran_downstream_steps(
        steps,
        retried_step_keys=retried_step_keys,
        reset_step=lambda step: _reset_step_for_retry(step, user=user, note=note),
    )


def _revive_plan_for_retry(plan: ExecutionPlan) -> None:
    if plan.status in _RESETTABLE_PLAN_STATUSES:
        plan.status = ExecutionPlanStatus.DRAFT.value
    plan.completed_at = None
    plan.last_error = None


async def _dispatch_plan(
    db: AsyncSession,
    plan: ExecutionPlan,
    *,
    user_id: str,
    reason: str,
    is_retry: bool = True,
) -> bool:
    del user_id, is_retry  # retained for private-call compatibility
    from packages.core.plans.wakeup import hint_pending_plan_cycle

    # ``True`` means the durable intent exists. The CONTROL publication below
    # is only a latency hint; its helper logs broker failure and deliberately
    # leaves Plan/Task business state unchanged for the outbox sweep to recover.
    dispatched = await hint_pending_plan_cycle(
        db,
        plan.id,
        reason=reason,
    )
    if plan.task_id:
        from packages.core.services.realtime import broadcast_task_runtime_update

        await broadcast_task_runtime_update(
            plan.entity_id,
            task_id=plan.task_id,
            workspace_id=plan.workspace_id,
            plan_id=plan.id,
            event="plan_dispatch_resolved",
        )
    return dispatched


async def _record_retry_on_task(
    db: AsyncSession,
    plan: ExecutionPlan,
    *,
    user: User,
    mode: str,
    reset_step_ids: list[str],
    note: str | None = None,
) -> None:
    """Mirror execution-layer retry onto the business Task timeline."""
    from packages.core.services.task_execution_origin import (
        record_manual_plan_execution,
        record_manual_task_execution,
    )

    record_manual_plan_execution(plan, user_id=user.id, action=mode)
    if not plan.task_id:
        return
    task = (await db.execute(
        select(Task).where(Task.id == plan.task_id, Task.entity_id == plan.entity_id)
    )).scalar_one_or_none()
    if not task:
        return

    now = datetime.now(timezone.utc)
    details = dict(task.details or {})
    manual_retry_count = int(details.get("manual_retry_count") or 0) + 1
    details["manual_retry_count"] = manual_retry_count
    details["manual_retry"] = {
        "requested_by": user.id,
        "requested_at": now.isoformat(),
        "mode": mode,
        "plan_id": plan.id,
        "step_ids": reset_step_ids,
        **({"note": note} if note else {}),
    }

    from packages.core.services.task_state_machine import apply_task_status_transition
    await apply_task_status_transition(
        task, "in_progress", now=now, db=db, actor_kind="user", actor_id=user.id,
    )
    task.started_at = now
    task.completed_at = None
    task.actual_output = None
    task.details = details
    record_manual_task_execution(task, user_id=user.id, action=mode)

    await add_task_log(
        db,
        task.id,
        TaskLogType.MANUAL_RETRY,
        f"Manual {mode} retry requested" + (f": {note}" if note else ""),
        actor=TaskActor.USER,
        created_by=user.display_name or user.email,
        metadata={
            "mode": mode,
            "plan_id": plan.id,
            "step_ids": reset_step_ids,
            "reset_steps": len(reset_step_ids),
            "retry_count": manual_retry_count,
            "requested_by": user.id,
        },
    )
    from packages.core.services import event_emitter
    await event_emitter.emit_in_session(
        db,
        plan.entity_id,
        "task.retried",
        source="plans_api",
        payload={
            "task_id": task.id,
            "plan_id": plan.id,
            "step_ids": reset_step_ids,
            "mode": mode,
            "reset_steps": len(reset_step_ids),
            "retry_count": manual_retry_count,
            "requested_by": user.id,
        },
        workspace_id=plan.workspace_id,
        deliver_after_commit=True,
    )


# ── Routes ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[PlanResponse])
async def list_plans(
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_readable(db, user, workspace_id)
    from packages.core.services.workspace_access import readable_workspace_ids_for_user
    readable_ws = await readable_workspace_ids_for_user(
        db, entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    stmt = select(ExecutionPlan).where(ExecutionPlan.entity_id == user.entity_id)
    stmt = stmt.where(
        workspace_resource_not_soft_deleted(
            ExecutionPlan.workspace_id,
            entity_id=user.entity_id,
        )
    )
    if workspace_id:
        stmt = stmt.where(ExecutionPlan.workspace_id == workspace_id)
    if readable_ws is not None:
        from sqlalchemy import or_ as _or
        stmt = stmt.where(_or(
            ExecutionPlan.workspace_id.is_(None),
            ExecutionPlan.workspace_id.in_(readable_ws),
        ))
    if task_id:
        stmt = stmt.where(ExecutionPlan.task_id == task_id)
    if status:
        stmt = stmt.where(ExecutionPlan.status == status)
    stmt = stmt.order_by(ExecutionPlan.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    task_ids = [p.task_id for p in rows if p.task_id]
    task_meta: dict[str, tuple[str, str]] = {}
    if task_ids:
        task_meta = {
            row[0]: (row[1], row[2])
            for row in (await db.execute(
                select(Task.id, Task.status, Task.title).where(
                    Task.entity_id == user.entity_id,
                    Task.id.in_(task_ids),
                )
            )).all()
        }
    return [_to_plan(p, *(task_meta.get(p.task_id or "") or (None, None))) for p in rows]


@router.get("/{plan_id}", response_model=PlanResponse)
async def get_one(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await get_plan(db, plan_id, entity_id=user.entity_id)
    if not p:
        raise HTTPException(404, "plan not found")
    await require_workspace_readable(db, user, p.workspace_id)
    task_status = None
    task_title = None
    if p.task_id:
        task_row = (await db.execute(
            select(Task.status, Task.title).where(Task.id == p.task_id, Task.entity_id == user.entity_id)
        )).one_or_none()
        if task_row:
            task_status, task_title = task_row
    return _to_plan(p, task_status, task_title)


@router.get("/{plan_id}/steps", response_model=list[StepResponse])
async def steps(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await get_plan(db, plan_id, entity_id=user.entity_id)
    if not p:
        raise HTTPException(404, "plan not found")
    await require_workspace_readable(db, user, p.workspace_id)
    rows = await list_plan_steps(db, plan_id)
    subscriptions_by_id, subscriptions_by_service, agents_by_id = await _step_display_lookups(
        db,
        rows,
        entity_id=user.entity_id,
        workspace_id=p.workspace_id,
    )
    return [
        _to_step(
            s,
            subscriptions_by_id=subscriptions_by_id,
            subscriptions_by_service=subscriptions_by_service,
            agents_by_id=agents_by_id,
        )
        for s in rows
    ]


def _to_task_plan_admission(submission) -> TaskPlanAdmissionResponse:
    return TaskPlanAdmissionResponse(
        submission_id=submission.id,
        task_id=submission.task_id,
        status=submission.state,
        plan_id=submission.plan_id,
    )


@router.post(
    "", response_model=PlanResponse | TaskPlanAdmissionResponse, status_code=201,
    responses={202: {"model": TaskPlanAdmissionResponse, "description": "Workspace Task Plan request accepted"}},
)
async def create_manual(
    req: PlanCreateRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a Workspace Task DAG to V3; keep taskless/legacy Plan creation."""
    rejected_receipt = await _validate_manual_plan_scope(db, user=user, request=req)
    if req.task_id and req.workspace_id:
        from packages.core.models.task_plan_submission import TaskPlanSubmissionKind
        from packages.core.services.task_plan_submission import (
            TaskPlanSubmissionConflict,
            manual_task_plan_fingerprint,
            submit_task_plan_request,
        )

        task = (await db.execute(
            select(Task).where(Task.id == req.task_id, Task.entity_id == user.entity_id)
            .execution_options(populate_existing=True)
        )).scalar_one()
        if rejected_receipt is not None:
            if rejected_receipt.request_fingerprint == manual_task_plan_fingerprint(
                execution_mode=req.execution_mode,
                approval_required=req.approval_required,
                plan=req.plan,
            ):
                raise HTTPException(
                    409, "This manual Plan was rejected: "
                    + str(rejected_receipt.rejection_reason or "correct the DAG before resubmitting"),
                )
            # Retry and replacement submission must commit together. A
            # separate /tasks/{id}/retry could wake the automatic Planner
            # before the corrected authored DAG reaches this endpoint.
            existing_plan = (await db.execute(
                select(ExecutionPlan.id).where(
                    ExecutionPlan.task_id == task.id,
                    ExecutionPlan.entity_id == task.entity_id,
                ).limit(1)
            )).scalar_one_or_none()
            if existing_plan is not None:
                raise HTTPException(409, "A Task with a saved Plan cannot replace a rejected manual DAG")
            from packages.core.models.task_coordinator import TaskCoordinatorRun

            run = (await db.execute(
                select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            expiry = run.claim_expires_at if run is not None else None
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if run is None or run.current_plan_id or (
                run.claim_token and expiry is not None and expiry > datetime.now(timezone.utc)
            ):
                raise HTTPException(409, "Task coordinator must be safely stopped before replacing its DAG")
            from packages.core.services.task_retry_service import prepare_task_retry

            try:
                retry_result = await prepare_task_retry(
                    db, task=task, user_id=user.id,
                    user_label=user.display_name or user.email,
                    note="Replace rejected manual Plan DAG",
                )
            except TaskRetryError as exc:
                raise HTTPException(exc.status_code, exc.detail) from exc
            task = retry_result.task
            from packages.core.constants.pending_actions import PendingActionKind
            from packages.core.services.task_chat_hitl import resolve_task_hitl

            await resolve_task_hitl(
                db, task=task, kind=PendingActionKind.TASK_RECOVERY,
                choice="retry", user_id=user.id,
                note="Replace rejected manual Plan DAG",
            )
            from packages.core.services.task_coordinator_wakeup import ensure_task_coordinator_run

            if retry_result.dispatch.coordinator is None:
                raise HTTPException(409, "Task has no active coordinator service scope")
            try:
                await ensure_task_coordinator_run(
                    db, task=task, selection=retry_result.dispatch.coordinator,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        try:
            submission = await submit_task_plan_request(
                db,
                task=task,
                requested_by_user_id=user.id,
                kind=TaskPlanSubmissionKind.MANUAL,
                execution_mode=req.execution_mode,
                approval_required=req.approval_required,
                plan=req.plan,
                source_submission_id=(rejected_receipt.id if rejected_receipt is not None else None),
            )
        except TaskPlanSubmissionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except PlanContractError as exc:
            raise HTTPException(400, f"plan contract failed: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await db.commit()
        response.status_code = 202
        return _to_task_plan_admission(submission)
    try:
        plan_row = await create_plan_from_dag(
            db,
            entity_id=user.entity_id,
            workspace_id=req.workspace_id,
            task_id=req.task_id,
            agent_subscription_id=req.agent_subscription_id,
            plan=req.plan,
            execution_mode=req.execution_mode,
            approval_required=req.approval_required,
            enforce_contract=True,
        )
    except PlanContractError as exc:
        raise HTTPException(400, f"plan contract failed: {exc}") from exc
    except TaskEffectReplayBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ActiveTaskPlanError as exc:
        raise HTTPException(409, str(exc)) from exc
    from packages.core.services.task_execution_origin import (
        record_manual_plan_execution,
        record_manual_task_execution,
    )

    record_manual_plan_execution(plan_row, user_id=user.id, action="plan_create")
    if plan_row.task_id:
        task = await db.get(Task, plan_row.task_id)
        record_manual_task_execution(task, user_id=user.id, action="plan_create")
    await db.commit()
    await _maybe_dispatch(
        db,
        plan_row,
        user_id=user.id,
        reason="manual_plan_create_dispatch_failed",
    )
    await db.refresh(plan_row)
    return _to_plan(plan_row)


@router.post(
    "/from-task/{task_id}", response_model=PlanResponse | TaskPlanAdmissionResponse,
    status_code=201,
    responses={202: {"model": TaskPlanAdmissionResponse, "description": "Workspace Task Planner request accepted"}},
)
async def create_from_task(
    task_id: str,
    req: PlanFromTaskRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admit a V3 Workspace Task Planner request; preserve entity legacy Plans."""
    scope = (await db.execute(
        select(Task.id, Task.workspace_id).where(Task.id == task_id, Task.entity_id == user.entity_id)
    )).one_or_none()
    if scope is None:
        raise HTTPException(404, "task not found")
    if scope.workspace_id:
        from packages.core.models.task_plan_submission import TaskPlanSubmissionKind
        from packages.core.services.task_plan_submission import (
            TaskPlanSubmissionConflict,
            submit_task_plan_request,
        )

        task = await _lock_task_for_plan(db, user=user, task_id=task_id)
        try:
            submission = await submit_task_plan_request(
                db,
                task=task,
                requested_by_user_id=user.id,
                kind=TaskPlanSubmissionKind.PLANNER,
                execution_mode=req.execution_mode,
            )
        except TaskPlanSubmissionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await db.commit()
        response.status_code = 202
        return _to_task_plan_admission(submission)
    await _authorize_task_for_planner(db, user=user, task_id=task_id)

    async def _revalidate_write_access(plan_row: ExecutionPlan) -> None:
        if plan_row.entity_id != user.entity_id:
            raise HTTPException(403, "task not in your entity")
        await require_workspace_writable(db, user, plan_row.workspace_id)

    async def _resolve_billing_scope():
        from packages.core.ai.runtime import (
            runtime_ensure_planner_task_billing_context,
        )

        return await runtime_ensure_planner_task_billing_context(db, task_id)

    try:
        plan_row = await plan_task_and_commit(
            db,
            task_id,
            execution_mode=req.execution_mode,
            manual_user_id=user.id,
            before_provider=_resolve_billing_scope,
            before_commit=_revalidate_write_access,
        )
    except CapabilityError as exc:
        raise HTTPException(400, f"planner capability validation failed: {exc}") from exc
    except TaskPlanOwnedByCoordinatorError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlannerError as exc:
        raise HTTPException(400, f"planner failed: {exc}") from exc
    except PlanContractError as exc:
        raise HTTPException(400, f"plan contract failed: {exc}") from exc
    except TaskEffectReplayBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ActiveTaskPlanError as exc:
        # The Plan commit is the durable boundary; dispatch happens after it.
        # If the request/worker died in that gap, a client retry must reuse and
        # dispatch the committed Plan instead of returning a permanent 409.
        await db.rollback()
        plan_row = await get_plan(db, exc.plan_id, entity_id=user.entity_id)
        if (
            plan_row is None
            or plan_row.task_id != task_id
            or plan_row.execution_mode != req.execution_mode
        ):
            raise HTTPException(409, str(exc)) from exc
        await _revalidate_write_access(plan_row)
        from packages.core.services.task_execution_origin import (
            record_manual_plan_execution,
            record_manual_task_execution,
        )

        plan_row = await _lock_plan_mutation_origin(db, plan_id=plan_row.id, user=user)
        if plan_row is None:
            raise HTTPException(404, "plan not found")
        record_manual_plan_execution(plan_row, user_id=user.id, action="plan_dispatch")
        if plan_row.task_id:
            task = await db.get(Task, plan_row.task_id)
            record_manual_task_execution(task, user_id=user.id, action="plan_dispatch")
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        await stage_plan_cycle_wake(
            db,
            plan_row.id,
            reason="manual_existing_plan_dispatch",
        )
        await db.commit()
    except TaskPlanClaimHeldError as exc:
        raise HTTPException(409, str(exc)) from exc
    except TaskPlanExecutionClaimLost as exc:
        raise HTTPException(409, "task planning ownership changed; retry") from exc
    await _maybe_dispatch(
        db,
        plan_row,
        user_id=user.id,
        reason="planned_task_dispatch_failed",
    )
    await db.refresh(plan_row)
    return _to_plan(plan_row)


@router.post("/{plan_id}/approve", response_model=PlanResponse)
async def approve(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await _lock_plan_mutation_origin(
        db,
        plan_id=plan_id,
        user=user,
    )
    if not p:
        raise HTTPException(404, "plan not found")
    await require_workspace_authority(
        db,
        user,
        p.workspace_id,
        "approve_tasks",
    )
    if p.status != ExecutionPlanStatus.PENDING_APPROVAL:
        raise HTTPException(409, f"plan status is {p.status}, not pending_approval")
    p.status = ExecutionPlanStatus.DRAFT.value  # executor flips draft → running on first cycle
    p.approval_required = False
    from packages.core.services.task_execution_origin import (
        record_manual_plan_execution,
        record_manual_task_execution,
    )

    record_manual_plan_execution(p, user_id=user.id, action="plan_approval")
    task = None
    if p.task_id:
        task = (await db.execute(
            select(Task).where(Task.id == p.task_id, Task.entity_id == p.entity_id)
        )).scalar_one_or_none()
        if task is not None:
            record_manual_task_execution(task, user_id=user.id, action="plan_approval")
        if task and task.status == TaskStatus.WAITING_ON_CUSTOMER:
            from packages.core.services.task_state_machine import apply_task_status_transition

            await apply_task_status_transition(
                task, "in_progress", db=db, actor_kind="user", actor_id=user.id,
            )
            await add_task_log(
                db,
                task.id,
                TaskLogType.AI_HITL_RESUMED,
                "Plan approval received. Execution will resume.",
                actor=TaskActor.USER,
                created_by=user.display_name or user.email,
                metadata={"plan_id": p.id, "approval_required": False},
            )
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )

    state = p.dispatcher_state if isinstance(p.dispatcher_state, dict) else {}
    v3_plan = state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION
    if v3_plan:
        from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
        from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake
        from packages.core.services.task_execution import task_execution_intent_generation

        run = (await db.execute(
            select(TaskCoordinatorRun).where(
                TaskCoordinatorRun.id == state.get(TASK_COORDINATOR_RUN_ID_KEY),
                TaskCoordinatorRun.task_id == p.task_id,
                TaskCoordinatorRun.current_plan_id == p.id,
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if run is None or run.phase in TaskCoordinatorPhase.terminal():
            raise HTTPException(409, "Task coordinator is no longer waiting for this Plan")
        if task is None or task.status != TaskStatus.IN_PROGRESS.value:
            raise HTTPException(409, "Task did not resume for this Plan approval")
        run.lifecycle_generation = task_execution_intent_generation(task)
        run.phase = TaskCoordinatorPhase.READY.value
        run.state_version += 1
        if await stage_task_coordinator_wake(
            db, task_id=p.task_id, reason="plan_approved",
        ) is None:
            raise HTTPException(409, "Task coordinator cannot resume this Plan")
    else:
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        await stage_plan_cycle_wake(
            db,
            p.id,
            reason="plan_approved",
        )
    await db.commit()
    await db.refresh(p)
    if not v3_plan:
        await _maybe_dispatch(
            db,
            p,
            user_id=user.id,
            reason="plan_approval_dispatch_failed",
            force=True,
        )
    return _to_plan(p)


@router.post("/{plan_id}/cancel", response_model=PlanResponse)
async def cancel(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await _lock_plan_mutation_origin(
        db,
        plan_id=plan_id,
        user=user,
    )
    if not p:
        raise HTTPException(404, "plan not found")
    _reject_task_coordinator_plan_cancel(p)
    cancelled = await cancel_plan(db, plan_id, reason="cancelled via API")
    await db.commit()
    await db.refresh(cancelled or p)
    return _to_plan(cancelled or p)


@router.post("/{plan_id}/retry-failed-steps", response_model=RetryPlanResponse)
async def retry_failed_steps(
    plan_id: str,
    req: RetryRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset failed/cancelled steps and re-dispatch the plan runner.

    A waiting-human Step is resumed only by its specific HITL decision surface;
    a generic retry is not approval or review consent.
    """
    p = await _lock_plan_mutation_origin(
        db,
        plan_id=plan_id,
        user=user,
    )
    if not p:
        raise HTTPException(404, "plan not found")
    _reject_replanned_plan_retry(p)
    _reject_task_coordinator_step_retry(p)
    await _require_authoritative_task_plan_retry(db, p)

    note = (req.note if req else None) or None
    steps = await list_plan_steps(db, plan_id)
    has_waiting_human = has_waiting_human_decision(steps)
    if has_waiting_human:
        raise HTTPException(409, WAITING_HUMAN_RETRY_DETAIL)
    if await has_pending_task_recovery(db, p):
        raise HTTPException(409, TASK_RECOVERY_RETRY_DETAIL)
    is_dispatch_recovery = is_plan_dispatch_recovery(p)
    retry_block_detail = plan_retry_block_detail(
        p,
        allow_standalone_dispatch_recovery=True,
    )
    if retry_block_detail:
        raise HTTPException(409, retry_block_detail)
    if is_dispatch_recovery:
        # Standalone Plans have no Task recovery surface. The prior decision
        # already committed its exact Step mutations, so only replay enqueue.
        _revive_plan_for_retry(p)
        await _record_retry_on_task(
            db,
            p,
            user=user,
            mode="plan_dispatch",
            reset_step_ids=[],
            note=note,
        )
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        await stage_plan_cycle_wake(
            db,
            p.id,
            reason="plan_dispatch_retry",
        )
        await db.commit()
        await db.refresh(p)
        dispatched = await _dispatch_plan(
            db,
            p,
            user_id=user.id,
            reason="plan_dispatch_retry_failed",
        )
        return RetryPlanResponse(
            plan=_to_plan(p),
            reset_steps=0,
            dispatched=dispatched,
        )
    retryable_steps = [
        step
        for step in steps
        if step.step_status in RETRYABLE_EXECUTION_STEP_STATUSES
    ]
    effect_block = await retry_effect_block_detail(db, retryable_steps)
    if effect_block:
        raise HTTPException(409, effect_block)
    if await _maybe_replan_stale_plan(
        db,
        p,
        user=user,
        note=note,
        has_waiting_human=has_waiting_human,
    ):
        await db.commit()
        await db.refresh(p)
        dispatched = await _dispatch_task_replan(db, p) if p.task_id else False
        return RetryPlanResponse(
            plan=_to_plan(p),
            reset_steps=0,
            dispatched=dispatched,
        )
    reset_steps = 0
    reset_step_ids: list[str] = []
    retried_parent_keys = {
        step.step_key
        for step in retryable_steps
        if not _is_coordinator_child_step(step)
    }
    for step in retryable_steps:
        # A coordinator child is admitted only with its parent. Preserve its
        # durable result/checkpoint/evidence; the stable parent handler owns
        # the actual claim and nested retry.
        if _is_coordinator_child_step(step):
            parent_key = str(
                (step.params or {}).get(COORDINATOR_PARENT_STEP_PARAM_KEY) or ""
            )
            if parent_key not in retried_parent_keys:
                continue
            _reset_step_for_retry(
                step,
                user=user,
                note=note,
                preserve_result=True,
            )
        else:
            _reset_step_for_retry(step, user=user, note=note)
        reset_steps += 1
        reset_step_ids.append(step.id)

    if reset_steps == 0:
        raise HTTPException(409, "plan has no retryable failed, cancelled, skipped, or paused steps")

    _revive_plan_for_retry(p)
    await _record_retry_on_task(
        db,
        p,
        user=user,
        mode="plan_failed_steps",
        reset_step_ids=reset_step_ids,
        note=note,
    )
    from packages.core.plans.wakeup import stage_plan_cycle_wake

    await stage_plan_cycle_wake(
        db,
        p.id,
        reason="plan_failed_steps_retried",
    )
    await db.commit()
    await db.refresh(p)
    dispatched = await _dispatch_plan(
        db,
        p,
        user_id=user.id,
        reason="plan_failed_steps_dispatch_failed",
    )
    return RetryPlanResponse(plan=_to_plan(p), reset_steps=reset_steps, dispatched=dispatched)


@router.post("/steps/{step_id}/retry", response_model=RetryStepResponse)
async def retry_step(
    step_id: str,
    req: RetryRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset one retryable step and re-dispatch its plan runner."""
    target_plan_id = (await db.execute(
        select(ExecutionStep.plan_id)
        .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
        .where(ExecutionStep.id == step_id, ExecutionPlan.entity_id == user.entity_id)
    )).scalar_one_or_none()
    if not target_plan_id:
        raise HTTPException(404, "step not found")
    p = await _lock_plan_mutation_origin(
        db,
        plan_id=target_plan_id,
        user=user,
    )
    if not p:
        raise HTTPException(404, "plan not found")
    _reject_replanned_plan_retry(p)
    _reject_task_coordinator_step_retry(p)
    await _require_authoritative_task_plan_retry(db, p)
    if await has_pending_task_recovery(db, p):
        raise HTTPException(409, TASK_RECOVERY_RETRY_DETAIL)
    retry_block_detail = plan_retry_block_detail(
        p,
        allow_standalone_dispatch_recovery=False,
    )
    if retry_block_detail:
        raise HTTPException(409, retry_block_detail)
    steps = await list_plan_steps(db, p.id)
    step = next((candidate for candidate in steps if candidate.id == step_id), None)
    if step is None:
        raise HTTPException(404, "step not found")
    if _is_coordinator_child_step(step):
        raise HTTPException(
            409,
            "Coordinator child Steps can only be resumed by their parent coordinator",
        )
    if step.step_status not in RETRYABLE_EXECUTION_STEP_STATUSES:
        raise HTTPException(409, f"step status is {step.step_status} and cannot be retried")

    note = (req.note if req else None) or None
    has_waiting_human = has_waiting_human_decision(steps)
    if has_waiting_human:
        raise HTTPException(409, WAITING_HUMAN_RETRY_DETAIL)
    logical_steps = [
        candidate for candidate in steps if not _is_coordinator_child_step(candidate)
    ]
    downstream_candidates: list[ExecutionStep] = []
    reset_never_ran_downstream_steps(
        logical_steps,
        retried_step_keys={step.step_key},
        reset_step=downstream_candidates.append,
    )
    owned_children = [
        candidate
        for candidate in steps
        if _is_coordinator_child_step(candidate)
        and str(
            (candidate.params or {}).get(COORDINATOR_PARENT_STEP_PARAM_KEY) or ""
        ) == step.step_key
        and candidate.step_status in RETRYABLE_EXECUTION_STEP_STATUSES
    ]
    effect_block = await retry_effect_block_detail(
        db,
        [step, *owned_children, *downstream_candidates],
    )
    if effect_block:
        raise HTTPException(409, effect_block)
    if await _maybe_replan_stale_plan(
        db,
        p,
        user=user,
        note=note,
        has_waiting_human=has_waiting_human,
    ):
        await db.commit()
        await db.refresh(p)
        await db.refresh(step)
        dispatched = await _dispatch_task_replan(db, p) if p.task_id else False
        subscriptions_by_id, subscriptions_by_service, agents_by_id = await _step_display_lookups(
            db,
            [step],
            entity_id=user.entity_id,
            workspace_id=p.workspace_id,
        )
        return RetryStepResponse(
            plan=_to_plan(p),
            step=_to_step(
                step,
                subscriptions_by_id=subscriptions_by_id,
                subscriptions_by_service=subscriptions_by_service,
                agents_by_id=agents_by_id,
            ),
            dispatched=dispatched,
        )
    _reset_step_for_retry(step, user=user, note=note)
    for child in owned_children:
        _reset_step_for_retry(
            child,
            user=user,
            note=note,
            preserve_result=True,
        )
    downstream_steps = _reset_skipped_downstream_steps_for_retry(
        logical_steps,
        retried_step_keys={step.step_key},
        user=user,
        note=note,
    )
    reset_step_ids = [
        step.id,
        *(child.id for child in owned_children),
        *(downstream.id for downstream in downstream_steps),
    ]
    _revive_plan_for_retry(p)
    await _record_retry_on_task(
        db,
        p,
        user=user,
        mode="plan_step",
        reset_step_ids=reset_step_ids,
        note=note,
    )
    from packages.core.plans.wakeup import stage_plan_cycle_wake

    await stage_plan_cycle_wake(
        db,
        p.id,
        reason="plan_step_retried",
    )
    await db.commit()
    await db.refresh(p)
    await db.refresh(step)
    dispatched = await _dispatch_plan(
        db,
        p,
        user_id=user.id,
        reason="plan_step_dispatch_failed",
    )
    subscriptions_by_id, subscriptions_by_service, agents_by_id = await _step_display_lookups(
        db,
        [step],
        entity_id=user.entity_id,
        workspace_id=p.workspace_id,
    )
    return RetryStepResponse(
        plan=_to_plan(p),
        step=_to_step(
            step,
            subscriptions_by_id=subscriptions_by_id,
            subscriptions_by_service=subscriptions_by_service,
            agents_by_id=agents_by_id,
        ),
        dispatched=dispatched,
    )


# ── Helpers ────────────────────────────────────────────────────────────

async def _maybe_dispatch(
    db: AsyncSession,
    plan_row: ExecutionPlan,
    *,
    user_id: str,
    reason: str,
    force: bool = False,
) -> bool:
    """Dispatch a ready Plan and persist an actionable failure state."""
    if plan_row.status == ExecutionPlanStatus.PENDING_APPROVAL and not force:
        return False
    return await _dispatch_plan(
        db,
        plan_row,
        user_id=user_id,
        reason=reason,
        is_retry=False,
    )
