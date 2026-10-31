"""Per-Workspace daily health roll-up (design §3.1, §9.2).

Reads one UTC day of finished work, classifies every row with the shared
definitions in ``workspace_health_metrics`` and replaces that day's rows in
``metrics_workspace_daily``. A row belongs to the day its run finished;
in-flight work is not counted. Rows reflect each run's status at roll-up
time, so re-running a day picks up later status changes.

Replacing (delete + insert) rather than upserting means a Workspace that
lost all of a day's runs on re-roll loses its row too. Same-day runs are
serialised with a transaction-scoped advisory lock.

This module only reads and counts. Whether a run succeeded, failed or was
cancelled, and whether a review accepted the work, is decided by the
classifiers in ``workspace_health_metrics`` — never here — so the daily rows
mean exactly what the launch review and the Stats library mean.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Union

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
)
from packages.core.constants.task import CancelSource
from packages.core.models.billing import CreditUsageLog
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.task_run_journal import TaskRunJournal, TaskRunJournalKind
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_health import MetricsWorkspaceDaily
from packages.core.services.chat_feedback import ChatFeedbackRating, ChatFeedbackTargetKind
from packages.core.services.workspace_health_metrics import (
    CANCELLED,
    FAILED,
    REVIEW_ACCEPTED,
    REVIEW_IGNORED,
    REVIEW_REJECTED,
    SUCCEEDED,
    TASK_OUTCOMES,
    TOP_ERROR_LIMIT,
    WORKFLOW_RUN_OUTCOMES,
    agent_child_outcome,
    automation_outcome,
    duration_bucket,
    error_text,
    is_agent_step,
    platform_stopped_task_ids,
    proposal_item_accepted,
    review_outcome,
    statuses_with,
    step_outcome,
    task_outcome,
    workflow_run_outcome,
)

SURFACE_TASK = "task"
SURFACE_AGENT = "agent"
SURFACE_WORKFLOW = "workflow"
SURFACE_AUTOMATION = "automation"
SURFACE_WORKSPACE = "workspace"
RUN_SURFACES = (SURFACE_TASK, SURFACE_AGENT, SURFACE_WORKFLOW, SURFACE_AUTOMATION)

MODE_LIVE = "live"
MODE_SIMULATION = "simulation"

RETENTION_DAYS = 180
_LOCK_NAMESPACE = 0x57484D  # "WHM"; the day's ordinal is the second key

_JUDGED = (SUCCEEDED, FAILED, CANCELLED)
_REVIEW_COLUMNS = {
    REVIEW_ACCEPTED: "reviews_accepted",
    REVIEW_REJECTED: "reviews_rejected",
    REVIEW_IGNORED: "reviews_ignored",
}


@dataclass
class _Tally:
    entity_id: Optional[str]
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    retried: int = 0
    user_cancelled: int = 0
    duration_sum_s: int = 0
    duration_count: int = 0
    buckets: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    counts: Counter = field(default_factory=Counter)
    """Quality counts and credits, keyed by column name (workspace surface)."""

    def add(
        self, outcome: str, *, started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None, duration_seconds: Optional[float] = None,
        error: Any = None, cancel_source: Optional[str] = None, retried: bool = False,
    ) -> None:
        if outcome == SUCCEEDED:
            self.succeeded += 1
            if duration_seconds is None and started_at and finished_at:
                duration_seconds = (finished_at - started_at).total_seconds()
            if duration_seconds is not None and duration_seconds >= 0:
                self.duration_sum_s += int(duration_seconds)
                self.duration_count += 1
                self.buckets[duration_bucket(duration_seconds)] += 1
        elif outcome == FAILED:
            self.failed += 1
            if error:
                self.errors[error_text(error)] += 1
        elif outcome == CANCELLED:
            self.cancelled += 1
            if cancel_source == CancelSource.USER.value:
                self.user_cancelled += 1
        if retried:
            self.retried += 1


_TallyFor = Callable[[str, Optional[str], str], _Tally]


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


async def rollup_workspace_day(
    db: AsyncSession, *, day: Union[date, str], now: Optional[datetime] = None,
) -> dict:
    """Replace ``day``'s rows and prune rows past retention. The caller commits."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    now = now or datetime.now(timezone.utc)
    start, end = _bounds(day)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _LOCK_NAMESPACE, "key": day.toordinal()},
    )
    tallies: dict[tuple[str, str], _Tally] = {}

    def tally(workspace_id: str, entity_id: Optional[str], surface: str) -> _Tally:
        key = (workspace_id, surface)
        if key not in tallies:
            tallies[key] = _Tally(entity_id=entity_id)
        elif tallies[key].entity_id is None:
            tallies[key].entity_id = entity_id
        return tallies[key]

    await _tasks(db, start, end, tally)
    await _workflow_runs(db, start, end, tally)
    await _agents(db, start, end, tally)
    await _automations(db, start, end, tally)
    await _quality(db, start, end, tally)

    dims = await _workspace_dimensions(db, {ws for ws, _ in tallies})
    await db.execute(delete(MetricsWorkspaceDaily).where(MetricsWorkspaceDaily.day == day))
    written = 0
    for (workspace_id, surface), t in sorted(tallies.items()):
        entity_id, blueprint_key, blueprint_version, mode = dims.get(
            workspace_id, (None, None, None, MODE_LIVE),
        )
        # The Workspace's own entity is authoritative; a row's entity only
        # fills in for a Workspace that no longer exists. ScheduledJob's
        # entity_id is nullable, so neither may be known: such a row cannot
        # be attributed to any account and is dropped.
        entity_id = entity_id or t.entity_id
        if not entity_id:
            continue
        db.add(MetricsWorkspaceDaily(
            day=day, workspace_id=workspace_id, surface=surface, entity_id=entity_id,
            blueprint_key=blueprint_key, blueprint_version=blueprint_version, mode=mode,
            succeeded=t.succeeded, failed=t.failed, cancelled=t.cancelled, retried=t.retried,
            user_cancelled=t.user_cancelled,
            duration_sum_s=t.duration_sum_s, duration_count=t.duration_count,
            duration_buckets=dict(t.buckets),
            top_errors=[
                {"message": message, "count": count}
                for message, count in t.errors.most_common(TOP_ERROR_LIMIT)
            ],
            **{column: int(value) for column, value in t.counts.items()},
        ))
        written += 1
    await db.flush()

    pruned = (await db.execute(delete(MetricsWorkspaceDaily).where(
        MetricsWorkspaceDaily.day < (now - timedelta(days=RETENTION_DAYS)).date()
    ))).rowcount or 0
    return {"day": day.isoformat(), "rows": written, "pruned": int(pruned)}


