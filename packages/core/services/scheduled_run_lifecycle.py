"""Typed lifecycle helpers for scheduled child admission and settlement."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any

from packages.core.constants.execution import (
    SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
    SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
    ScheduledChildAdmissionStatus,
    ScheduledChildExecutionState,
    ScheduledDispatchKind,
    ScheduledRecoveryKind,
    ScheduledResultProjectionKind,
    ScheduledResultProjectionState,
    ScheduledRunStatus,
    ScheduledSettlementKind,
)


EXECUTION_STATE_KEY = "scheduled_execution_state"
EXECUTION_CHILD_KIND_KEY = "scheduled_child_kind"
EXECUTION_CHILD_ID_KEY = "scheduled_child_id"
SETTLEMENT_KEY = "scheduled_settlement"
RESULT_PROJECTION_KEY = "scheduled_result_projection"
RESULT_PROJECTION_QUARANTINE_KEY = "scheduled_result_projection_quarantine"
RECOVERY_EPOCH_PATTERN = r"^[0-9]{1,10}(?:\.[0-9]{1,6})?$"
RECOVERY_MAX_EPOCH_SECONDS = 9_999_999_999
_DISPATCH_KEYS = ("dispatch", "dispatch_status", "dispatch_ledger")
_TERMINAL_RUN_STATUSES = frozenset({
    ScheduledRunStatus.SUCCESS,
    ScheduledRunStatus.COMPLETED,
    ScheduledRunStatus.SKIPPED,
    ScheduledRunStatus.CANCELLED,
    ScheduledRunStatus.ERROR,
})


@dataclass(frozen=True)
class ScheduledRunOutcome:
    """Factory-owned terminal fact for one ScheduledJobRun."""

    status: ScheduledRunStatus
    result: dict[str, Any] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("scheduled run outcome requires a terminal status")

    @classmethod
    def from_execution(
        cls,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        status: ScheduledRunStatus | None = None,
    ) -> "ScheduledRunOutcome":
        if status is None:
            if error:
                status = ScheduledRunStatus.ERROR
            elif result and result.get("skipped"):
                status = ScheduledRunStatus.SKIPPED
            elif (
                result
                and result.get("status") == ScheduledRunStatus.CANCELLED.value
            ):
                status = ScheduledRunStatus.CANCELLED
            else:
                status = ScheduledRunStatus.COMPLETED
        return cls(status=status, result=result, error=error)

    @classmethod
    def cancelled(cls, reason: str) -> "ScheduledRunOutcome":
        return cls(
            status=ScheduledRunStatus.CANCELLED,
            result={
                "status": ScheduledRunStatus.CANCELLED.value,
                "skipped": True,
                "reason": reason,
            },
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ScheduledRunOutcome | None":
        if not isinstance(payload, dict):
            return None
        try:
            status = ScheduledRunStatus(str(payload.get("status") or ""))
        except ValueError:
            return None
        if status not in _TERMINAL_RUN_STATUSES:
            return None
        raw_result = payload.get("result")
        if raw_result is not None and not isinstance(raw_result, dict):
            return None
        raw_error = payload.get("error")
        error = str(raw_error) if raw_error is not None else None
        return cls(status=status, result=raw_result, error=error)


@dataclass(frozen=True)
class ScheduledRunSettlement:
    """Serializable recovery instruction produced after business completion."""

    kind: ScheduledSettlementKind
    outcome: ScheduledRunOutcome
    child_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        result: dict[str, Any] | None,
        error: str | None,
        kind: ScheduledSettlementKind = ScheduledSettlementKind.GENERIC,
        child_id: str | None = None,
        status: ScheduledRunStatus | None = None,
    ) -> "ScheduledRunSettlement":
        return cls(
            kind=kind,
            child_id=child_id,
            outcome=ScheduledRunOutcome.from_execution(
                result=result,
                error=error,
                status=status,
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": self.kind.value,
            "child_id": self.child_id,
            "outcome": self.outcome.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ScheduledRunSettlement | None":
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        try:
            kind = ScheduledSettlementKind(str(payload.get("kind") or ""))
        except ValueError:
            return None
        outcome = ScheduledRunOutcome.from_payload(payload.get("outcome"))
        if outcome is None:
            return None
        raw_child_id = payload.get("child_id")
        child_id = str(raw_child_id) if raw_child_id else None
        if kind is ScheduledSettlementKind.AGENT_TASK and not child_id:
            return None
        return cls(kind=kind, outcome=outcome, child_id=child_id)


@dataclass(frozen=True)
class ScheduledChildAdmission:
    """Typed result returned by the atomic scheduler admission factory."""

    status: ScheduledChildAdmissionStatus
    reason: str

    @property
    def admitted(self) -> bool:
        return self.status is ScheduledChildAdmissionStatus.ADMITTED


@dataclass(frozen=True)
class ScheduledResultProjection:
    """Factory-owned durable intent for one user-visible result projection."""

    kind: ScheduledResultProjectionKind
    state: ScheduledResultProjectionState
    task_id: str
    error: str | None = None
    recovery_attempts: int = 0
    recovery_chain_id: str | None = None

    @classmethod
    def workspace_chat(cls, *, task_id: str) -> "ScheduledResultProjection":
        return cls(
            kind=ScheduledResultProjectionKind.WORKSPACE_CHAT,
            state=ScheduledResultProjectionState.PENDING,
            task_id=task_id,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "ScheduledResultProjection | None":
        if not isinstance(payload, dict):
            return None
        try:
            kind = ScheduledResultProjectionKind(str(payload.get("kind") or ""))
            state = ScheduledResultProjectionState(str(payload.get("state") or ""))
        except ValueError:
            return None
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return None
        recovery_attempts = payload.get("recovery_attempts", 0)
        if (
            isinstance(recovery_attempts, bool)
            or not isinstance(recovery_attempts, int)
            or recovery_attempts < 0
        ):
            return None
        raw_recovery_chain_id = payload.get("recovery_chain_id")
        if raw_recovery_chain_id is not None and not isinstance(
            raw_recovery_chain_id,
            str,
        ):
            return None
        recovery_chain_id = str(raw_recovery_chain_id or "").strip() or None
        if recovery_chain_id is not None and len(recovery_chain_id) > 200:
            return None
        error = str(payload.get("error") or "").strip() or None
        return cls(
            kind=kind,
            state=state,
            task_id=task_id,
            error=error,
            recovery_attempts=recovery_attempts,
            recovery_chain_id=recovery_chain_id,
        )

    def with_state(
        self,
        state: ScheduledResultProjectionState,
        *,
        error: str | None = None,
    ) -> "ScheduledResultProjection":
        return ScheduledResultProjection(
            kind=self.kind,
            state=state,
            task_id=self.task_id,
            error=error,
            recovery_attempts=self.recovery_attempts,
            recovery_chain_id=self.recovery_chain_id,
        )

    def for_retry(
        self,
        *,
        error: str | None = None,
        recovery_chain_id: str | None = None,
    ) -> "ScheduledResultProjection":
        """Record one durable Celery retry chain for this projection."""

        normalized_chain_id = str(recovery_chain_id or "").strip() or None
        already_recorded = (
            normalized_chain_id is not None
            and normalized_chain_id == self.recovery_chain_id
        )
        return ScheduledResultProjection(
            kind=self.kind,
            state=ScheduledResultProjectionState.PENDING,
            task_id=self.task_id,
            error=self.error if error is None else error,
            recovery_attempts=(
                self.recovery_attempts
                if already_recorded
                else self.recovery_attempts + 1
            ),
            recovery_chain_id=(
                self.recovery_chain_id
                if normalized_chain_id is None
                else normalized_chain_id
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "state": self.state.value,
            "task_id": self.task_id,
            "recovery_attempts": self.recovery_attempts,
        }
        if self.error:
            payload["error"] = self.error[:1000]
        if self.recovery_chain_id:
            payload["recovery_chain_id"] = self.recovery_chain_id
        return payload


def scheduled_result_projection(run: object) -> ScheduledResultProjection | None:
    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return None
    return ScheduledResultProjection.from_payload(result.get(RESULT_PROJECTION_KEY))


def apply_scheduled_result_projection(
    run: object,
    projection: ScheduledResultProjection,
) -> None:
    result = dict(getattr(run, "result", None) or {})
    result[RESULT_PROJECTION_KEY] = projection.to_payload()
    run.result = result


def scheduled_recovery_next_attempt_at_key(kind: ScheduledRecoveryKind) -> str:
    """Return the JSON key owned by one independent recovery phase."""

    return f"scheduled_{kind.value}_recovery_next_attempt_at"


def defer_scheduled_recovery(
    run: object,
    *,
    kind: ScheduledRecoveryKind,
    now: datetime,
    retry_after: timedelta,
) -> None:
    """Move one attempted recovery behind newer due handoffs."""

    if now.tzinfo is None:
        raise ValueError("scheduled recovery time must be timezone-aware")
    result = dict(getattr(run, "result", None) or {})
    result[scheduled_recovery_next_attempt_at_key(kind)] = (
        now.astimezone(timezone.utc) + retry_after
    ).timestamp()
    run.result = result


def normalize_scheduled_recovery_deadline(
    run: object,
    *,
    kind: ScheduledRecoveryKind,
) -> bool:
    """Rewrite one legacy ISO deadline to the canonical numeric epoch."""

    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return False
    key = scheduled_recovery_next_attempt_at_key(kind)
    raw_next_attempt_at = result.get(key)
    if isinstance(raw_next_attempt_at, bool) or isinstance(
        raw_next_attempt_at,
        (int, float),
    ):
        return False
    if not isinstance(raw_next_attempt_at, str):
        return False
    try:
        deadline = datetime.fromisoformat(raw_next_attempt_at)
        if deadline.tzinfo is None:
            return False
        deadline_epoch = deadline.astimezone(timezone.utc).timestamp()
    except (OverflowError, TypeError, ValueError):
        return False
    normalized = dict(result)
    normalized[key] = deadline_epoch
    run.result = normalized
    return True


def scheduled_recovery_is_due(
    run: object,
    *,
    kind: ScheduledRecoveryKind,
    now: datetime,
) -> bool:
    """Return whether one recovery phase is due, treating bad metadata as due."""

    if now.tzinfo is None:
        raise ValueError("scheduled recovery time must be timezone-aware")
    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return True
    raw_next_attempt_at = result.get(scheduled_recovery_next_attempt_at_key(kind))
    now_epoch = now.astimezone(timezone.utc).timestamp()
    if (
        not isinstance(raw_next_attempt_at, bool)
        and isinstance(raw_next_attempt_at, (int, float))
    ):
        return (
            raw_next_attempt_at < 0
            or raw_next_attempt_at > RECOVERY_MAX_EPOCH_SECONDS
            or not math.isfinite(raw_next_attempt_at)
            or raw_next_attempt_at <= now_epoch
        )
    if not isinstance(raw_next_attempt_at, str):
        return True
    if re.fullmatch(RECOVERY_EPOCH_PATTERN, raw_next_attempt_at):
        try:
            deadline_epoch = float(raw_next_attempt_at)
            return (
                deadline_epoch > RECOVERY_MAX_EPOCH_SECONDS
                or not math.isfinite(deadline_epoch)
                or deadline_epoch <= now_epoch
            )
        except (OverflowError, ValueError):
            return True
    try:
        deadline = datetime.fromisoformat(raw_next_attempt_at)
        if deadline.tzinfo is None:
            return True
        return deadline.astimezone(timezone.utc) <= now.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return True


def merge_scheduled_run_result(
    existing: object,
    result: dict[str, Any] | None = None,
    *,
    execution_state: ScheduledChildExecutionState | None = None,
    settlement: ScheduledRunSettlement | None = None,
) -> dict[str, Any]:
    """Merge business output without discarding durable dispatch metadata."""

    existing_result = dict(existing) if isinstance(existing, dict) else {}
    merged = dict(existing_result)
    if result:
        merged.update(result)
    for key in _DISPATCH_KEYS:
        if key in existing_result:
            merged[key] = existing_result[key]
    if execution_state is not None:
        merged[EXECUTION_STATE_KEY] = execution_state.value
    if settlement is not None:
        merged[SETTLEMENT_KEY] = settlement.to_payload()
    elif execution_state is ScheduledChildExecutionState.SETTLED:
        merged.pop(SETTLEMENT_KEY, None)
    return merged


def _recorded_scheduled_child(
    run: object,
) -> tuple[ScheduledDispatchKind | None, str | None]:
    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return None, None
    try:
        kind = ScheduledDispatchKind(str(result.get(EXECUTION_CHILD_KIND_KEY) or ""))
    except ValueError:
        kind = None
    child_id = str(result.get(EXECUTION_CHILD_ID_KEY) or "").strip() or None
    return kind, child_id


def _dispatch_matches_scheduled_child(
    run: object,
    *,
    child_kind: ScheduledDispatchKind,
    child_id: str,
) -> bool:
    """Validate a delivered child against the immutable dispatch envelope."""

    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return True  # Legacy rows are fenced by the child provenance below.
    dispatch = result.get("dispatch")
    if dispatch is None:
        return True
    if not isinstance(dispatch, dict):
        return False
    try:
        dispatch_kind = ScheduledDispatchKind(str(dispatch.get("kind") or ""))
    except ValueError:
        return False
    args = dispatch.get("args")
    return (
        dispatch_kind is child_kind
        and isinstance(args, list)
        and bool(args)
        and str(args[0]) == child_id
    )


async def _scheduled_child_has_exact_provenance(
    db,
    *,
    child_kind: ScheduledDispatchKind,
    child_id: str,
    run_id: str,
    job_id: str,
) -> bool:
    """Lock and verify the child created for this exact occurrence."""

    from sqlalchemy import select

    if child_kind is ScheduledDispatchKind.AGENT_TASK:
        from packages.core.models.task import Task

        child = (await db.execute(
            select(Task).where(Task.id == child_id).with_for_update()
        )).scalar_one_or_none()
        provenance = child.details if child is not None else None
    elif child_kind is ScheduledDispatchKind.WORKFLOW:
        from packages.core.models.workflow import WorkflowRun

        child = (await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == child_id).with_for_update()
        )).scalar_one_or_none()
        provenance = child.trigger_data if child is not None else None
    else:
        return False
    return (
        isinstance(provenance, dict)
        and str(provenance.get("scheduled_run_id") or "") == run_id
        and str(provenance.get("scheduled_job_id") or "") == job_id
    )


def scheduled_child_execution_state(run: object) -> ScheduledChildExecutionState | None:
    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return None
    try:
        return ScheduledChildExecutionState(str(result.get(EXECUTION_STATE_KEY) or ""))
    except ValueError:
        return None


def scheduled_run_settlement(run: object) -> ScheduledRunSettlement | None:
    result = getattr(run, "result", None)
    if not isinstance(result, dict):
        return None
    if scheduled_child_execution_state(run) is not ScheduledChildExecutionState.SETTLEMENT_PENDING:
        return None
    return ScheduledRunSettlement.from_payload(result.get(SETTLEMENT_KEY))


def apply_scheduled_run_outcome(
    run: object,
    outcome: ScheduledRunOutcome,
    *,
    completed_at: datetime | None = None,
) -> None:
    """Apply one terminal outcome while preserving its dispatch envelope."""

    finished_at = completed_at or datetime.now(timezone.utc)
    run.status = outcome.status.value
    run.completed_at = finished_at
    if getattr(run, "started_at", None):
        run.duration_ms = (
            finished_at - run.started_at
        ).total_seconds() * 1000
    run.error = outcome.error[:1000] if outcome.error else None
    existing_result = getattr(run, "result", None)
    has_execution_envelope = isinstance(existing_result, dict) and any(
        key in existing_result
        for key in (*_DISPATCH_KEYS, EXECUTION_STATE_KEY, SETTLEMENT_KEY)
    )
    run.result = merge_scheduled_run_result(
        existing_result,
        outcome.result,
        execution_state=(
            ScheduledChildExecutionState.SETTLED
            if has_execution_envelope
            else None
        ),
    )


async def persist_scheduled_run_settlement_pending(
    db,
    *,
    run_id: str,
    job_id: str | None,
    settlement: ScheduledRunSettlement,
) -> bool:
    """Fence completed business work so recovery retries only settlement."""

    from packages.core.services.scheduler_service import lock_scheduled_job_and_run

    _job, run = await lock_scheduled_job_and_run(
        db,
        job_id=job_id,
        run_id=run_id,
    )
    if run is None or run.status != ScheduledRunStatus.RUNNING.value:
        return False
    run.result = merge_scheduled_run_result(
        run.result,
        settlement.outcome.result,
        execution_state=ScheduledChildExecutionState.SETTLEMENT_PENDING,
        settlement=settlement,
    )
    defer_scheduled_recovery(
        run,
        kind=ScheduledRecoveryKind.SETTLEMENT,
        now=datetime.now(timezone.utc),
        retry_after=timedelta(
            seconds=SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
        ),
    )
    await db.flush()
    return True


async def cancel_scheduled_child_if_pristine(
    db,
    *,
    child_kind: ScheduledDispatchKind | None,
    child_id: str | None,
    run_id: str,
    job_id: str,
) -> bool:
    """Idempotently cancel a prepared child that has not begun execution.

    The exact scheduler provenance check prevents a stale or malformed broker
    message from terminalizing an unrelated Task/WorkflowRun.  Callers that do
    not already own the parent admission lock must also own the child's shared
    execution claim before entering this helper.
    """
    if child_kind is None or not child_id:
        return False

    from sqlalchemy import select

    if child_kind is ScheduledDispatchKind.AGENT_TASK:
        from packages.core.constants.task import TaskStatus
        from packages.core.models.task import Task
        from packages.core.services.task_state_machine import apply_task_status_transition

        task = (await db.execute(
            select(Task).where(Task.id == child_id).with_for_update()
        )).scalar_one_or_none()
        if task is None:
            return False
        details = task.details if isinstance(task.details, dict) else {}
        if (
            str(details.get("scheduled_run_id") or "") != run_id
            or str(details.get("scheduled_job_id") or "") != job_id
        ):
            return False
        if task.status == TaskStatus.CANCELLED:
            return True
        if task.status not in {
            TaskStatus.CREATED,
            TaskStatus.PENDING,
            TaskStatus.SCHEDULED,
        }:
            return False
        task.details = {
            **details,
            "scheduled_suppression_reason": (
                "Scheduled occurrence closed before child execution"
            ),
        }
        await apply_task_status_transition(
            task,
            TaskStatus.CANCELLED.value,
            db=db,
        )
        return True

    if child_kind is ScheduledDispatchKind.WORKFLOW:
        from packages.core.ai.workflow_runner import (
            mark_workflow_terminal_effects_pending,
        )
        from packages.core.models.workflow import WorkflowRun
        from packages.core.services.workflow_chat_projection import (
            project_workflow_run_status,
        )
        from packages.core.services.workflow_run_control import cancel_run

        workflow_run = (await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == child_id).with_for_update()
        )).scalar_one_or_none()
        if workflow_run is None:
            return False
        trigger_data = (
            workflow_run.trigger_data
            if isinstance(workflow_run.trigger_data, dict)
            else {}
        )
        if (
            str(trigger_data.get("scheduled_run_id") or "") != run_id
            or str(trigger_data.get("scheduled_job_id") or "") != job_id
        ):
            return False
        if workflow_run.status == "cancelled":
            return True
        if workflow_run.status not in {"pending", "running"}:
            return False
        if (
            workflow_run.current_step_id
            or workflow_run.step_results
            or workflow_run.execution_trace
        ):
            return False
        cancel_run(workflow_run, actor_id="scheduler_lifecycle_gate")
        mark_workflow_terminal_effects_pending(workflow_run)
        await project_workflow_run_status(db, run=workflow_run)
        from packages.core.ledger.adapters import record_workflow_run_status

        await record_workflow_run_status(db, workflow_run)
        return True

    return False


async def admit_scheduled_child(
    db,
    *,
    run_id: str,
    job_id: str | None,
    child_kind: ScheduledDispatchKind | None = None,
    child_id: str | None = None,
) -> ScheduledChildAdmission:
    """Atomically linearize child admission against pause/delete writers."""

    from packages.core.services.scheduler_service import (
        lock_scheduled_job_and_run,
        reconcile_scheduled_job_run_projection,
    )

    job, run = await lock_scheduled_job_and_run(
        db,
        job_id=job_id,
        run_id=run_id,
    )
    if job is None or run is None:
        return ScheduledChildAdmission(
            ScheduledChildAdmissionStatus.CLOSED,
            "scheduled_occurrence_missing",
        )
    if run.status != ScheduledRunStatus.RUNNING.value:
        return ScheduledChildAdmission(
            ScheduledChildAdmissionStatus.CLOSED,
            "scheduled_occurrence_terminal",
        )
    if (
        scheduled_child_execution_state(run)
        is ScheduledChildExecutionState.SETTLEMENT_PENDING
    ):
        return ScheduledChildAdmission(
            ScheduledChildAdmissionStatus.SETTLEMENT_PENDING,
            "scheduled_settlement_pending",
        )
    execution_state = scheduled_child_execution_state(run)
    recorded_kind, recorded_child_id = _recorded_scheduled_child(run)
    if execution_state is ScheduledChildExecutionState.ADMITTED:
        if child_kind is not None or child_id is not None:
            if child_kind is None or not child_id:
                return ScheduledChildAdmission(
                    ScheduledChildAdmissionStatus.CLOSED,
                    "scheduled_child_identity_incomplete",
                )
            if (
                recorded_kind is not None
                and recorded_kind is not child_kind
            ) or (
                recorded_child_id is not None
                and recorded_child_id != child_id
            ):
                return ScheduledChildAdmission(
                    ScheduledChildAdmissionStatus.CLOSED,
                    "scheduled_child_identity_mismatch",
                )
            if not _dispatch_matches_scheduled_child(
                run,
                child_kind=child_kind,
                child_id=child_id,
            ) or not await _scheduled_child_has_exact_provenance(
                db,
                child_kind=child_kind,
                child_id=child_id,
                run_id=run.id,
                job_id=job.job_id,
            ):
                return ScheduledChildAdmission(
                    ScheduledChildAdmissionStatus.CLOSED,
                    "scheduled_child_lineage_mismatch",
                )
            if recorded_kind is None or recorded_child_id is None:
                admitted_result = dict(run.result or {})
                admitted_result[EXECUTION_CHILD_KIND_KEY] = child_kind.value
                admitted_result[EXECUTION_CHILD_ID_KEY] = child_id
                run.result = admitted_result
                await db.flush()
        return ScheduledChildAdmission(
            ScheduledChildAdmissionStatus.ADMITTED,
            (
                "scheduled_child_admitted_before_disable"
                if not job.enabled
                else "scheduled_child_already_admitted"
            ),
        )

    if child_kind is not None or child_id is not None:
        if child_kind is None or not child_id:
            return ScheduledChildAdmission(
                ScheduledChildAdmissionStatus.CLOSED,
                "scheduled_child_identity_incomplete",
            )
        if not _dispatch_matches_scheduled_child(
            run,
            child_kind=child_kind,
            child_id=child_id,
        ) or not await _scheduled_child_has_exact_provenance(
            db,
            child_kind=child_kind,
            child_id=child_id,
            run_id=run.id,
            job_id=job.job_id,
        ):
            return ScheduledChildAdmission(
                ScheduledChildAdmissionStatus.CLOSED,
                "scheduled_child_lineage_mismatch",
            )

    result = run.result if isinstance(run.result, dict) else {}
    dispatch_ledger = result.get("dispatch_ledger")
    dispatch_revision = (
        dispatch_ledger.get("revision")
        if isinstance(dispatch_ledger, dict)
        else None
    )
    if (
        isinstance(dispatch_revision, int)
        and not isinstance(dispatch_revision, bool)
        and dispatch_revision != job.revision
    ):
        outcome = ScheduledRunOutcome.cancelled(
            "scheduled_job_revision_changed"
        )
        apply_scheduled_run_outcome(run, outcome)
        await cancel_scheduled_child_if_pristine(
            db,
            child_kind=child_kind,
            child_id=child_id,
            run_id=run.id,
            job_id=job.job_id,
        )
        await reconcile_scheduled_job_run_projection(
            db,
            job,
            finalized_run_id=run.id,
        )
        from packages.core.ledger.adapters import record_automation_run_finished

        await record_automation_run_finished(
            db,
            job,
            run_id=run.id,
            status=ScheduledRunStatus.CANCELLED.value,
        )
        return ScheduledChildAdmission(
            ScheduledChildAdmissionStatus.CANCELLED,
            "scheduled_job_revision_changed",
        )

    if not job.enabled:
        outcome = ScheduledRunOutcome.cancelled("scheduled_job_disabled")
        apply_scheduled_run_outcome(run, outcome)
        await cancel_scheduled_child_if_pristine(
            db,
            child_kind=child_kind,
            child_id=child_id,
            run_id=run.id,
            job_id=job.job_id,
        )
        await reconcile_scheduled_job_run_projection(
            db,
            job,
            finalized_run_id=run.id,
        )
        from packages.core.ledger.adapters import record_automation_run_finished

        await record_automation_run_finished(
            db,
            job,
            run_id=run.id,
            status=ScheduledRunStatus.CANCELLED.value,
        )
        return ScheduledChildAdmission(
            ScheduledChildAdmissionStatus.CANCELLED,
            "scheduled_job_disabled",
        )

    admitted_result = merge_scheduled_run_result(
        run.result,
        execution_state=ScheduledChildExecutionState.ADMITTED,
    )
    if child_kind is not None and child_id:
        admitted_result[EXECUTION_CHILD_KIND_KEY] = child_kind.value
        admitted_result[EXECUTION_CHILD_ID_KEY] = child_id
    run.result = admitted_result
    defer_scheduled_recovery(
        run,
        kind=ScheduledRecoveryKind.EXECUTION,
        now=datetime.now(timezone.utc),
        retry_after=timedelta(
            seconds=SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
        ),
    )
    await db.flush()
    return ScheduledChildAdmission(
        ScheduledChildAdmissionStatus.ADMITTED,
        "scheduled_child_admitted",
    )
