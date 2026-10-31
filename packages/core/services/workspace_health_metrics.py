"""Workspace health metrics — the one definition of a run's outcome.

Every surface that reports how a Workspace's work went classifies runs
through this module: the launch review, the Stats library, the evaluation
scorecard, the admin reliability metrics and the daily health briefing. A
success rate therefore means the same thing wherever it appears::

    success_rate = succeeded / (succeeded + failed)

A cancellation is nobody's failure: it is reported beside the rate, never
inside it. Work still running, waiting on a person, or never accepted is
not judged at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from packages.core.constants.approvals import ApprovalStatus, HitlType
from packages.core.constants.execution import (
    COORDINATOR_PARAM_KEY,
    ExecutionStepStatus,
    ScheduledRunStatus,
)
from packages.core.constants.proposal import ProposalItemStatus
from packages.core.constants.task import CancelSource, TaskLogType, TaskStatus
from packages.core.constants.workflow import WorkflowRunStatus
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_agent_child import (
    TASK_AGENT_CHILD_TERMINAL_PHASES,
    TaskAgentChildRun,
)
from packages.core.models.task_coordinator import TaskCoordinatorPhase
from packages.core.models.workflow import WorkflowRun

SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
IN_FLIGHT = "in_flight"

SUCCESS_RATE_PASS = 0.9
"""At or above: the surface passes."""
SUCCESS_RATE_FAIL = 0.7
"""Below: the surface fails. Between the two it warns."""
MIN_JUDGED_RUNS = 5
"""Fewer succeeded + failed runs than this is too little evidence to judge
a rate: with 3 runs a single failure already reads as 67%."""
APPROVAL_BACKLOG_AFTER = timedelta(hours=24)
"""An approval left pending this long means nobody is watching the queue."""
STALLED_AFTER = timedelta(hours=48)
"""In-progress work with no status change for this long is stuck."""
RUNTIME_MIGRATED_ERROR = "runtime_migrated"
"""Failure code the Plan/Step engine retirement (migration 20261025_10)
logged on the Tasks it stopped. The platform stopped them; they say nothing
about the Workspace."""

# Task Coordinator V3 parks a task whose execution failed as ``blocked``
# (waiting for a person) rather than ``failed``; both are failed work.
TASK_OUTCOMES: Mapping[str, str] = MappingProxyType({
    TaskStatus.COMPLETED.value: SUCCEEDED,
    TaskStatus.FAILED.value: FAILED,
    TaskStatus.BLOCKED.value: FAILED,
    TaskStatus.CANCELLED.value: CANCELLED,
})

STEP_OUTCOMES: Mapping[str, str] = MappingProxyType({
    ExecutionStepStatus.DONE.value: SUCCEEDED,
    ExecutionStepStatus.FAILED.value: FAILED,
    ExecutionStepStatus.CANCELLED.value: CANCELLED,
})

WORKFLOW_RUN_OUTCOMES: Mapping[str, str] = MappingProxyType({
    WorkflowRunStatus.COMPLETED.value: SUCCEEDED,
    WorkflowRunStatus.FAILED.value: FAILED,
    WorkflowRunStatus.CANCELLED.value: CANCELLED,
})

AUTOMATION_OUTCOMES: Mapping[str, str] = MappingProxyType({
    ScheduledRunStatus.SUCCESS.value: SUCCEEDED,
    ScheduledRunStatus.COMPLETED.value: SUCCEEDED,
    ScheduledRunStatus.ERROR.value: FAILED,
    ScheduledRunStatus.CANCELLED.value: CANCELLED,
})

AGENT_CHILD_JUDGED_PHASES = frozenset(TASK_AGENT_CHILD_TERMINAL_PHASES) - {
    TaskCoordinatorPhase.CANCELLED.value,
}
"""Agent-loop delegation phases a success rate judges."""


def statuses_with(outcomes: Mapping[str, str], *wanted: str) -> frozenset[str]:
    """The raw statuses that map to any of ``wanted`` — for SQL filters."""
    return frozenset(status for status, outcome in outcomes.items() if outcome in wanted)


def success_rate(succeeded: int, failed: int) -> Optional[float]:
    """Cancellations are not judged; ``None`` when nothing was judged."""
    judged = succeeded + failed
    return round(succeeded / judged, 4) if judged else None


def task_outcome(
    status: Any, *, platform_stopped: bool = False, cancel_source: Optional[str] = None,
) -> Optional[str]:
    """``None`` for a proposed Task: a suggestion nobody accepted is not work,
    and neither is one a person rejected (``cancel_source`` proposal). A Task
    the platform itself stopped counts as cancelled, not failed."""
    if status == TaskStatus.PROPOSED.value or cancel_source == CancelSource.PROPOSAL.value:
        return None
    outcome = TASK_OUTCOMES.get(status, IN_FLIGHT)
    if outcome == FAILED and platform_stopped:
        return CANCELLED
    return outcome


def step_outcome(status: Any) -> str:
    """Outcome of a plan step; anything not yet finished is in flight."""
    return STEP_OUTCOMES.get(status, IN_FLIGHT)


def workflow_run_outcome(status: Any) -> str:
    """Outcome of a Workflow run; anything not yet finished is in flight."""
    return WORKFLOW_RUN_OUTCOMES.get(status, IN_FLIGHT)


def automation_outcome(status: Any) -> Optional[str]:
    """``None`` for a skipped occurrence (e.g. the Workspace was paused)."""
    if status == ScheduledRunStatus.SKIPPED.value:
        return None
    return AUTOMATION_OUTCOMES.get(status, IN_FLIGHT)


def is_agent_step(step: Any) -> bool:
    """A plan step an agent actually worked on. Legacy coordinator fold
    roots only supervise their children; skipped steps never ran."""
    params = getattr(step, "params", None)
    return (
        bool(getattr(step, "resolved_agent_id", None))
        and not (isinstance(params, dict) and COORDINATOR_PARAM_KEY in params)
        and getattr(step, "step_status", None) != ExecutionStepStatus.SKIPPED.value
    )


def agent_child_outcome(child: Any) -> str:
    """Outcome of a Task agent-loop delegation. A child that finished but
    reported anything other than ``completed`` (blocked, partial …) did
    not deliver what it was asked for."""
    if child.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES:
        return IN_FLIGHT
    if child.phase == TaskCoordinatorPhase.CANCELLED.value:
        return CANCELLED
    if child.phase == TaskCoordinatorPhase.COMPLETED.value and child.outcome_status in (
        None, TaskCoordinatorPhase.COMPLETED.value,
    ):
        return SUCCEEDED
    return FAILED


def agent_child_succeeded_clause() -> ColumnElement[bool]:
    """SQL form of ``agent_child_outcome(child) == SUCCEEDED``."""
    return and_(
        TaskAgentChildRun.phase == TaskCoordinatorPhase.COMPLETED.value,
        or_(
            TaskAgentChildRun.outcome_status.is_(None),
            TaskAgentChildRun.outcome_status == TaskCoordinatorPhase.COMPLETED.value,
        ),
    )


def task_platform_stopped_clause() -> ColumnElement[bool]:
    """SQL form of the platform-stopped rule: the Task's latest execution
    failure log is the platform's own stop (``RUNTIME_MIGRATED_ERROR``)."""
    latest_error = (
        select(TaskLog.meta["error"].astext)
        .where(
            TaskLog.task_id == Task.id,
            TaskLog.log_type == TaskLogType.AI_EXECUTION_FAILED.value,
        )
        .order_by(TaskLog.created_at.desc(), TaskLog.id.desc())
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )
    # coalesce: a Task with no log (NULL) must be NOT stopped under ``~``.
    return func.coalesce(latest_error, "") == RUNTIME_MIGRATED_ERROR