# ── Run surfaces ─────────────────────────────────────────────────────

_TASK_TERMINAL = statuses_with(TASK_OUTCOMES, *_JUDGED)
_WORKFLOW_TERMINAL = statuses_with(WORKFLOW_RUN_OUTCOMES, *_JUDGED)


async def _tasks(db: AsyncSession, start: datetime, end: datetime, tally: _TallyFor) -> None:
    # Task failure text is "Task <status>", not the AI_EXECUTION_FAILED log:
    # the per-task log lookup is what launch review does for one Workspace,
    # but across every Workspace for a day it is an N-row subquery for little
    # gain. Launch review still shows the real reasons.
    finished = func.coalesce(
        Task.completed_at, Task.status_changed_at, Task.updated_at, Task.created_at,
    )
    rows = (await db.execute(select(
        Task.id, Task.workspace_id, Task.entity_id, Task.status, Task.cancel_source,
        Task.started_at, finished.label("finished_at"),
    ).where(
        Task.workspace_id.is_not(None), Task.status.in_(_TASK_TERMINAL),
        finished >= start, finished < end,
    ))).all()
    stopped = await platform_stopped_task_ids(
        db, [r.id for r in rows if task_outcome(r.status) == FAILED],
    )
    for r in rows:
        outcome = task_outcome(
            r.status, platform_stopped=r.id in stopped, cancel_source=r.cancel_source,
        )
        if outcome not in _JUDGED:
            continue
        tally(r.workspace_id, r.entity_id, SURFACE_TASK).add(
            outcome, started_at=r.started_at, finished_at=r.finished_at,
            error=f"Task {r.status}" if outcome == FAILED else None,
            cancel_source=r.cancel_source,
        )
    # A Task retry reopens a stopped run; the journal records each one.
    for r in (await db.execute(select(
        TaskRunJournal.workspace_id, TaskRunJournal.entity_id, func.count().label("n"),
    ).where(
        TaskRunJournal.workspace_id.is_not(None),
        TaskRunJournal.kind == TaskRunJournalKind.RETRY.value,
        TaskRunJournal.created_at >= start, TaskRunJournal.created_at < end,
    ).group_by(TaskRunJournal.workspace_id, TaskRunJournal.entity_id))).all():
        tally(r.workspace_id, r.entity_id, SURFACE_TASK).retried += int(r.n)


