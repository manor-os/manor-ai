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
                                             reset retryable failed/HITL steps
  POST   /api/v1/plans/steps/{step_id}/retry
                                             reset one retryable step
  GET    /api/v1/plans/{id}/steps           list steps under a plan
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
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
from packages.core.plans.planner import PlannerError, plan_task
from packages.core.plans.schema import Plan
from packages.core.services.task_service import add_task_log


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
    result: Optional[dict]
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


_RETRYABLE_STEP_STATUSES = {"failed", "skipped", "waiting_human", "paused", "cancelled"}
_RESETTABLE_PLAN_STATUSES = {"failed", "needs_attention", "paused", "cancelled", "completed"}


def _reset_step_for_retry(
    step: ExecutionStep,
    *,
    user: User,
    note: str | None = None,
    preserve_result: bool = False,
) -> None:
    step.step_status = "pending"
    step.current_lease_id = None
    step.error = None
    step.finished_at = None
    step.started_at = None
    step.attempt_count = 0
    step.human_input_prompt = None
    if not preserve_result:
        step.result = None
    step.human_input_response = (
        {
            "response": note,
            "user": user.display_name or user.email,
            "user_id": user.id,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        if note else None
    )


def _reset_skipped_downstream_steps_for_retry(
    steps: list[ExecutionStep],
    *,
    retried_step_keys: set[str],
    user: User,
    note: str | None = None,
) -> list[ExecutionStep]:
    """Reset skipped descendants unblocked by the retried step.

    A single-step retry often targets the first failed step in a dependency
    chain. Downstream steps may already be terminal ``skipped`` from the earlier
    failed run, so the executor will never revisit them unless we revive the
    skipped chain here.
    """
    done_or_retried = {
        step.step_key
        for step in steps
        if step.step_status == "done" or step.step_key in retried_step_keys
    }
    revived_keys = set(retried_step_keys)
    reset: list[ExecutionStep] = []

    changed = True
    while changed:
        changed = False
        for step in steps:
            if step.step_key in done_or_retried or step.step_status != "skipped":
                continue
            deps = [str(dep) for dep in (step.depends_on or []) if dep]
            if not deps:
                continue
            if not any(dep in revived_keys for dep in deps):
                continue
            if not all(dep in done_or_retried for dep in deps):
                continue
            _reset_step_for_retry(step, user=user, note=note)
            reset.append(step)
            done_or_retried.add(step.step_key)
            revived_keys.add(step.step_key)
            changed = True

    return reset


def _revive_plan_for_retry(plan: ExecutionPlan) -> None:
    if plan.status in _RESETTABLE_PLAN_STATUSES:
        plan.status = "draft"
    plan.completed_at = None
    plan.last_error = None


async def _dispatch_plan(plan: ExecutionPlan) -> bool:
    try:
        from packages.core.tasks.ai_tasks import run_plan
        run_plan.delay(plan.id)
        return True
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "Plan %s retry dispatch failed", plan.id, exc_info=True,
        )
        return False


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
    apply_task_status_transition(task, "in_progress", now=now)
    task.started_at = now
    task.completed_at = None
    task.actual_output = None
    task.details = details

    await add_task_log(
        db,
        task.id,
        "manual_retry",
        f"Manual {mode} retry requested" + (f": {note}" if note else ""),
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
    event_emitter.emit(
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
    stmt = select(ExecutionPlan).where(ExecutionPlan.entity_id == user.entity_id)
    if workspace_id:
        stmt = stmt.where(ExecutionPlan.workspace_id == workspace_id)
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


@router.post("", response_model=PlanResponse, status_code=201)
async def create_manual(
    req: PlanCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dev / power-user path: hand a Plan in directly. Skips Planner."""
    plan_row = await create_plan_from_dag(
        db,
        entity_id=user.entity_id,
        workspace_id=req.workspace_id,
        task_id=req.task_id,
        agent_subscription_id=req.agent_subscription_id,
        plan=req.plan,
        execution_mode=req.execution_mode,
        approval_required=req.approval_required,
    )
    await db.commit()
    await _maybe_dispatch(plan_row)
    await db.refresh(plan_row)
    return _to_plan(plan_row)


@router.post("/from-task/{task_id}", response_model=PlanResponse, status_code=201)
async def create_from_task(
    task_id: str,
    req: PlanFromTaskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invoke the Planner for a task, persist, optionally dispatch."""
    try:
        plan_row = await plan_task(db, task_id, execution_mode=req.execution_mode)
    except PlannerError as exc:
        raise HTTPException(400, f"planner failed: {exc}")
    if plan_row.entity_id != user.entity_id:
        raise HTTPException(403, "task not in your entity")
    await db.commit()
    await _maybe_dispatch(plan_row)
    await db.refresh(plan_row)
    return _to_plan(plan_row)


@router.post("/{plan_id}/approve", response_model=PlanResponse)
async def approve(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await get_plan(db, plan_id, entity_id=user.entity_id)
    if not p:
        raise HTTPException(404, "plan not found")
    if p.status != "pending_approval":
        raise HTTPException(409, f"plan status is {p.status}, not pending_approval")
    p.status = "draft"  # executor flips draft → running on first cycle
    p.approval_required = False
    if p.task_id:
        task = (await db.execute(
            select(Task).where(Task.id == p.task_id, Task.entity_id == p.entity_id)
        )).scalar_one_or_none()
        if task and task.status == "waiting_on_customer":
            from packages.core.services.task_state_machine import apply_task_status_transition

            apply_task_status_transition(task, "in_progress")
            await add_task_log(
                db,
                task.id,
                "ai_hitl_resumed",
                "Plan approval received. Execution will resume.",
                metadata={"plan_id": p.id, "approval_required": False},
            )
    await db.commit()
    await db.refresh(p)
    await _maybe_dispatch(p, force=True)
    return _to_plan(p)


@router.post("/{plan_id}/cancel", response_model=PlanResponse)
async def cancel(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await get_plan(db, plan_id, entity_id=user.entity_id)
    if not p:
        raise HTTPException(404, "plan not found")
    cancelled = await cancel_plan(db, plan_id, reason="cancelled via API")
    await db.commit()
    return _to_plan(cancelled or p)


@router.post("/{plan_id}/retry-failed-steps", response_model=RetryPlanResponse)
async def retry_failed_steps(
    plan_id: str,
    req: RetryRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset failed/HITL/cancelled steps and re-dispatch the plan runner."""
    p = await get_plan(db, plan_id, entity_id=user.entity_id)
    if not p:
        raise HTTPException(404, "plan not found")

    note = (req.note if req else None) or None
    steps = await list_plan_steps(db, plan_id)
    reset_steps = 0
    reset_step_ids: list[str] = []
    for step in steps:
        if step.step_status in _RETRYABLE_STEP_STATUSES:
            _reset_step_for_retry(step, user=user, note=note)
            reset_steps += 1
            reset_step_ids.append(step.id)

    if reset_steps == 0:
        raise HTTPException(409, "plan has no retryable failed, cancelled, skipped, or HITL steps")

    _revive_plan_for_retry(p)
    await _record_retry_on_task(
        db,
        p,
        user=user,
        mode="plan_failed_steps",
        reset_step_ids=reset_step_ids,
        note=note,
    )
    await db.commit()
    await db.refresh(p)
    dispatched = await _dispatch_plan(p)
    return RetryPlanResponse(plan=_to_plan(p), reset_steps=reset_steps, dispatched=dispatched)


@router.post("/steps/{step_id}/retry", response_model=RetryStepResponse)
async def retry_step(
    step_id: str,
    req: RetryRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset one retryable step and re-dispatch its plan runner."""
    row = (await db.execute(
        select(ExecutionStep, ExecutionPlan)
        .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
        .where(ExecutionStep.id == step_id, ExecutionPlan.entity_id == user.entity_id)
    )).first()
    if not row:
        raise HTTPException(404, "step not found")
    step, p = row
    if step.step_status not in _RETRYABLE_STEP_STATUSES:
        raise HTTPException(409, f"step status is {step.step_status} and cannot be retried")

    note = (req.note if req else None) or None
    steps = await list_plan_steps(db, p.id)
    _reset_step_for_retry(step, user=user, note=note)
    downstream_steps = _reset_skipped_downstream_steps_for_retry(
        steps,
        retried_step_keys={step.step_key},
        user=user,
        note=note,
    )
    reset_step_ids = [step.id, *(downstream.id for downstream in downstream_steps)]
    _revive_plan_for_retry(p)
    await _record_retry_on_task(
        db,
        p,
        user=user,
        mode="plan_step",
        reset_step_ids=reset_step_ids,
        note=note,
    )
    await db.commit()
    await db.refresh(p)
    await db.refresh(step)
    dispatched = await _dispatch_plan(p)
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

async def _maybe_dispatch(plan_row: ExecutionPlan, *, force: bool = False) -> None:
    """Fire ``run_plan`` Celery task unless the plan still needs approval.

    Best-effort: failures are logged but don't block the API response —
    the plan row exists; the operator can re-trigger from the UI."""
    if plan_row.status == "pending_approval" and not force:
        return
    try:
        from packages.core.tasks.ai_tasks import run_plan
        run_plan.delay(plan_row.id)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "Plan %s created but Celery dispatch failed", plan_row.id, exc_info=True,
        )