async def platform_stopped_task_ids(db: AsyncSession, task_ids: Iterable[str]) -> set[str]:
    """The Tasks among ``task_ids`` the platform itself stopped — the
    ``platform_stopped`` input of :func:`task_outcome`."""
    ids = list(dict.fromkeys(task_ids))
    if not ids:
        return set()
    return set((await db.execute(
        select(Task.id).where(Task.id.in_(ids), task_platform_stopped_clause())
    )).scalars().all())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def task_is_stalled(task: Any, *, now: datetime) -> bool:
    if task.status != TaskStatus.IN_PROGRESS.value:
        return False
    last_change = task.status_changed_at or task.updated_at or task.created_at
    return last_change is not None and _aware(last_change) < _aware(now) - STALLED_AFTER


def stalled_task_clause(now: datetime) -> ColumnElement[bool]:
    """SQL form of :func:`task_is_stalled`."""
    return and_(
        Task.status == TaskStatus.IN_PROGRESS.value,
        func.coalesce(Task.status_changed_at, Task.updated_at, Task.created_at)
        < _aware(now) - STALLED_AFTER,
    )


def workflow_run_is_stalled(run: Any, *, now: datetime) -> bool:
    """A Workflow run still unfinished ``STALLED_AFTER`` after it began."""
    if workflow_run_outcome(run.status) != IN_FLIGHT:
        return False
    began = run.started_at or run.created_at
    return began is not None and _aware(began) < _aware(now) - STALLED_AFTER


