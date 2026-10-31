"""Launch review — "did this Workspace run well enough to go live?"

Extends the simulation report with the operational evidence an operator
needs before promoting:

  Operations   Run outcomes over the report window, one section per
               execution surface: Tasks, Agent runs, Workflow runs, and
               scheduled Automations. Each carries success rate, average
               duration, retries, a daily trend, top failure reasons and a
               per-agent / per-workflow / per-job breakdown. Success rate
               is succeeded / (succeeded + failed): a cancellation is
               nobody's failure, so unlike the ``workspace.*.success_rate``
               Stats it is reported beside the rate rather than inside it.

  Human load   Approval requests the Workspace raised: volume per day, how
               long people took, and what is still waiting. This is the
               human effort the Workspace will need once it is live.

  Readiness    Operational readiness: a list of checks and one verdict on
               whether the Workspace runs reliably, at what cost and human
               effort. Setup gaps come from the promote preflight and
               connection gaps from the Workspace connection status; the
               rest are platform-default thresholds over the sections above.

  Value        Value evidence, judged apart from readiness because running
               reliably is not the same as being worth it: goal pace (the
               Goals' own ``pace_status``) and how often people accept the
               work they review.

Both are indicators only. Neither decides a release, and the Promote action
keeps its own gate.

Queries read only the columns the review needs: run rows carry large JSON
(traces, snapshots, checkpoints) that would make the report slow.

Read-only — no side effects.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.task import TaskLogType
from packages.core.models.billing import CreditUsageLog
from packages.core.models.execution import ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_agent_child import (
    TaskAgentChildRun,
)
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Agent, Workspace
from packages.core.services.scheduler_service import (
    SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD,
)
from packages.core.services.workspace_health_metrics import (
    ACCEPTANCE_FAIL,
    ACCEPTANCE_PASS,
    APPROVAL_BACKLOG_AFTER,
    APPROVED_STATUSES,
    CANCELLED,
    FAILED,
    IN_FLIGHT,
    MIN_JUDGED_RUNS,
    REVIEW_ACCEPTED,
    REVIEW_REJECTED,
    STALLED_AFTER,
    SUCCEEDED,
    SUCCESS_RATE_FAIL,
    SUCCESS_RATE_PASS,
    TOP_ERROR_LIMIT,
    agent_child_outcome,
    automation_outcome,
    error_text,
    is_agent_step,
    platform_stopped_task_ids,
    review_outcome,
    step_outcome,
    success_rate,
    task_is_stalled,
    task_outcome,
    workflow_run_is_stalled,
    workflow_run_outcome,
)

# ── Platform-default launch thresholds ────────────────────────────────

# Outcome vocabulary and the success-rate thresholds live in
# services.workspace_health_metrics, shared with Stats and the admin metrics.
MIN_OBSERVATION_DAYS = 1.0
APPROVAL_WAIT_WARN = APPROVAL_BACKLOG_AFTER
"""An approval left pending this long means nobody is watching the queue."""
BUDGET_WARN_FRACTION = 0.8

BREAKDOWN_LIMIT = 10
DAILY_TREND_LIMIT = 30

PASS = "pass"
WARN = "warn"
FAIL = "fail"
NOT_APPLICABLE = "na"

VERDICT_READY = "ready"
VERDICT_AT_RISK = "at_risk"
VERDICT_BLOCKED = "blocked"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"

VALUE_ON_TRACK = "on_track"
VALUE_CONCERNS = "concerns"
VALUE_BELOW_EXPECTATIONS = "below_expectations"
VALUE_UNPROVEN = "unproven"

# Goal.pace_status vocabulary (goals.pace.compute_pace).
_GOOD_PACE = {"achieved", "ahead", "on_track"}


# ── DTOs ──────────────────────────────────────────────────────────────

@dataclass
class ErrorCount:
    message: str
    count: int


@dataclass
class RunBreakdown:
    key: str
    label: str
    total: int
    succeeded: int
    failed: int
    success_rate: Optional[float]
    judged: int = 0
    """succeeded + failed — the runs the rate is computed over."""


@dataclass
class DailyOutcome:
    day: str
    """ISO date (UTC)."""
    succeeded: int = 0
    failed: int = 0


@dataclass
class RunOutcomes:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    in_flight: int = 0
    retried: int = 0
    """Runs that needed more than one attempt — flaky even when they
    eventually succeeded."""
    success_rate: Optional[float] = None
    """succeeded / (succeeded + failed); None when nothing was judged."""
    avg_duration_seconds: Optional[float] = None
    top_errors: list[ErrorCount] = field(default_factory=list)
    breakdown: list[RunBreakdown] = field(default_factory=list)
    below_target: list[RunBreakdown] = field(default_factory=list)
    """Every agent / workflow / job with enough finished runs to judge whose
    own success rate misses the target, worst first. A healthy aggregate
    can hide one broken workflow; these are judged on their own."""
    daily: list[DailyOutcome] = field(default_factory=list)
    """Finished runs per UTC day across the window, oldest first."""

    @property
    def finished(self) -> int:
        return self.succeeded + self.failed + self.cancelled

    @property
    def judged(self) -> int:
        return self.succeeded + self.failed


@dataclass
class HumanLoad:
    requested: int = 0
    approved: int = 0
    denied: int = 0
    expired: int = 0
    """Closed without a decision, e.g. the originating work ended."""
    pending: int = 0
    per_day: float = 0.0
    median_wait_minutes: Optional[float] = None
    """Request → human decision, over requests a person decided."""
    oldest_pending_hours: Optional[float] = None
    accepted: int = 0
    """Review / authorize requests a person approved."""
    rejected: int = 0
    """Review / authorize requests a person denied."""

    @property
    def acceptance_rate(self) -> Optional[float]:
        reviewed = self.accepted + self.rejected
        return round(self.accepted / reviewed, 4) if reviewed else None


@dataclass
class OperationsSection:
    tasks: RunOutcomes
    agents: RunOutcomes
    workflows: RunOutcomes
    automations: RunOutcomes
    human: HumanLoad = field(default_factory=HumanLoad)
    stalled_tasks: int = 0
    """Tasks in progress without a status change for 48h."""
    stalled_workflow_runs: int = 0
    """Workflow runs started over 48h ago that are still not finished."""
    auto_paused_automations: list[str] = field(default_factory=list)
    """Jobs the scheduler disabled after repeated failures."""
    failing_automations: list[str] = field(default_factory=list)
    """Enabled jobs whose latest run failed."""

    @property
    def finished_work(self) -> int:
        return sum(
            section.finished
            for section in (self.tasks, self.agents, self.workflows, self.automations)
        )

    @property
    def judged_work(self) -> int:
        """Succeeded + failed runs across every surface — the evidence a
        verdict rests on."""
        return sum(
            section.judged
            for section in (self.tasks, self.agents, self.workflows, self.automations)
        )

    @property
    def successful_outcomes(self) -> int:
        """Delivered units of work. Agent runs are parts of tasks and
        automations mostly spawn tasks or workflow runs, so only tasks and
        workflow runs count — otherwise the same work is counted twice."""
        return self.tasks.succeeded + self.workflows.succeeded


@dataclass
class ConnectionIssue:
    label: str
    reason: str
    required: bool


@dataclass
class LaunchCheck:
    key: str
    status: str
    """pass | warn | fail | na"""
    reason: str
    """Stable code the UI localises; ``values`` fill its placeholders."""
    values: dict[str, Any] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    """Free-text items, e.g. the unmet promote requirements."""


@dataclass
class LaunchReadiness:
    verdict: str
    """ready | at_risk | blocked | insufficient_data"""
    checks: list[LaunchCheck]


@dataclass
class GoalSnapshot:
    title: str
    pace_status: Optional[str]
    measured: bool


@dataclass
class ValueEvidence:
    verdict: str
    """on_track | concerns | below_expectations | unproven"""
    checks: list[LaunchCheck]


# ── Outcome accumulation ──────────────────────────────────────────────

class _OutcomeTally:
    """Folds individual runs into one RunOutcomes section."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._durations: list[float] = []
        self._errors: Counter[str] = Counter()
        self._groups: dict[str, Counter[str]] = {}
        self._daily: dict[date, Counter[str]] = {}
        self._retried = 0

    def add(
        self,
        outcome: str,
        *,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        happened_at: Optional[datetime] = None,
        duration_seconds: Optional[float] = None,
        error: Any = None,
        group: Optional[str] = None,
        retried: bool = False,
    ) -> None:
        self._counts[outcome] += 1
        if retried:
            self._retried += 1
        if duration_seconds is None and started_at and finished_at:
            duration_seconds = (finished_at - started_at).total_seconds()
        if outcome == SUCCEEDED and duration_seconds is not None and duration_seconds >= 0:
            self._durations.append(duration_seconds)
        if outcome == FAILED and error:
            self._errors[error_text(error)] += 1
        if group:
            self._groups.setdefault(group, Counter())[outcome] += 1
        when = finished_at or happened_at or started_at
        if outcome in (SUCCEEDED, FAILED) and when is not None:
            self._daily.setdefault(_utc_day(when), Counter())[outcome] += 1

    def build(
        self,
        labels: Optional[dict[str, str]] = None,
        *,
        days: Iterable[date] = (),
    ) -> RunOutcomes:
        succeeded = self._counts[SUCCEEDED]
        failed = self._counts[FAILED]
        cancelled = self._counts[CANCELLED]
        breakdown = [
            RunBreakdown(
                key=key,
                label=(labels or {}).get(key) or key,
                total=sum(counts.values()),
                succeeded=counts[SUCCEEDED],
                failed=counts[FAILED],
                success_rate=success_rate(counts[SUCCEEDED], counts[FAILED]),
                judged=counts[SUCCEEDED] + counts[FAILED],
            )
            for key, counts in self._groups.items()
        ]
        breakdown.sort(key=lambda row: (-row.failed, -row.total, row.label))
        below_target = sorted(
            (
                row for row in breakdown
                if row.judged >= MIN_JUDGED_RUNS
                and (row.success_rate or 0.0) < SUCCESS_RATE_PASS
            ),
            key=lambda row: (row.success_rate or 0.0, row.label),
        )
        return RunOutcomes(
            total=sum(self._counts.values()),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            in_flight=self._counts[IN_FLIGHT],
            retried=self._retried,
            success_rate=success_rate(succeeded, failed),
            avg_duration_seconds=(
                round(sum(self._durations) / len(self._durations), 1)
                if self._durations else None
            ),
            top_errors=[
                ErrorCount(message=message, count=count)
                for message, count in self._errors.most_common(TOP_ERROR_LIMIT)
            ],
            breakdown=breakdown[:BREAKDOWN_LIMIT],
            below_target=below_target,
            daily=[
                DailyOutcome(
                    day=day.isoformat(),
                    succeeded=self._daily.get(day, Counter())[SUCCEEDED],
                    failed=self._daily.get(day, Counter())[FAILED],
                )
                for day in days
            ],
        )


