"""Temporal activities — small, deterministic-friendly DB operations.

Activities are how a workflow talks to the outside world. The workflow
itself must be deterministic (no DB / network / time / random); every
side effect goes through an activity.

We deliberately keep activities tiny and idempotent: workflow replay
re-runs activities, so anything that mutates state must tolerate being
called twice. We rely on the underlying SQL operations to be idempotent
where possible (UPDATE ... WHERE step_status='running') and accept
benign duplication where not (chat events).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from temporalio import activity

from packages.core.database import async_session
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.workspace_chat import notifiers as chat_notify

logger = logging.getLogger(__name__)


# ── Plan reading ─────────────────────────────────────────────────────

@activity.defn
async def load_plan_dag(plan_id: str) -> dict:
    """Return the plan's DAG + entity/workspace metadata as a plain
    dict so the workflow can iterate without holding ORM objects.

    Also includes the ``execution_mode`` so the workflow can route
    sandbox plans to dry-run handling without a second activity call.
    """
    from sqlalchemy import select

    async with async_session() as db:
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
        )).scalar_one_or_none()
        if plan is None:
            raise ValueError(f"plan {plan_id} not found")

        # Load step rows so we can map step_key → step_id (workflow
        # signals carry step_key; Dispatcher operates on step_id).
        steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan_id)
            .order_by(ExecutionStep.created_at)
        )).scalars().all())

        return {
            "plan_id": plan.id,
            "entity_id": plan.entity_id,
            "workspace_id": plan.workspace_id,
            "execution_mode": plan.execution_mode,
            "approval_required": plan.approval_required,
            "task_id": plan.task_id,
            "steps": [
                {
                    "id": s.id,
                    "key": s.step_key,
                    "kind": s.kind,
                    "service_key": s.service_key,
                    "depends_on": list(s.depends_on or []),
                    "params": dict(s.params or {}),
                    "max_attempts": s.max_attempts,
                }
                for s in steps
            ],
        }


# ── Step state mutations ─────────────────────────────────────────────

@activity.defn
async def mark_step_pending_for_dispatcher(plan_id: str, step_key: str) -> str:
    """Flip a step to ``pending`` so the Dispatcher's checkout picks
    it up on the next worker heartbeat. Returns the step_id (the
    workflow may want to log it).

    Idempotent: if the step is already pending or running, no-op.
    """
    from sqlalchemy import select, update

    async with async_session() as db:
        step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == step_key,
            )
        )).scalar_one_or_none()
        if step is None:
            raise ValueError(f"step {plan_id}/{step_key} not found")

        # Only resurrect from terminal-but-retryable states.
        if step.step_status in ("done", "running"):
            return step.id

        step.step_status = "pending"
        step.error = None
        step.current_lease_id = None
        await db.commit()
        return step.id


@activity.defn
async def mark_step_done_inline(
    plan_id: str, step_key: str, result: dict,
) -> None:
    """Mark a step done without going through the Dispatcher — used
    for workflow-level inline kinds (sleep) where there's no worker."""
    from sqlalchemy import select

    async with async_session() as db:
        step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == step_key,
            )
        )).scalar_one_or_none()
        if step is None:
            return
        step.step_status = "done"
        step.result = result
        step.finished_at = datetime.now(timezone.utc)
        await db.commit()


@activity.defn
async def mark_step_waiting_human(
    plan_id: str, step_key: str, prompt: str,
) -> None:
    """Workflow-level human step — pause + post HITL chat card."""
    from sqlalchemy import select

    async with async_session() as db:
        step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == step_key,
            )
        )).scalar_one_or_none()
        if step is None:
            return
        step.step_status = "waiting_human"
        step.human_input_prompt = prompt
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
        )).scalar_one()
        await db.commit()

        # Best-effort chat post (own session inside notifier).
        await chat_notify.notify_step_needs_human(
            entity_id=plan.entity_id,
            workspace_id=plan.workspace_id,
            plan_id=plan_id,
            step_id=step.id,
            step_key=step_key,
            prompt=prompt,
            subscription_id=step.resolved_subscription_id,
        )


@activity.defn
async def mark_step_skipped(
    plan_id: str, step_key: str, reason: str,
) -> None:
    from sqlalchemy import select

    async with async_session() as db:
        step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == step_key,
            )
        )).scalar_one_or_none()
        if step is None:
            return
        step.step_status = "skipped"
        step.error = {"type": "skipped", "message": reason}
        step.finished_at = datetime.now(timezone.utc)
        await db.commit()


# ── Plan finalisation ────────────────────────────────────────────────

@activity.defn
async def finalize_plan(plan_id: str, status: str) -> None:
    """Mark plan terminal + emit plan_completed / plan_failed chat."""
    from sqlalchemy import select

    async with async_session() as db:
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
        )).scalar_one_or_none()
        if plan is None:
            return
        if plan.status in ("completed", "failed", "cancelled"):
            return  # already terminal — idempotent

        now = datetime.now(timezone.utc)
        plan.status = status
        plan.completed_at = now
        await db.commit()

        # Plan-level chat events.
        if not plan.workspace_id:
            return
        duration = None
        if plan.started_at and plan.completed_at:
            duration = (plan.completed_at - plan.started_at).total_seconds()

        if status == "completed":
            await chat_notify.notify_plan_completed(
                entity_id=plan.entity_id,
                workspace_id=plan.workspace_id,
                plan_id=plan.id,
                task_id=plan.task_id,
                duration_seconds=duration,
                cost_usd=(plan.cost_tracking or {}).get("usd"),
            )
        elif status == "failed":
            await chat_notify.notify_plan_failed(
                entity_id=plan.entity_id,
                workspace_id=plan.workspace_id,
                plan_id=plan.id,
                task_id=plan.task_id,
                error=plan.last_error,
            )


@activity.defn
async def announce_plan_started(plan_id: str) -> None:
    """Set plan status running + post the started chat event. Called
    once at the top of the workflow."""
    from sqlalchemy import select

    async with async_session() as db:
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
        )).scalar_one_or_none()
        if plan is None:
            return
        if plan.status == "draft":
            plan.status = "running"
            plan.started_at = datetime.now(timezone.utc)
            await db.commit()
            if plan.workspace_id:
                step_count = (await db.execute(
                    select(ExecutionStep.id).where(ExecutionStep.plan_id == plan_id)
                )).all()
                await chat_notify.notify_plan_started(
                    entity_id=plan.entity_id,
                    workspace_id=plan.workspace_id,
                    plan_id=plan.id,
                    task_id=plan.task_id,
                    task_title=None,
                    step_count=len(step_count),
                    execution_mode=plan.execution_mode,
                )


# ── Ref resolution ───────────────────────────────────────────────────

@activity.defn
async def resolve_step_refs(
    plan_id: str, step_key: str, prior_results: dict,
) -> Optional[dict]:
    """Apply ``${{ steps.X.result.path }}`` substitution to step.params
    in-place. Returns the resolved params (also persisted) so the
    workflow can pass them to subsequent activities if needed.

    Returning None signals an unrecoverable ReferenceError — workflow
    should mark the step failed and continue / abort.
    """
    from sqlalchemy import select
    from packages.core.plans.refs import ReferenceError, resolve_refs

    async with async_session() as db:
        step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan_id,
                ExecutionStep.step_key == step_key,
            )
        )).scalar_one_or_none()
        if step is None:
            return None
        try:
            resolved = resolve_refs(step.params or {}, prior_results)
        except ReferenceError as exc:
            step.step_status = "failed"
            step.error = {"type": "ReferenceError", "message": str(exc)}
            step.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return None
        step.params = resolved
        await db.commit()
        return resolved