def stalled_workflow_run_clause(now: datetime) -> ColumnElement[bool]:
    """SQL form of :func:`workflow_run_is_stalled`."""
    return and_(
        WorkflowRun.status.not_in(tuple(WORKFLOW_RUN_OUTCOMES)),
        func.coalesce(WorkflowRun.started_at, WorkflowRun.created_at)
        < _aware(now) - STALLED_AFTER,
    )


# ── Quality and acceptance ───────────────────────────────────────────

ACCEPTANCE_PASS = 0.9
"""People accept at least this share of the work they review."""
ACCEPTANCE_FAIL = 0.7
NEGATIVE_RATE_WARN = 0.2
"""Ignored reviews, retries and user cancels above this share degrade a Workspace."""
MISSED_FIRE_AFTER = timedelta(hours=1)
"""An enabled job whose next fire is this far in the past was missed."""
COVERAGE_GRACE = timedelta(days=30)
"""Unused services only count against a Workspace live for this long."""

REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"
REVIEW_IGNORED = "ignored"

APPROVED_STATUSES = frozenset({ApprovalStatus.GRANTED.value, ApprovalStatus.CONSUMED.value})
_ACCEPTED_PROPOSAL_ITEMS = frozenset({
    ProposalItemStatus.APPROVED.value, ProposalItemStatus.EXECUTING.value,
    ProposalItemStatus.SUCCEEDED.value, ProposalItemStatus.FAILED.value,
})


def review_outcome(
    *, status: Any, hitl_type: Any, decided_by_user_id: Optional[str],
) -> Optional[str]:
    """A person's verdict on the Workspace's work. Input requests ask for
    information and carry no verdict; a request closed because its work
    ended while nobody had looked is ignored."""
    if hitl_type == HitlType.INPUT.value:
        return None
    if status == ApprovalStatus.EXPIRED.value:
        return REVIEW_IGNORED
    if not decided_by_user_id:
        return None
    if status in APPROVED_STATUSES:
        return REVIEW_ACCEPTED
    if status == ApprovalStatus.DENIED.value:
        return REVIEW_REJECTED
    return None


def proposal_item_accepted(status: Any) -> Optional[bool]:
    """True once a person approved the item, False when rejected, None while
    undecided or closed without a decision."""
    if status in _ACCEPTED_PROPOSAL_ITEMS:
        return True
    if status == ProposalItemStatus.REJECTED.value:
        return False
    return None


def judged_rate(part: int, whole: int) -> Optional[float]:
    """part / whole, or None below MIN_JUDGED_RUNS — too little evidence."""
    if whole < MIN_JUDGED_RUNS:
        return None
    return round(part / whole, 4)


DURATION_BUCKET_BOUNDS_S = (10, 60, 300, 900, 3600, 21_600, 86_400)


def duration_bucket(seconds: float) -> str:
    for bound in DURATION_BUCKET_BOUNDS_S:
        if seconds <= bound:
            return f"le_{bound}"
    return f"gt_{DURATION_BUCKET_BOUNDS_S[-1]}"


TOP_ERROR_LIMIT = 3
ERROR_TEXT_LIMIT = 160