def _utc_day(value: datetime) -> date:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.date()


def window_days(window_start: datetime, window_end: datetime) -> list[date]:
    """The UTC days of the window, capped to the most recent ones."""
    first, last = _utc_day(window_start), _utc_day(window_end)
    count = min((last - first).days + 1, DAILY_TREND_LIMIT)
    return [last - timedelta(days=offset) for offset in reversed(range(max(count, 1)))]



# ── Surface builders ─────────────────────────────────────────────────

def build_task_outcomes(
    tasks: Iterable[Any],
    *,
    now: datetime,
    failure_reasons: Optional[dict[str, str]] = None,
    platform_stopped: Iterable[str] = (),
    days: Iterable[date] = (),
) -> tuple[RunOutcomes, int]:
    """Task outcomes plus the count of stalled in-progress tasks. Proposed
    tasks are suggestions nobody accepted yet, so they are not work; Tasks
    the platform itself stopped count as cancelled, not failed."""
    tally = _OutcomeTally()
    stalled = 0
    stopped = set(platform_stopped)
    for task in tasks:
        outcome = task_outcome(
            task.status, platform_stopped=task.id in stopped,
            cancel_source=getattr(task, "cancel_source", None),
        )
        if outcome is None:
            continue
        tally.add(
            outcome,
            started_at=task.started_at,
            finished_at=task.completed_at,
            happened_at=task.status_changed_at or task.updated_at,
            error=(failure_reasons or {}).get(task.id) or (
                f"Task {task.status}" if outcome == FAILED else None
            ),
        )
        if task_is_stalled(task, now=now):
            stalled += 1
    return tally.build(days=days), stalled