async def _workflow_runs(
    db: AsyncSession, start: datetime, end: datetime, tally: _TallyFor,
) -> None:
    finished = func.coalesce(
        WorkflowRun.completed_at, WorkflowRun.updated_at, WorkflowRun.created_at,
    )
    for r in (await db.execute(select(
        WorkflowRun.workspace_id, WorkflowRun.entity_id, WorkflowRun.status, WorkflowRun.error,
        WorkflowRun.cancel_source, WorkflowRun.attempt_number, WorkflowRun.started_at,
        finished.label("finished_at"),
    ).where(
        WorkflowRun.workspace_id.is_not(None), WorkflowRun.status.in_(_WORKFLOW_TERMINAL),
        finished >= start, finished < end,
    ))).all():
        tally(r.workspace_id, r.entity_id, SURFACE_WORKFLOW).add(
            workflow_run_outcome(r.status), started_at=r.started_at, finished_at=r.finished_at,
            error=r.error, cancel_source=r.cancel_source,
            retried=(r.attempt_number or 1) > 1,
        )


async def _agents(db: AsyncSession, start: datetime, end: datetime, tally: _TallyFor) -> None:
    # Same two sources as launch review's agent section
    # (``build_agent_outcomes``): agent plan steps of the retired Plan/Step
    # engine and Task agent-loop delegations.
    for s in (await db.execute(select(
        ExecutionStep.workspace_id, ExecutionStep.entity_id, ExecutionStep.resolved_agent_id,
        ExecutionStep.params, ExecutionStep.step_status, ExecutionStep.started_at,
        ExecutionStep.finished_at, ExecutionStep.error, ExecutionStep.last_execution_error,
        ExecutionStep.attempt_count,
    ).where(
        ExecutionStep.workspace_id.is_not(None), ExecutionStep.resolved_agent_id.is_not(None),
        ExecutionStep.finished_at >= start, ExecutionStep.finished_at < end,
    ))).all():
        if not is_agent_step(s):
            continue
        outcome = step_outcome(s.step_status)
        if outcome not in _JUDGED:
            continue
        tally(s.workspace_id, s.entity_id, SURFACE_AGENT).add(
            outcome, started_at=s.started_at, finished_at=s.finished_at,
            error=s.error or s.last_execution_error, retried=(s.attempt_count or 0) > 1,
        )
    for c in (await db.execute(select(
        TaskAgentChildRun.workspace_id, TaskAgentChildRun.entity_id, TaskAgentChildRun.phase,
        TaskAgentChildRun.outcome_status, TaskAgentChildRun.continue_interrupted,
        TaskAgentChildRun.created_at, TaskAgentChildRun.terminal_at,
        TaskAgentChildRun.result["message"].astext.label("result_message"),
        TaskAgentChildRun.result["error"].astext.label("result_error"),
    ).where(
        TaskAgentChildRun.workspace_id.is_not(None),
        TaskAgentChildRun.terminal_at >= start, TaskAgentChildRun.terminal_at < end,
    ))).all():
        outcome = agent_child_outcome(c)
        if outcome not in _JUDGED:
            continue
        tally(c.workspace_id, c.entity_id, SURFACE_AGENT).add(
            outcome, started_at=c.created_at, finished_at=c.terminal_at,
            error=(
                c.result_message or c.result_error
                or f"Agent run {c.outcome_status or c.phase}"
            ) if outcome == FAILED else None,
            retried=bool(c.continue_interrupted),
        )


async def _automations(
    db: AsyncSession, start: datetime, end: datetime, tally: _TallyFor,
) -> None:
    finished = func.coalesce(ScheduledJobRun.completed_at, ScheduledJobRun.created_at)
    for r in (await db.execute(select(
        ScheduledJob.workspace_id, ScheduledJob.entity_id, ScheduledJobRun.status,
        ScheduledJobRun.error, ScheduledJobRun.duration_ms, ScheduledJobRun.started_at,
        finished.label("finished_at"),
    ).join(ScheduledJob, ScheduledJob.job_id == ScheduledJobRun.job_id).where(
        ScheduledJob.workspace_id.is_not(None), finished >= start, finished < end,
    ))).all():
        outcome = automation_outcome(r.status)
        if outcome not in _JUDGED:
            continue
        # The scheduler measures a run itself; prefer that over the stamps.
        tally(r.workspace_id, r.entity_id, SURFACE_AUTOMATION).add(
            outcome, started_at=r.started_at, finished_at=r.finished_at,
            duration_seconds=r.duration_ms / 1000.0 if r.duration_ms is not None else None,
            error=r.error,
        )


# ── Workspace surface: quality signals and credits ───────────────────

