"""ExecutionPlan + ExecutionStep CRUD + plan materialisation.

The ``materialize_plan_steps`` helper is the bridge from the Pydantic
``Plan`` shape (what the Planner produces) to one ``execution_steps``
row per node (what the executor / dispatcher reads). Doing this once
on plan launch — rather than re-parsing plan_dag on every executor
cycle — keeps the hot path of ``run_cycle`` cheap and lets the
dispatcher query "all pending steps for this plan" with a single
indexed lookup.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.plans.schema import Plan, PlanStep

logger = logging.getLogger(__name__)


# ── Plan CRUD ─────────────────────────────────────────────────────────

async def create_plan_from_dag(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: Optional[str],
    task_id: Optional[str],
    agent_subscription_id: Optional[str],
    plan: Plan,
    planner_version: Optional[str] = None,
    parent_plan_id: Optional[str] = None,
    execution_mode: str = "live",
    approval_required: bool = False,
) -> ExecutionPlan:
    """Persist a validated Plan as ExecutionPlan + ExecutionStep rows.

    Sets initial status='draft' (or 'pending_approval' when the
    plan-level approval_required flag is explicitly set). Step-level
    approval is enforced by the Dispatcher when that step becomes
    runnable, so safe predecessor work can still proceed.
    """
    # Plan-level approval is an explicit operator/API choice. Do not promote
    # every approval-gated/high-risk step into a full-plan gate; the
    # Dispatcher pauses those steps at execution time.
    plan_needs_approval = bool(approval_required)
    initial_status = "pending_approval" if plan_needs_approval else "draft"

    row = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_subscription_id=agent_subscription_id,
        plan_dag=plan.model_dump(mode="json"),
        planner_version=planner_version,
        parent_plan_id=parent_plan_id,
        status=initial_status,
        approval_required=plan_needs_approval,
        execution_mode=execution_mode,
    )
    db.add(row)
    await db.flush()

    # Materialise immediately so step rows exist as soon as the plan
    # does — keeps the executor path uniform regardless of whether the
    # plan needs approval (approval just gates the status transition,
    # not the existence of step rows).
    await materialize_plan_steps(db, row.id, plan)
    if plan_needs_approval and task_id:
        await _surface_pending_plan_approval(db, row)
    return row


async def _surface_pending_plan_approval(db: AsyncSession, plan_row: ExecutionPlan) -> None:
    """Mirror a plan-level approval gate onto the task timeline."""
    if not plan_row.task_id:
        return
    from packages.core.models.task import Task
    from packages.core.services.task_service import add_task_log
    from packages.core.services.task_state_machine import (
        TaskStatusTransitionError,
        apply_task_status_transition,
    )

    task = (await db.execute(
        select(Task).where(Task.id == plan_row.task_id, Task.entity_id == plan_row.entity_id)
    )).scalar_one_or_none()
    if not task:
        return

    if task.status != "waiting_on_customer":
        try:
            apply_task_status_transition(task, "waiting_on_customer")
        except TaskStatusTransitionError:
            # Approval visibility should not make plan creation fail for
            # uncommon task states; the log still gives operators a path.
            pass

    approval_steps = list((await db.execute(
        select(ExecutionStep).where(
            ExecutionStep.plan_id == plan_row.id,
            ExecutionStep.requires_approval == True,  # noqa: E712
        ).order_by(ExecutionStep.created_at)
    )).scalars().all())
    step_labels = [
        step.step_key.replace("_", " ")
        for step in approval_steps
    ] or ["approval-gated step"]
    await add_task_log(
        db,
        plan_row.task_id,
        "ai_hitl_requested",
        (
            "This execution plan is waiting for approval before it can start.\n\n"
            f"Approval-gated step(s): {', '.join(step_labels)}.\n\n"
            "Approve the plan to dispatch execution, or revise the task/runtime rules."
        ),
        created_by="system",
        metadata={
            "plan_id": plan_row.id,
            "verdict": "pending_approval",
            "approval_required": True,
            "step_ids": [step.id for step in approval_steps],
            "capability_ids": sorted({step.capability_id for step in approval_steps if step.capability_id}),
        },
    )


async def materialize_plan_steps(
    db: AsyncSession, plan_id: str, plan: Plan,
) -> list[ExecutionStep]:
    """Insert one ExecutionStep row per node in the plan, in topo order.

    Idempotent on re-run only if no steps yet exist for the plan;
    callers should treat this as a one-shot at plan creation.
    """
    plan_row = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    )).scalar_one()
    workspace_retry_config: dict = {}
    if plan_row.workspace_id:
        from packages.core.models.workspace import Workspace
        from packages.core.services.retry_policy import workspace_retry_policy_config

        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == plan_row.workspace_id)
        )).scalar_one_or_none()
        workspace_retry_config = workspace_retry_policy_config(getattr(workspace, "settings", None))

    from packages.core.services.retry_policy import (
        merge_retry_policy_configs,
        plan_retry_policy_config,
        step_retry_policy_config,
    )
    plan_retry_config = plan_retry_policy_config(plan_row)

    ordered = plan.topo_order()
    shape_by_key = _resolve_output_shapes(ordered)

    rows: list[ExecutionStep] = []
    for ps in ordered:
        step_preview = type("_StepPreview", (), {"params": ps.params})()
        policy = merge_retry_policy_configs(
            ("workspace", workspace_retry_config),
            ("plan", plan_retry_config),
            ("step", step_retry_policy_config(step_preview)),
        )
        rows.append(_step_from_pydantic(
            plan_row, ps,
            max_attempts=policy.max_attempts,
            output_shape=shape_by_key.get(ps.key),
        ))

    db.add_all(rows)
    await db.flush()
    return rows


class PlanContractError(Exception):
    """A plan has unresolvable contract gaps (dangling refs / unshaped
    producers) that survived auto-repair. Such a plan must not be dispatched —
    it would die at runtime as ReferenceError/OutputSchemaError."""

    def __init__(self, gaps: list) -> None:
        self.gaps = list(gaps)
        detail = "; ".join(
            f"{getattr(g, 'step_key', '?')}: {getattr(g, 'detail', g)}" for g in self.gaps
        )
        super().__init__(detail or "plan has unresolvable contract gaps")


def _linker_lite_steps(steps: list[PlanStep]) -> list[dict]:
    """Project plan steps onto the minimal shape the contract linker reads."""
    from packages.core.plans.refs import extract_step_refs

    return [
        {
            "key": s.key,
            "kind": s.kind,
            "output_shape": s.output_shape,
            "input_refs": extract_step_refs(s.params),
        }
        for s in steps
    ]


def plan_contract_gaps(steps: list[PlanStep]) -> list:
    """Return the contract linker's unfixable gaps (``LinkIssue`` list) for a
    plan, after auto-repair. Empty list means the plan is contract-clean (every
    consumed value is producible, every produced value is shaped). Pure — no
    side effects; callers decide whether to enforce."""
    from packages.core.contracts.linker import repair_plan

    _repaired, remaining = repair_plan(_linker_lite_steps(steps))
    return remaining


def _resolve_output_shapes(steps: list[PlanStep]) -> dict[str, str]:
    """Run the plan-time linker over the steps: auto-repair missing output
    shapes and report unfixable I/O gaps. Returns ``{step_key: shape_name}``
    for steps that resolved to a canonical shape (explicit or inferred)."""
    from packages.core.contracts.linker import repair_plan

    repaired, remaining = repair_plan(_linker_lite_steps(steps))
    for issue in remaining:
        logger.warning(
            "plan-time contract gap: step=%s kind=%s detail=%s",
            issue.step_key, issue.kind, issue.detail,
        )
    return {s["key"]: s["output_shape"] for s in repaired if s.get("output_shape")}


def _step_from_pydantic(
    plan_row: ExecutionPlan,
    ps: PlanStep,
    *,
    max_attempts: int | None = None,
    output_shape: str | None = None,
) -> ExecutionStep:
    # When a step resolves to a canonical shape (declared or linker-inferred),
    # derive expected_output_schema from the shape so producer/normalizer/
    # validator share one vocabulary. For free-form kinds (llm/subagent) the
    # canonical shape WINS over a hand-written schema — the Planner's guessed
    # schema (e.g. {text} on a step that returns {drafts:[...]}) is exactly the
    # OutputSchemaError source. Structured kinds keep an explicit schema: it's a
    # real contract with an external system, not a guess.
    expected_output_schema = ps.expected_output_schema
    if output_shape:
        from packages.core.contracts.shapes import get_shape
        try:
            shape_schema = get_shape(output_shape).json_schema()
        except KeyError:
            shape_schema = None
        if shape_schema is not None and (
            expected_output_schema is None or ps.kind in ("llm", "subagent")
        ):
            expected_output_schema = shape_schema
    return ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_row.id,
        entity_id=plan_row.entity_id,
        workspace_id=plan_row.workspace_id,
        step_key=ps.key,
        kind=ps.kind,
        service_key=ps.service_key,
        provider=ps.provider,
        action_key=ps.action_key,
        capability_id=ps.capability_id,
        integration_id=ps.integration_id,
        params=ps.params,
        expected_input_schema=ps.expected_input_schema,
        expected_output_schema=expected_output_schema,
        depends_on=list(ps.depends_on),
        step_status="pending",
        risk_level=ps.risk_level,
        requires_approval=ps.requires_approval,
        max_attempts=max_attempts or ps.max_attempts,
    )


# ── Reads ─────────────────────────────────────────────────────────────

async def get_plan(
    db: AsyncSession, plan_id: str,
    *, entity_id: Optional[str] = None,
) -> Optional[ExecutionPlan]:
    stmt = select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    if entity_id is not None:
        stmt = stmt.where(ExecutionPlan.entity_id == entity_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_step(db: AsyncSession, step_id: str) -> Optional[ExecutionStep]:
    return (await db.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one_or_none()


async def list_plan_steps(
    db: AsyncSession, plan_id: str,
) -> list[ExecutionStep]:
    return list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id == plan_id)
        .order_by(ExecutionStep.created_at)
    )).scalars().all())


# ── Lifecycle ────────────────────────────────────────────────────────

async def cancel_plan(
    db: AsyncSession, plan_id: str,
    *, reason: Optional[str] = None,
) -> Optional[ExecutionPlan]:
    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    )).scalar_one_or_none()
    if not plan or plan.status in ("completed", "cancelled", "failed"):
        return plan

    now = datetime.now(timezone.utc)
    plan.status = "cancelled"
    plan.completed_at = now
    if reason:
        plan.last_error = {"cancelled": reason}

    # Mark any non-terminal steps as cancelled too.
    steps = await list_plan_steps(db, plan_id)
    for s in steps:
        if s.step_status in ("pending", "running", "waiting_human"):
            s.step_status = "cancelled"
            s.finished_at = now

    await db.flush()
    return plan