def _child_error(child: Any) -> Optional[str]:
    message = getattr(child, "result_message", None) or getattr(child, "result_error", None)
    result = getattr(child, "result", None)
    if not message and isinstance(result, dict):
        message = result.get("message") or result.get("error")
    return message


def build_agent_outcomes(
    steps: Iterable[ExecutionStep],
    agent_names: dict[str, str],
    *,
    child_runs: Iterable[Any] = (),
    days: Iterable[date] = (),
) -> RunOutcomes:
    """Agent work from both Task engines: delegated child runs of the Task
    agent loop, and the agent-resolved steps of the retired Plan/Step
    engine (still present in older simulations)."""
    tally = _OutcomeTally()
    for step in steps:
        if not is_agent_step(step):
            continue
        tally.add(
            step_outcome(step.step_status),
            started_at=step.started_at,
            finished_at=step.finished_at,
            error=step.error or step.last_execution_error,
            group=step.resolved_agent_id,
            retried=(step.attempt_count or 0) > 1,
        )
    for child in child_runs:
        outcome = agent_child_outcome(child)
        tally.add(
            outcome,
            started_at=child.created_at,
            finished_at=child.terminal_at,
            happened_at=child.updated_at,
            error=_child_error(child) or (
                f"Agent run {child.outcome_status or child.phase}" if outcome == FAILED else None
            ),
            group=child.agent_id,
            retried=bool(child.continue_interrupted),
        )
    return tally.build(agent_names, days=days)