def error_text(error: Any) -> str:
    """One-line, bounded failure text for grouping top errors."""
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error.get("type") or ""
    text = " ".join(str(error).split())
    if len(text) > ERROR_TEXT_LIMIT:
        text = text[: ERROR_TEXT_LIMIT - 1] + "…"
    return text or "unknown error"


# ── Health status ────────────────────────────────────────────────────

STATUS_NOT_READY = "not_ready"
STATUS_FAILING = "failing"
STATUS_DEGRADED = "degraded"
STATUS_IDLE = "idle"
STATUS_HEALTHY = "healthy"
_STATUS_ORDER = (STATUS_NOT_READY, STATUS_FAILING, STATUS_DEGRADED, STATUS_IDLE, STATUS_HEALTHY)

_NEGATIVE_QUALITY = ("ignored_rate", "retry_rate", "user_cancel_rate")
_POSITIVE_QUALITY = ("acceptance_rate", "feedback_rate", "proposal_acceptance_rate")


@dataclass(frozen=True)
class HealthInputs:
    readiness_gaps: list[str]
    surfaces: dict[str, dict[str, Any]]
    quality: dict[str, Optional[float]]
    missed_schedules: int
    pending_approvals_over_24h: int
    service_coverage: Optional[float]
    live_since: Optional[datetime]
    stalled_over_threshold: int
    judged_runs_7d: int
    now: datetime


@dataclass(frozen=True)
class HealthStatus:
    status: str
    reasons: list[dict[str, Any]]


def derive_status(inputs: HealthInputs) -> HealthStatus:
    """The single status rule: the most severe matched rule wins and every
    matched rule is kept as a reason. Surfaces marked unknown never match."""
    matched: list[tuple[str, dict[str, Any]]] = []

    def hit(status: str, rule: str, *, surface: Optional[str] = None, value: Any = None) -> None:
        matched.append((status, {"rule": rule, "surface": surface, "value": value}))

    if inputs.readiness_gaps:
        hit(STATUS_NOT_READY, "readiness_gaps", value=len(inputs.readiness_gaps))
    for name, surface in sorted(inputs.surfaces.items()):
        rate = surface.get("success_rate_7d")
        if surface.get("unknown") or rate is None or (surface.get("judged_7d") or 0) < MIN_JUDGED_RUNS:
            continue
        if rate < SUCCESS_RATE_FAIL:
            hit(STATUS_FAILING, "success_rate_fail", surface=name, value=rate)
        elif rate < SUCCESS_RATE_PASS:
            hit(STATUS_DEGRADED, "success_rate_warn", surface=name, value=rate)
    if inputs.stalled_over_threshold:
        hit(STATUS_FAILING, "stalled_work", value=inputs.stalled_over_threshold)
    for key in _POSITIVE_QUALITY:
        rate = inputs.quality.get(key)
        if rate is None:
            continue
        rule = key.removesuffix("_rate")
        if rate < ACCEPTANCE_FAIL:
            hit(STATUS_FAILING, f"{rule}_fail", value=rate)
        elif rate < ACCEPTANCE_PASS:
            hit(STATUS_DEGRADED, f"{rule}_warn", value=rate)
    for key in _NEGATIVE_QUALITY:
        rate = inputs.quality.get(key)
        if rate is not None and rate > NEGATIVE_RATE_WARN:
            hit(STATUS_DEGRADED, key, value=rate)
    if inputs.missed_schedules:
        hit(STATUS_DEGRADED, "missed_schedules", value=inputs.missed_schedules)
    if inputs.pending_approvals_over_24h:
        hit(STATUS_DEGRADED, "approval_backlog", value=inputs.pending_approvals_over_24h)
    if (
        inputs.service_coverage is not None and inputs.service_coverage < 1
        and inputs.live_since is not None
        and _aware(inputs.now) - _aware(inputs.live_since) > COVERAGE_GRACE
    ):
        hit(STATUS_DEGRADED, "service_coverage", value=inputs.service_coverage)
    if not matched and inputs.judged_runs_7d == 0:
        hit(STATUS_IDLE, "no_runs", value=0)

    if not matched:
        return HealthStatus(STATUS_HEALTHY, [])
    matched.sort(key=lambda item: _STATUS_ORDER.index(item[0]))
    return HealthStatus(matched[0][0], [reason for _, reason in matched])