async def _quality(db: AsyncSession, start: datetime, end: datetime, tally: _TallyFor) -> None:
    decided = func.coalesce(HitlRequest.decided_at, HitlRequest.updated_at, HitlRequest.created_at)
    for r in (await db.execute(select(
        HitlRequest.workspace_id, HitlRequest.entity_id, HitlRequest.status,
        HitlRequest.hitl_type, HitlRequest.decided_by_user_id,
    ).where(
        HitlRequest.workspace_id.is_not(None), decided >= start, decided < end,
    ))).all():
        column = _REVIEW_COLUMNS.get(review_outcome(
            status=r.status, hitl_type=r.hitl_type, decided_by_user_id=r.decided_by_user_id,
        ))
        if column:
            tally(r.workspace_id, r.entity_id, SURFACE_WORKSPACE).counts[column] += 1

    # Completion-receipt thumbs. Their subject is a Task or a Plan; the
    # explicit task_id / plan_id columns say which. target_id is not used:
    # a Task receipt that also names a Plan stores the Plan id there.
    workspace_id = func.coalesce(Task.workspace_id, ExecutionPlan.workspace_id)
    for r in (await db.execute(select(
        workspace_id.label("workspace_id"), ChatMessageFeedback.entity_id,
        ChatMessageFeedback.rating,
    ).outerjoin(Task, Task.id == ChatMessageFeedback.task_id).outerjoin(
        ExecutionPlan, ExecutionPlan.id == ChatMessageFeedback.plan_id,
    ).where(
        ChatMessageFeedback.target_kind.in_((
            ChatFeedbackTargetKind.TASK_COMPLETION.value,
            ChatFeedbackTargetKind.PLAN_COMPLETION.value,
        )),
        workspace_id.is_not(None),
        ChatMessageFeedback.updated_at >= start, ChatMessageFeedback.updated_at < end,
    ))).all():
        column = "feedback_up" if r.rating == ChatFeedbackRating.UP.value else "feedback_down"
        tally(r.workspace_id, r.entity_id, SURFACE_WORKSPACE).counts[column] += 1

    for r in (await db.execute(select(
        ProposalItemRecord.workspace_id, ProposalItemRecord.entity_id, ProposalItemRecord.status,
    ).where(
        ProposalItemRecord.decided_at >= start, ProposalItemRecord.decided_at < end,
    ))).all():
        accepted = proposal_item_accepted(r.status)
        if accepted is not None:
            column = "proposals_accepted" if accepted else "proposals_rejected"
            tally(r.workspace_id, r.entity_id, SURFACE_WORKSPACE).counts[column] += 1

    for r in (await db.execute(select(
        CreditUsageLog.workspace_id, CreditUsageLog.entity_id,
        func.coalesce(func.sum(CreditUsageLog.total_credit), 0).label("credits"),
    ).where(
        CreditUsageLog.workspace_id.is_not(None),
        CreditUsageLog.created_at >= start, CreditUsageLog.created_at < end,
    ).group_by(CreditUsageLog.workspace_id, CreditUsageLog.entity_id))).all():
        tally(r.workspace_id, r.entity_id, SURFACE_WORKSPACE).counts["credits"] += int(r.credits)


# ── Workspace dimensions ─────────────────────────────────────────────

async def _workspace_dimensions(
    db: AsyncSession, workspace_ids: Iterable[str],
) -> dict[str, tuple[str, Optional[str], Optional[str], str]]:
    """{workspace_id: (entity_id, blueprint_key, blueprint_version, mode)}."""
    ids = sorted(set(workspace_ids))
    if not ids:
        return {}
    return {
        row.id: (row.entity_id, *workspace_dimensions(row))
        for row in (await db.execute(select(
            Workspace.id, Workspace.entity_id, Workspace.kind, Workspace.settings,
        ).where(Workspace.id.in_(ids)))).all()
    }


def workspace_dimensions(ws: Any) -> tuple[Optional[str], Optional[str], str]:
    """(blueprint_key, blueprint_version, mode). A Workspace is in
    simulation while it is a sandbox (``simulation_runtime._is_sandbox``)."""
    settings = ws.settings if isinstance(ws.settings, dict) else {}
    blueprint = settings.get(BLUEPRINT_SETTINGS_KEY)
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    sandbox = settings.get("sandbox") is True or ws.kind == "sandbox"
    version = blueprint.get(BLUEPRINT_VERSION_KEY)
    return (
        blueprint.get(BLUEPRINT_ID_KEY) or None,
        str(version) if version is not None else None,
        MODE_SIMULATION if sandbox else MODE_LIVE,
    )