def build_workflow_outcomes(
    runs: Iterable[Any],
    workflow_names: dict[str, str],
    *,
    now: datetime,
    days: Iterable[date] = (),
) -> tuple[RunOutcomes, int]:
    """Workflow outcomes plus runs still unfinished 48h after they began."""
    tally = _OutcomeTally()
    stalled = 0
    for run in runs:
        outcome = workflow_run_outcome(run.status)
        tally.add(
            outcome,
            started_at=run.started_at,
            finished_at=run.completed_at,
            happened_at=run.updated_at or run.created_at,
            error=run.error,
            group=run.workflow_id,
            retried=(run.attempt_number or 1) > 1,
        )
        if workflow_run_is_stalled(run, now=now):
            stalled += 1
    return tally.build(workflow_names, days=days), stalled


def build_automation_outcomes(
    runs: Iterable[Any],
    job_names: dict[str, str],
    *,
    days: Iterable[date] = (),
) -> RunOutcomes:
    """Skipped occurrences (e.g. the Workspace was paused) did no work."""
    tally = _OutcomeTally()
    for run in runs:
        outcome = automation_outcome(run.status)
        if outcome is None:
            continue
        tally.add(
            outcome,
            started_at=run.started_at,
            finished_at=run.completed_at,
            happened_at=run.created_at,
            duration_seconds=(
                run.duration_ms / 1000.0 if run.duration_ms is not None else None
            ),
            error=run.error,
            group=run.job_id,
        )
    return tally.build(job_names, days=days)


def build_human_load(
    requests: Iterable[Any], *, now: datetime, observation_days: float,
) -> HumanLoad:
    load = HumanLoad()
    waits: list[float] = []
    oldest_pending: Optional[datetime] = None
    for request in requests:
        load.requested += 1
        if request.status in APPROVED_STATUSES:
            load.approved += 1
        elif request.status == ApprovalStatus.DENIED.value:
            load.denied += 1
        elif request.status == ApprovalStatus.PENDING.value:
            load.pending += 1
            if oldest_pending is None or request.created_at < oldest_pending:
                oldest_pending = request.created_at
        else:
            load.expired += 1
        if request.decided_by_user_id and request.decided_at and request.created_at:
            waits.append((request.decided_at - request.created_at).total_seconds() / 60)
        verdict = review_outcome(
            status=request.status,
            hitl_type=getattr(request, "hitl_type", None),
            decided_by_user_id=request.decided_by_user_id,
        )
        if verdict == REVIEW_ACCEPTED:
            load.accepted += 1
        elif verdict == REVIEW_REJECTED:
            load.rejected += 1
    if observation_days > 0:
        load.per_day = round(load.requested / max(observation_days, 1.0), 1)
    if waits:
        load.median_wait_minutes = round(statistics.median(waits), 1)
    if oldest_pending is not None:
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
        load.oldest_pending_hours = round((now - oldest_pending).total_seconds() / 3600, 1)
    return load


