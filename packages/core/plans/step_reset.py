"""Revive never-ran downstream steps when an upstream step is retried.

A single-step retry usually targets the first failed step of a dependency
chain. Its descendants are terminal too — ``skipped`` from the earlier
failed run, or ``failed`` without ever running because the executor held
them at the boundary (``UpstreamPartialResult``) — so the executor would
never revisit them unless the chain is revived. A step that genuinely ran
and failed keeps its own retry decision.

Shared by the manual retry surface (``apps/api/routers/plans.py``) and the
supervisor's step re-run (``packages/core/plans/executor.py``) so the two
cannot drift on what "never ran" means.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    LEASE_REVOKED_ERROR_TYPE,
    UPSTREAM_PARTIAL_RESULT_ERROR_TYPE,
    ExecutionStepStatus,
    WorkLeaseStatus,
    is_coordinator_child_params,
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


def is_typed_coordinator_child(step: Any) -> bool:
    """Whether ``step`` is a dormant/releasable V2 coordinator child."""

    params = getattr(step, "params", None)
    return (
        is_coordinator_child_params(params)
        and params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is True
    )


def reset_step_to_retry_ready(step: Any) -> None:
    """Apply the shared retry admission transition.

    Ordinary Steps and coordinator roots return to ``pending`` exactly as
    before.  A typed coordinator child must instead return behind its durable
    parent-owned release gate: ``paused`` plus ``released=false``.  Otherwise
    a generic Task/Plan/Supervisor retry can place it directly on the
    Dispatcher's pending queue and bypass the coordinator contract.
    """

    if is_typed_coordinator_child(step):
        params = dict(step.params or {})
        params[COORDINATOR_CHILD_RELEASED_PARAM_KEY] = False
        params.pop(COORDINATOR_CHILD_WAKE_AT_PARAM_KEY, None)
        params.pop(COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY, None)
        params.pop(COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY, None)
        step.params = params
        step.step_status = ExecutionStepStatus.PAUSED.value
    else:
        step.step_status = ExecutionStepStatus.PENDING.value
    step.current_lease_id = None


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
    """Lock a Plan's runtime rows in the canonical Lease -> Step order.

    Callers already own the Plan (and Task, when present).  Matching the
    Dispatcher report path's Lease -> Step suffix prevents cancellation from
    deadlocking a worker completion while ensuring the refreshed Step state is
    evaluated only after every live lease is fenced.
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
    Keeping this ordering and transition in one helper makes Plan cancellation
    and executor orphan cleanup share the same concurrency semantics.
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
        # The worker fence is closed before the Step becomes terminal.  A late
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


def never_ran_downstream(step: Any) -> bool:
    """Skipped, or failed at the executor's boundary gate without running."""
    status = getattr(step, "step_status", None)
    if status == ExecutionStepStatus.SKIPPED:
        return True
    error = getattr(step, "error", None)
    error = error if isinstance(error, dict) else {}
    return (
        status == ExecutionStepStatus.FAILED
        and str(error.get("type") or "") == UPSTREAM_PARTIAL_RESULT_ERROR_TYPE
    )


def reset_never_ran_downstream_steps(
    steps: Iterable[Any],
    *,
    retried_step_keys: set[str],
    reset_step: Callable[[Any], None],
) -> list[Any]:
    """Reset never-ran descendants unblocked by the retried steps.

    ``reset_step`` performs the caller's own reset (the manual surface stamps
    the operator note; the supervisor re-run does not), so the algorithm
    stays one and the bookkeeping stays with the caller. Returns the steps
    that were reset, in dependency order.
    """
    rows = list(steps)
    done_or_retried = {
        step.step_key
        for step in rows
        if step.step_status == ExecutionStepStatus.DONE or step.step_key in retried_step_keys
    }
    revived_keys = set(retried_step_keys)
    reset: list[Any] = []

    changed = True
    while changed:
        changed = False
        for step in rows:
            if step.step_key in done_or_retried or not never_ran_downstream(step):
                continue
            deps = [str(dep) for dep in (getattr(step, "depends_on", None) or []) if dep]
            if not deps:
                continue
            if not any(dep in revived_keys for dep in deps):
                continue
            if not all(dep in done_or_retried for dep in deps):
                continue
            reset_step(step)
            reset.append(step)
            done_or_retried.add(step.step_key)
            revived_keys.add(step.step_key)
            changed = True

    return reset
