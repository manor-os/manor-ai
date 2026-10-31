"""Execution-step state primitives of the worker-job transport.

A worker job (local CLI action, remote coding run, local worker) is one
``ExecutionStep`` leased to an external worker through ``WorkLease``. These
helpers keep its approval intent and its cancellation fence in one place.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    LEASE_REVOKED_ERROR_TYPE,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.models.execution import ExecutionStep
from packages.core.models.worker import WorkLease, WorkerActivityLog


_OPEN_STEP_STATUSES = frozenset({
    ExecutionStepStatus.PENDING.value,
    ExecutionStepStatus.RUNNING.value,
    ExecutionStepStatus.WAITING_HUMAN.value,
    ExecutionStepStatus.PAUSED.value,
})
_REVOCABLE_LEASE_STATUSES = frozenset({
    WorkLeaseStatus.ACTIVE.value,
    WorkLeaseStatus.NEEDS_HUMAN.value,
    WorkLeaseStatus.SUSPENDED.value,
})


def renew_step_approval_intent(step: Any) -> None:
    """Record a new user-requested execution; never use for automatic retry."""
    step.approval_operation_revision = int(getattr(step, "approval_operation_revision", 0) or 0) + 1


def step_approval_operation_id(step: Any) -> str:
    base = f"step:{step.plan_id or ''}:{step.id}"
    revision = int(getattr(step, "approval_operation_revision", 0) or 0)
    return f"{base}:intent:{revision}" if revision else base


async def lock_plan_leases_then_steps(
    db: AsyncSession,
    *,
    plan_id: str,
) -> tuple[list[WorkLease], list[ExecutionStep]]:
    """Lock a job's runtime rows in the canonical Lease -> Step order.

    Matching the Dispatcher report path's Lease -> Step suffix prevents
    cancellation from deadlocking a worker completion while ensuring the
    refreshed Step state is evaluated only after every live lease is fenced.
    """

    leases = list((await db.execute(
        select(WorkLease)
        .where(
            WorkLease.plan_id == plan_id,
            WorkLease.status.in_(sorted(_REVOCABLE_LEASE_STATUSES)),
        )
        .order_by(WorkLease.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    steps = list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id == plan_id)
        .order_by(ExecutionStep.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    return leases, steps


def revoke_leases_before_cancelling_steps(
    db: AsyncSession,
    *,
    leases: Iterable[WorkLease],
    steps: Iterable[ExecutionStep],
    step_ids: set[str],
    reason: str,
    now: datetime,
    clear_runtime_checkpoint: bool,
) -> list[ExecutionStep]:
    """Fence selected leases, then cancel their still-open Step rows.

    ``leases`` and ``steps`` must come from :func:`lock_plan_leases_then_steps`.
    """

    if not step_ids:
        return []
    lease_error = {"type": LEASE_REVOKED_ERROR_TYPE, "message": reason}
    for lease in leases:
        if lease.step_id not in step_ids or lease.status not in _REVOCABLE_LEASE_STATUSES:
            continue
        lease.status = WorkLeaseStatus.CANCELLED.value
        lease.execution_claim_id = None
        lease.execution_claimed_at = None
        lease.credential_leases = []
        lease.error = dict(lease_error)
        db.add(WorkerActivityLog(
            worker_id=lease.worker_id,
            event="lease_revoke",
            lease_id=lease.id,
            payload_summary={
                "step_id": lease.step_id,
                "plan_id": lease.plan_id,
                "reason": reason,
            },
        ))

    cancelled: list[ExecutionStep] = []
    for step in steps:
        if step.id not in step_ids or step.step_status not in _OPEN_STEP_STATUSES:
            continue
        # The worker fence is closed before the Step becomes terminal. A late
        # completion therefore fails its lease/claim check and cannot overwrite
        # this cancellation.
        step.current_lease_id = None
        step.step_status = ExecutionStepStatus.CANCELLED.value
        step.finished_at = now
        if clear_runtime_checkpoint and not any(
            isinstance(ref, dict) and ref.get("kind") == "tool_effect"
            for ref in (step.evidence_refs or [])
        ):
            step.runtime_checkpoint = None
        cancelled.append(step)
    return cancelled


__all__ = [
    "lock_plan_leases_then_steps",
    "renew_step_approval_intent",
    "revoke_leases_before_cancelling_steps",
    "step_approval_operation_id",
]