async def build_operations(
    db: AsyncSession,
    ws: Workspace,
    steps: list[ExecutionStep],
    window_start: datetime,
    window_end: datetime,
    *,
    now: Optional[datetime] = None,
) -> OperationsSection:
    now = now or datetime.now(timezone.utc)
    in_window = (window_start, window_end)
    days = window_days(window_start, window_end)
    observation_days = max((window_end - window_start).total_seconds() / 86_400.0, 0.0)

    tasks = (await db.execute(
        select(
            Task.id, Task.status, Task.started_at, Task.completed_at,
            Task.status_changed_at, Task.updated_at, Task.created_at,
            Task.cancel_source,
        ).where(
            Task.entity_id == ws.entity_id,
            Task.workspace_id == ws.id,
            Task.created_at.between(*in_window),
        )
    )).all()
    failed_task_ids = [
        task.id for task in tasks if task_outcome(task.status) == FAILED
    ]
    failure_reasons: dict[str, str] = {}
    platform_stopped = await platform_stopped_task_ids(db, failed_task_ids)
    if failed_task_ids:
        # The latest execution-failure log per task — the same source the
        # Strategist's execution snapshot reads.
        for row in (await db.execute(
            select(TaskLog.task_id, TaskLog.content)
            .where(
                TaskLog.task_id.in_(failed_task_ids),
                TaskLog.log_type == TaskLogType.AI_EXECUTION_FAILED.value,
            )
            .distinct(TaskLog.task_id)
            .order_by(TaskLog.task_id, TaskLog.created_at.desc(), TaskLog.id.desc())
        )).all():
            if row.content and row.task_id not in platform_stopped:
                failure_reasons[row.task_id] = row.content
    task_outcomes, stalled_tasks = build_task_outcomes(
        tasks, now=now, failure_reasons=failure_reasons,
        platform_stopped=platform_stopped, days=days,
    )

    child_runs = (await db.execute(
        select(
            TaskAgentChildRun.agent_id, TaskAgentChildRun.agent_label,
            TaskAgentChildRun.phase, TaskAgentChildRun.outcome_status,
            TaskAgentChildRun.continue_interrupted, TaskAgentChildRun.created_at,
            TaskAgentChildRun.terminal_at, TaskAgentChildRun.updated_at,
            TaskAgentChildRun.result["message"].astext.label("result_message"),
            TaskAgentChildRun.result["error"].astext.label("result_error"),
        ).where(
            TaskAgentChildRun.entity_id == ws.entity_id,
            TaskAgentChildRun.workspace_id == ws.id,
            TaskAgentChildRun.created_at.between(*in_window),
        )
    )).all()
    agent_ids = {step.resolved_agent_id for step in steps if is_agent_step(step)}
    agent_ids |= {child.agent_id for child in child_runs}
    agent_names: dict[str, str] = {
        child.agent_id: child.agent_label for child in child_runs if child.agent_label
    }
    if agent_ids:
        agent_names.update({
            row.id: row.name
            for row in (await db.execute(
                select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
            )).all()
        })

    workflow_runs = (await db.execute(
        select(
            WorkflowRun.workflow_id, WorkflowRun.status, WorkflowRun.error,
            WorkflowRun.attempt_number, WorkflowRun.started_at,
            WorkflowRun.completed_at, WorkflowRun.created_at, WorkflowRun.updated_at,
        ).where(
            WorkflowRun.entity_id == ws.entity_id,
            WorkflowRun.workspace_id == ws.id,
            WorkflowRun.created_at.between(*in_window),
        )
    )).all()
    workflow_ids = {run.workflow_id for run in workflow_runs}
    workflow_names: dict[str, str] = {}
    if workflow_ids:
        workflow_names = {
            row.id: row.name
            for row in (await db.execute(
                select(WorkflowDefinition.id, WorkflowDefinition.name)
                .where(WorkflowDefinition.id.in_(workflow_ids))
            )).all()
        }
    workflow_outcomes, stalled_workflow_runs = build_workflow_outcomes(
        workflow_runs, workflow_names, now=now, days=days,
    )

    jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == ws.entity_id,
            ScheduledJob.workspace_id == ws.id,
        )
    )).scalars().all())
    job_runs: list[Any] = []
    if jobs:
        job_runs = list((await db.execute(
            select(
                ScheduledJobRun.job_id, ScheduledJobRun.status, ScheduledJobRun.error,
                ScheduledJobRun.duration_ms, ScheduledJobRun.started_at,
                ScheduledJobRun.completed_at, ScheduledJobRun.created_at,
            ).where(
                ScheduledJobRun.job_id.in_([job.job_id for job in jobs]),
                ScheduledJobRun.created_at.between(*in_window),
            )
        )).all())

    approvals = (await db.execute(
        select(
            HitlRequest.status, HitlRequest.hitl_type, HitlRequest.created_at,
            HitlRequest.decided_at, HitlRequest.decided_by_user_id,
        ).where(
            HitlRequest.entity_id == ws.entity_id,
            HitlRequest.workspace_id == ws.id,
            HitlRequest.created_at.between(*in_window),
        )
    )).all()

    return OperationsSection(
        tasks=task_outcomes,
        agents=build_agent_outcomes(steps, agent_names, child_runs=child_runs, days=days),
        workflows=workflow_outcomes,
        automations=build_automation_outcomes(
            job_runs, {job.job_id: job.name or job.job_id for job in jobs}, days=days,
        ),
        human=build_human_load(approvals, now=now, observation_days=observation_days),
        stalled_tasks=stalled_tasks,
        stalled_workflow_runs=stalled_workflow_runs,
        auto_paused_automations=sorted(
            job.name or job.job_id for job in jobs
            if not job.enabled
            and (job.consecutive_errors or 0) >= SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD
        ),
        failing_automations=sorted(
            job.name or job.job_id for job in jobs
            if job.enabled and (job.consecutive_errors or 0) > 0
        ),
    )


async def billed_credits_in_window(
    db: AsyncSession, ws: Workspace, window_start: datetime, window_end: datetime,
) -> int:
    """Credits actually charged to this Workspace — the billing ledger, not
    the per-step cost estimates."""
    return int((await db.execute(
        select(func.coalesce(func.sum(CreditUsageLog.total_credit), 0)).where(
            CreditUsageLog.entity_id == ws.entity_id,
            CreditUsageLog.workspace_id == ws.id,
            CreditUsageLog.created_at.between(window_start, window_end),
        )
    )).scalar_one() or 0)


def connection_issues(connection_status: dict[str, Any]) -> list[ConnectionIssue]:
    """Integration gaps from the Workspace connection status. Channels and
    browser sessions are already part of the promote preflight."""
    return [
        ConnectionIssue(
            label=str(item.get("label") or item.get("provider") or "integration"),
            reason=str(item.get("reason") or ""),
            required=bool(item.get("required")),
        )
        for item in connection_status.get("requirements") or []
        if item.get("kind") == "integration" and not item.get("ready")
    ]


# ── Launch checks ────────────────────────────────────────────────────

def _success_check(key: str, outcomes: RunOutcomes) -> LaunchCheck:
    values = {
        "succeeded": outcomes.succeeded,
        "finished": outcomes.judged,
        "failed": outcomes.failed,
        "cancelled": outcomes.cancelled,
        "rate": round((outcomes.success_rate or 0) * 100),
        "target": round(SUCCESS_RATE_PASS * 100),
        "minimum": MIN_JUDGED_RUNS,
    }
    if outcomes.judged == 0:
        return LaunchCheck(key=key, status=NOT_APPLICABLE, reason="no_runs", values=values)
    if outcomes.judged < MIN_JUDGED_RUNS:
        return LaunchCheck(key=key, status=WARN, reason="too_few_runs", values=values)
    rate = outcomes.success_rate or 0.0
    if rate >= SUCCESS_RATE_PASS:
        return LaunchCheck(key=key, status=PASS, reason="on_target", values=values)
    status = WARN if rate >= SUCCESS_RATE_FAIL else FAIL
    return LaunchCheck(key=key, status=status, reason="below_target", values=values)


def _weak_items_check(operations: OperationsSection) -> LaunchCheck:
    """Each agent, workflow and automation judged on its own runs."""
    weak = sorted(
        (
            row for surface in (operations.agents, operations.workflows, operations.automations)
            for row in surface.below_target
        ),
        key=lambda row: (row.success_rate or 0.0, row.label),
    )
    values = {
        "count": len(weak),
        "target": round(SUCCESS_RATE_PASS * 100),
        "minimum": MIN_JUDGED_RUNS,
    }
    details = [
        f"{row.label} — {round((row.success_rate or 0.0) * 100)}% ({row.succeeded}/{row.judged})"
        for row in weak
    ]
    if any((row.success_rate or 0.0) < SUCCESS_RATE_FAIL for row in weak):
        return LaunchCheck(key="item_success", status=FAIL, reason="items_failing", values=values, details=details)
    if weak:
        return LaunchCheck(key="item_success", status=WARN, reason="items_below_target", values=values, details=details)
    return LaunchCheck(key="item_success", status=PASS, reason="items_on_target", values=values)


def _issue_text(issue: ConnectionIssue) -> str:
    return f"{issue.label}: {issue.reason}" if issue.reason else issue.label


def build_launch_readiness(
    *,
    unmet_setup: list[str],
    operations: OperationsSection,
    observation_days: float,
    projected_monthly_usd: float,
    monthly_budget_usd: Optional[float],
    connections: Optional[list[ConnectionIssue]] = None,
) -> LaunchReadiness:
    """``connections`` is None when the connection status could not be
    evaluated (no user to resolve accounts for, or the lookup failed).

    Verdict precedence: missing setup or a required integration that is
    down blocks normal work; then no finished work is too little evidence;
    then any failed check puts the launch at risk. Warnings never change
    the verdict."""
    checks: list[LaunchCheck] = []

    checks.append(LaunchCheck(
        key="setup",
        status=FAIL if unmet_setup else PASS,
        reason="unmet" if unmet_setup else "complete",
        values={"count": len(unmet_setup)},
        details=list(unmet_setup),
    ))

    if connections is None:
        checks.append(LaunchCheck(key="connections", status=NOT_APPLICABLE, reason="unavailable"))
    else:
        required = [issue for issue in connections if issue.required]
        optional = [issue for issue in connections if not issue.required]
        if required:
            checks.append(LaunchCheck(
                key="connections", status=FAIL, reason="required_disconnected",
                values={"count": len(required)},
                details=[_issue_text(issue) for issue in required],
            ))
        elif optional:
            checks.append(LaunchCheck(
                key="connections", status=WARN, reason="optional_disconnected",
                values={"count": len(optional)},
                details=[_issue_text(issue) for issue in optional],
            ))
        else:
            checks.append(LaunchCheck(key="connections", status=PASS, reason="connected"))

    # One finished run cannot show a Workspace is ready; the verdict needs
    # as many judged runs as a single rate does.
    judged = operations.judged_work
    enough_evidence = judged >= MIN_JUDGED_RUNS
    checks.append(LaunchCheck(
        key="evidence",
        status=(
            FAIL if judged == 0
            else WARN if not enough_evidence or observation_days < MIN_OBSERVATION_DAYS
            else PASS
        ),
        reason=(
            "no_finished_work" if judged == 0
            else "too_little_work" if not enough_evidence
            else "short_window" if observation_days < MIN_OBSERVATION_DAYS
            else "sufficient"
        ),
        values={"finished": judged, "days": round(observation_days, 1), "minimum": MIN_JUDGED_RUNS},
    ))

    checks.append(_success_check("tasks", operations.tasks))
    checks.append(_success_check("agents", operations.agents))
    checks.append(_success_check("workflows", operations.workflows))
    checks.append(_success_check("automations", operations.automations))
    checks.append(_weak_items_check(operations))

    if operations.auto_paused_automations:
        checks.append(LaunchCheck(
            key="automation_health", status=FAIL, reason="auto_paused",
            values={"count": len(operations.auto_paused_automations)},
            details=list(operations.auto_paused_automations),
        ))
    elif operations.failing_automations:
        checks.append(LaunchCheck(
            key="automation_health", status=WARN, reason="failing",
            values={"count": len(operations.failing_automations)},
            details=list(operations.failing_automations),
        ))
    else:
        checks.append(LaunchCheck(key="automation_health", status=PASS, reason="healthy"))

    stalled = operations.stalled_tasks + operations.stalled_workflow_runs
    checks.append(LaunchCheck(
        key="stalled_work",
        status=WARN if stalled else PASS,
        reason="stalled" if stalled else "none",
        values={
            "tasks": operations.stalled_tasks,
            "workflow_runs": operations.stalled_workflow_runs,
            "hours": int(STALLED_AFTER.total_seconds() // 3600),
        },
    ))

    human = operations.human
    human_values = {
        "requested": human.requested,
        "per_day": human.per_day,
        "pending": human.pending,
        "oldest_hours": human.oldest_pending_hours,
        "median_minutes": human.median_wait_minutes,
        "limit_hours": int(APPROVAL_WAIT_WARN.total_seconds() // 3600),
    }
    if human.requested == 0:
        checks.append(LaunchCheck(
            key="human_load", status=NOT_APPLICABLE, reason="no_approvals", values=human_values,
        ))
    elif (
        human.oldest_pending_hours is not None
        and human.oldest_pending_hours * 3600 >= APPROVAL_WAIT_WARN.total_seconds()
    ):
        checks.append(LaunchCheck(
            key="human_load", status=WARN, reason="approval_backlog", values=human_values,
        ))
    elif human.median_wait_minutes is None:
        checks.append(LaunchCheck(
            key="human_load", status=PASS, reason="approvals_waiting", values=human_values,
        ))
    else:
        checks.append(LaunchCheck(
            key="human_load", status=PASS, reason="approvals_handled", values=human_values,
        ))

    budget_values = {
        "projected_usd": round(projected_monthly_usd, 2),
        "budget_usd": round(monthly_budget_usd, 2) if monthly_budget_usd else None,
        "days": round(observation_days, 1),
    }
    if observation_days < MIN_OBSERVATION_DAYS:
        # Minutes of spend extrapolated to a month say nothing reliable.
        checks.append(LaunchCheck(
            key="budget", status=WARN, reason="too_early_to_project", values=budget_values,
        ))
    elif not monthly_budget_usd:
        checks.append(LaunchCheck(
            key="budget", status=WARN, reason="no_budget", values=budget_values,
        ))
    elif projected_monthly_usd > monthly_budget_usd:
        checks.append(LaunchCheck(
            key="budget", status=FAIL, reason="over_budget", values=budget_values,
        ))
    elif projected_monthly_usd > monthly_budget_usd * BUDGET_WARN_FRACTION:
        checks.append(LaunchCheck(
            key="budget", status=WARN, reason="near_budget", values=budget_values,
        ))
    else:
        checks.append(LaunchCheck(
            key="budget", status=PASS, reason="within_budget", values=budget_values,
        ))

    required_down = bool(connections and any(issue.required for issue in connections))
    if unmet_setup or required_down:
        verdict = VERDICT_BLOCKED
    elif not enough_evidence:
        verdict = VERDICT_INSUFFICIENT_DATA
    elif any(check.status == FAIL for check in checks):
        verdict = VERDICT_AT_RISK
    else:
        verdict = VERDICT_READY
    return LaunchReadiness(verdict=verdict, checks=checks)


# ── Value evidence ───────────────────────────────────────────────────

def _goal_check(goals: list[GoalSnapshot]) -> LaunchCheck:
    at_risk = [goal for goal in goals if goal.pace_status == "at_risk"]
    behind = [goal for goal in goals if goal.pace_status == "behind"]
    good = [goal for goal in goals if goal.pace_status in _GOOD_PACE]
    values = {
        "defined": len(goals),
        "measured": sum(1 for goal in goals if goal.measured),
        "on_pace": len(good),
        "behind": len(behind),
        "at_risk": len(at_risk),
    }
    details = [goal.title for goal in at_risk + behind]
    if not goals:
        return LaunchCheck(key="goal_progress", status=WARN, reason="no_goals", values=values)
    if at_risk:
        return LaunchCheck(key="goal_progress", status=FAIL, reason="goals_at_risk", values=values, details=details)
    if behind:
        return LaunchCheck(key="goal_progress", status=WARN, reason="goals_behind", values=values, details=details)
    if not good:
        # Defined but never measured, or without the baseline / deadline a
        # pace needs: the goals cannot speak to value yet.
        reason = "goals_unmeasured" if not values["measured"] else "goals_unpaced"
        return LaunchCheck(key="goal_progress", status=WARN, reason=reason, values=values)
    return LaunchCheck(key="goal_progress", status=PASS, reason="goals_on_pace", values=values)


def _acceptance_check(human: HumanLoad) -> LaunchCheck:
    reviewed = human.accepted + human.rejected
    values = {
        "accepted": human.accepted,
        "reviewed": reviewed,
        "rate": round((human.acceptance_rate or 0) * 100),
        "target": round(ACCEPTANCE_PASS * 100),
        "minimum": MIN_JUDGED_RUNS,
    }
    if reviewed == 0:
        return LaunchCheck(key="output_acceptance", status=WARN, reason="no_reviews", values=values)
    if reviewed < MIN_JUDGED_RUNS:
        return LaunchCheck(key="output_acceptance", status=WARN, reason="too_few_reviews", values=values)
    rate = human.acceptance_rate or 0.0
    if rate >= ACCEPTANCE_PASS:
        return LaunchCheck(key="output_acceptance", status=PASS, reason="mostly_accepted", values=values)
    status = WARN if rate >= ACCEPTANCE_FAIL else FAIL
    return LaunchCheck(key="output_acceptance", status=status, reason="often_rejected", values=values)


_NO_VALUE_SIGNAL = {"no_goals", "goals_unmeasured", "goals_unpaced", "no_reviews", "too_few_reviews"}


def build_value_evidence(goals: list[GoalSnapshot], human: HumanLoad) -> ValueEvidence:
    """Whether the Workspace delivers what it is for, apart from whether it
    runs. Unproven when neither goals nor reviews give a signal."""
    checks = [_goal_check(goals), _acceptance_check(human)]
    if any(check.status == FAIL for check in checks):
        verdict = VALUE_BELOW_EXPECTATIONS
    elif all(check.reason in _NO_VALUE_SIGNAL for check in checks):
        verdict = VALUE_UNPROVEN
    elif any(
        check.status == WARN and check.reason not in _NO_VALUE_SIGNAL for check in checks
    ):
        # A missing signal is not a concern; a goal falling behind or
        # output rejected more than it should be is.
        verdict = VALUE_CONCERNS
    else:
        verdict = VALUE_ON_TRACK
    return ValueEvidence(verdict=verdict, checks=checks)
