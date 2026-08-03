"""Workspace event ledger — adapters (M1 wave 3).

Focused helpers that map existing domain objects (Task, ScheduledJob,
WorkflowRun, HitlRequest, Goal, strategist proposals) onto
``workspace_events`` rows via :func:`packages.core.ledger.service.record_event`.

Design rules:

* ALL field-mapping logic lives here — call sites stay 1–3 line diffs.
* Ledger writes are best-effort and NEVER fatal for the host flow: every
  public adapter swallows its own exceptions (logged at ``warning``), so a
  ledger bug can not break task execution, approvals, or scheduling.
  ``record_event`` already savepoints duplicate-key inserts; the wrapper here
  additionally absorbs ``ValueError`` / programming errors.
* Only workspace-scoped facts are recorded: any adapter whose subject has no
  ``workspace_id`` (entity-level tasks/jobs/approvals/goals) is a silent no-op.
* Same-session discipline: adapters only ever call ``record_event`` on the
  caller's session — no commits, no second connections, no LLM/network calls.
"""
from __future__ import annotations

import functools
import hashlib
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from packages.core.ledger import event_types as et
from packages.core.ledger.service import record_event

logger = logging.getLogger(__name__)


async def _safe(coro: Awaitable[Any]) -> Any:
    """Await ``coro`` and swallow (log) any exception — ledger writes are
    best-effort and must never break the business operation."""
    try:
        return await coro
    except Exception:  # noqa: BLE001 — deliberately catch-all: never fatal
        logger.warning("workspace_events adapter write failed (ignored)", exc_info=True)
        return None


def _never_fatal(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorator form of :func:`_safe` for the public adapters below."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await _safe(fn(*args, **kwargs))

    return wrapper


def _clip(value: Any, limit: int) -> Optional[str]:
    """Bound a correlation/id field to its column width (never raise)."""
    if value is None:
        return None
    text = str(value)
    return text[:limit] if len(text) > limit else text


# ── Task execution (state-machine transitions) ─────────────────────

# Task status → ledger event. Statuses outside this map (proposed, pending,
# on_hold, blocked, waiting_on_customer, scheduled) intentionally emit nothing:
# proposal facts come from the strategist adapters, and intermediate parking
# states are not execution facts.
_TASK_STATUS_EVENTS: dict[str, str] = {
    "in_progress": et.EXECUTION_STARTED,
    "completed": et.EXECUTION_COMPLETED,
    "failed": et.EXECUTION_FAILED,
    "cancelled": et.EXECUTION_CANCELLED,
}


def _task_root_execution_id(task: Any) -> Optional[str]:
    """Root of the execution chain a task belongs to.

    Cohort tasks inherit the strategist work batch; dispatched tasks may carry
    an explicit ``root_execution_id``; anything else is its own root.
    """
    details = task.details or {}
    return _clip(
        details.get("root_execution_id")
        or details.get("workspace_work_batch_id")
        or task.id,
        64,
    )


def _task_causation_id(task: Any) -> Optional[str]:
    details = task.details or {}
    return _clip(
        details.get("proposal_item_id")
        or details.get("strategist_review_id")
        or details.get("scheduled_job_id")
        or None,
        64,
    )


@_never_fatal
async def record_task_transition(
    db: Any,
    task: Any,
    new_status: str,
    *,
    actor_kind: str = "system",
    actor_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    config_versions: Optional[dict] = None,
) -> None:
    """Emit the execution_* event for an applied task status transition.

    Idempotency: every event key is attempt-free (``task:{id}:{event_type}``).
    For terminal events that is the spec: a retry that re-completes must not
    duplicate ``execution_completed``. For ``execution_started`` it means the
    FIRST start wins and later restarts dedupe onto it — restarts after a
    failure remain visible through the interleaved ``execution_failed`` rows.

    ``config_versions`` (M11) carries the execution-config revisions that
    produced the run — ``{"agent_revision": N, "skill_revision": N}`` — so
    outcome analysis can attribute a result to the exact agent/skill
    content. Callers pass it only where a cheap join exists (the plan
    executor's finalize); it is always optional.
    """
    event_type = _TASK_STATUS_EVENTS.get(new_status)
    if event_type is None:
        return
    if not getattr(task, "workspace_id", None):
        return  # entity-level task — not a workspace fact
    await record_event(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        event_type=event_type,
        source_kind="task",
        source_id=_clip(task.id, 64),
        status=new_status,
        config_versions=config_versions or None,
        root_execution_id=_task_root_execution_id(task),
        causation_id=_task_causation_id(task),
        actor_kind=actor_kind,
        actor_id=_clip(actor_id, 64),
        occurred_at=occurred_at,
        idempotency_key=f"task:{task.id}:{event_type}",
    )


# ── Artifacts (plan finalize) ──────────────────────────────────────

def _artifact_identity(ref: dict) -> str:
    return str(
        ref.get("document_id")
        or ref.get("fs_path")
        or ref.get("url")
        or ref.get("path")
        or ref.get("name")
        or ""
    )


@_never_fatal
async def record_task_artifacts(db: Any, task: Any, refs: list[dict] | None) -> None:
    """Emit one ``artifact_created`` per distinct artifact ref a task produced."""
    if not refs or not getattr(task, "workspace_id", None):
        return
    root = _task_root_execution_id(task)
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        identity = _artifact_identity(ref)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        # Long fs paths / URLs don't fit the key columns — hash them stably.
        key_ident = (
            identity
            if len(identity) <= 120
            else hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        )
        payload = {
            k: ref[k]
            for k in ("type", "step", "name", "document_id", "fs_path", "url")
            if ref.get(k)
        }
        await record_event(
            db,
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            event_type=et.ARTIFACT_CREATED,
            source_kind="artifact",
            source_id=_clip(identity, 64),
            causation_id=_clip(task.id, 64),
            root_execution_id=root,
            output_refs=[f"artifact:{identity}"],
            payload=payload or None,
            idempotency_key=_clip(f"artifact:{task.id}:{key_ident}", 200),
        )


# ── Automations (ScheduledJob / ScheduledJobRun) ───────────────────

def automation_period_key(job: Any, now: datetime) -> str:
    """Dedup period for an automation dispatch: hourly for sub-daily
    ``every`` jobs, daily otherwise."""
    try:
        every = float(job.every_seconds or 86400)
    except (TypeError, ValueError):
        every = 86400.0
    if (job.schedule_kind == "every") and every < 86400:
        return now.strftime("%Y-%m-%dT%H")
    return now.strftime("%Y-%m-%d")


def _automation_scope(job: Any) -> tuple[Optional[str], Optional[str]]:
    entity_id = getattr(job, "entity_id", None)
    target = job.execution_target if isinstance(job.execution_target, dict) else {}
    workspace_id = target.get("workspace_id") or getattr(job, "workspace_id", None)
    return entity_id, workspace_id


@_never_fatal
async def record_automation_dispatched(
    db: Any, job: Any, *, run_id: Optional[str], now: datetime,
    revision: Optional[int] = None, experiment_id: Optional[str] = None,
) -> None:
    entity_id, workspace_id = _automation_scope(job)
    if not entity_id or not workspace_id or not run_id:
        return  # entity-level job (or no run row) — skip
    period_key = automation_period_key(job, now)
    # M11: stamp the config version active at dispatch time so outcome
    # analysis can attribute results to the exact automation revision.
    if revision is None:
        revision = getattr(job, "revision", None)
    config_versions = (
        {"automation_revision": int(revision)} if revision else None
    )
    # M13: a run dispatched under an active experiment overlay belongs to
    # that experiment's cohort — the xp:{id}:{period} correlation is how
    # the guardrail monitor / evaluator recover the cohort from the ledger.
    correlation = (
        f"xp:{experiment_id}:{period_key}"
        if experiment_id
        else f"{job.id}:{period_key}"
    )
    await record_event(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type=et.AUTOMATION_RUN_DISPATCHED,
        source_kind="scheduled_job",
        source_id=_clip(job.id, 64),
        run_id=_clip(run_id, 64),
        root_execution_id=_clip(run_id, 64),
        correlation_id=_clip(correlation, 96),
        period_key=_clip(period_key, 40),
        status="dispatched",
        config_versions=config_versions,
        payload={
            "job_key": job.job_id,
            "execution_type": job.execution_type,
            "name": job.name,
        },
        occurred_at=now,
        idempotency_key=f"sjrun:{run_id}:dispatched",
    )


@_never_fatal
async def record_automation_run_missed(
    db: Any, job: Any, *, expected_by: datetime,
) -> Any:
    """Emit ``automation_run_missed`` for an enabled interval job whose
    period elapsed with no run (the scheduler tick's throttled missed-scan).

    ``expected_by`` is when the run should have happened
    (``last_run_at + every_seconds``); the period key derives from it so
    exactly one event is recorded per missed period. Returns the event row
    (or ``None`` when this period was already recorded) so the scan can
    count fresh emissions.
    """
    entity_id, workspace_id = _automation_scope(job)
    if not entity_id or not workspace_id:
        return None  # entity-level job — not a workspace fact
    period_key = automation_period_key(job, expected_by)
    return await record_event(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type=et.AUTOMATION_RUN_MISSED,
        source_kind="scheduled_job",
        source_id=_clip(job.id, 64),
        correlation_id=_clip(f"{job.id}:{period_key}", 96),
        period_key=_clip(period_key, 40),
        status="missed",
        payload={
            "job_key": job.job_id,
            "name": job.name,
            "expected_by": expected_by.isoformat(),
        },
        occurred_at=expected_by,
        idempotency_key=f"sj:{job.id}:missed:{period_key}",
    )


# ScheduledJobRun final statuses → ledger event ("skipped" is not an
# execution fact; automation_run_missed is the scheduler tick's missed-scan,
# wired through record_automation_run_missed above).
_AUTOMATION_FINAL_EVENTS: dict[str, str] = {
    "success": et.AUTOMATION_RUN_COMPLETED,
    "completed": et.AUTOMATION_RUN_COMPLETED,
    "error": et.AUTOMATION_RUN_FAILED,
    "failed": et.AUTOMATION_RUN_FAILED,
}


@_never_fatal
async def record_automation_run_finished(
    db: Any, job: Any, *, run_id: Optional[str], status: str,
) -> None:
    event_type = _AUTOMATION_FINAL_EVENTS.get(status)
    if event_type is None or not run_id:
        return
    entity_id, workspace_id = _automation_scope(job)
    if not entity_id or not workspace_id:
        return
    await record_event(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type=event_type,
        source_kind="scheduled_job",
        source_id=_clip(job.id, 64),
        run_id=_clip(run_id, 64),
        root_execution_id=_clip(run_id, 64),
        status=status,
        idempotency_key=f"sjrun:{run_id}:{event_type}",
    )


# ── Workflows (WorkflowRun) ────────────────────────────────────────

_WORKFLOW_STATUS_EVENTS: dict[str, str] = {
    "completed": et.WORKFLOW_RUN_COMPLETED,
    "failed": et.WORKFLOW_RUN_FAILED,
    "paused": et.WORKFLOW_RUN_PAUSED,
}


@_never_fatal
async def record_workflow_run_status(db: Any, run: Any) -> None:
    """Emit the workflow_run_* event matching ``run.status`` (terminal/paused).

    Safe to call at any settle point — non-terminal statuses and repeat calls
    (idempotency key is per run+event) are no-ops.
    """
    event_type = _WORKFLOW_STATUS_EVENTS.get(run.status)
    if event_type is None or not getattr(run, "workspace_id", None):
        return
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    payload = None
    if run.status == "failed" and run.error:
        payload = {"error": str(run.error)[:500]}
    await record_event(
        db,
        entity_id=run.entity_id,
        workspace_id=run.workspace_id,
        event_type=event_type,
        source_kind="workflow",
        source_id=_clip(run.binding_id or run.workflow_id, 64),
        run_id=_clip(run.id, 64),
        root_execution_id=_clip(run.id, 64),
        causation_id=_clip(trigger_data.get("scheduled_job_id"), 64),
        status=run.status,
        payload=payload,
        idempotency_key=f"wfrun:{run.id}:{event_type}",
    )


# ── Approvals (HitlRequest) ────────────────────────────────────

@_never_fatal
async def record_approval_event(
    db: Any, request: Any, event_type: str, *, actor_id: Optional[str] = None,
) -> None:
    """Emit an approval_* lifecycle event for a HitlRequest."""
    if not getattr(request, "workspace_id", None):
        return  # approvals with no workspace surface are not workspace facts
    if event_type in (et.APPROVAL_GRANTED, et.APPROVAL_DENIED):
        actor_kind = "user"
        actor = actor_id or request.decided_by_user_id
    else:
        actor_kind = "system"
        actor = actor_id
    await record_event(
        db,
        entity_id=request.entity_id,
        workspace_id=request.workspace_id,
        event_type=event_type,
        source_kind="approval",
        source_id=_clip(request.id, 64),
        causation_id=_clip(request.origin_step_id or request.origin_task_id, 64),
        actor_kind=actor_kind,
        actor_id=_clip(actor, 64),
        status=request.status,
        payload={
            k: v
            for k, v in {
                "action_key": request.action_key,
                "capability_id": request.capability_id,
                "matched_rule": request.matched_rule,
                "origin_kind": request.origin_kind,
            }.items()
            if v
        } or None,
        idempotency_key=f"approval:{request.id}:{event_type}",
    )


# ── Goals ──────────────────────────────────────────────────────────

@_never_fatal
async def record_goal_measurement_events(
    db: Any,
    goal: Any,
    *,
    value: Any,
    source: str,
    measured_at: datetime,
    old_pace: Optional[str],
    pace_recomputed: bool,
    achieved_now: bool,
) -> None:
    """Emit goal_measured (+ pace-change / achievement) for one measurement."""
    if not getattr(goal, "workspace_id", None):
        return
    base = dict(
        entity_id=goal.entity_id,
        workspace_id=goal.workspace_id,
        source_kind="goal",
        source_id=_clip(goal.id, 64),
        goal_refs=[goal.id],
        occurred_at=measured_at,
    )
    await record_event(
        db,
        event_type=et.GOAL_MEASURED,
        payload={"value": float(value), "source": source},
        status=None,
        idempotency_key=f"goal:{goal.id}:measured:{measured_at.isoformat()}",
        **base,
    )
    new_pace = goal.pace_status
    if pace_recomputed and new_pace and new_pace != old_pace:
        await record_event(
            db,
            event_type=et.GOAL_PACE_CHANGED,
            payload={"old_pace": old_pace, "new_pace": new_pace},
            idempotency_key=f"goal:{goal.id}:pace:{new_pace}:{measured_at.isoformat()}",
            **base,
        )
    if achieved_now:
        await record_event(
            db,
            event_type=et.GOAL_ACHIEVED,
            payload={"value": float(value)},
            idempotency_key=f"goal:{goal.id}:achieved",
            **base,
        )


# ── Strategist proposals (current tasks[] flow) ────────────────────

@_never_fatal
async def record_proposal_created(
    db: Any, workspace: Any, *, review_id: str, task_ids: list[str],
) -> None:
    await record_event(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=et.PROPOSAL_CREATED,
        source_kind="proposal",
        source_id=_clip(review_id, 64),
        causation_id=_clip(review_id, 64),
        payload={"task_count": len(task_ids), "task_ids": list(task_ids)},
        idempotency_key=f"proposal:{review_id}:created",
    )


@_never_fatal
async def record_human_request_item_auto_approved(
    db: Any,
    item: Any,
    *,
    review_id: str,
    commitment_id: Optional[str] = None,
) -> None:
    """M10: a kind="human_request" proposal item auto-approved at
    creation (human requests never mint a HitlRequest)."""
    if not getattr(item, "workspace_id", None):
        return
    payload = dict(item.payload or {})
    await record_event(
        db,
        entity_id=item.entity_id,
        workspace_id=item.workspace_id,
        event_type=et.PROPOSAL_ITEM_APPROVED,
        source_kind="proposal",
        source_id=_clip(item.id, 64),
        causation_id=_clip(review_id, 64),
        root_execution_id=_clip(commitment_id, 64) if commitment_id else None,
        actor_kind="system",
        payload={
            "kind": "human_request",
            "decided_via": "auto",
            "request_kind": payload.get("request_kind"),
            "question": payload.get("question"),
            "commitment_id": commitment_id,
        },
        idempotency_key=f"proposal:{review_id}:approved:{item.id}",
    )


@_never_fatal
async def record_change_item_applied(
    db: Any,
    item: Any,
    *,
    target_kind: str,
    target_id: str,
    operation: str,
    revision: Optional[int] = None,
) -> None:
    """M10: an approved configuration-change item was applied to its
    canonical row. ``goal_change`` items emit ``goal_changed``; the
    automation/workflow kinds emit ``config_changed``. ``config_versions``
    carries the new revision so downstream execution facts can be
    attributed to the exact config generation."""
    if not getattr(item, "workspace_id", None):
        return
    is_goal = target_kind == "goal"
    await record_event(
        db,
        entity_id=item.entity_id,
        workspace_id=item.workspace_id,
        event_type=et.GOAL_CHANGED if is_goal else et.CONFIG_CHANGED,
        source_kind="goal" if is_goal else "config",
        source_id=_clip(target_id, 64),
        causation_id=_clip(item.id, 64),
        root_execution_id=_clip(item.id, 64),
        actor_kind="agent",
        status="applied",
        config_versions=(
            {f"{target_kind}_revision": int(revision)} if revision is not None else None
        ),
        payload={
            "kind": getattr(item, "kind", None),
            "operation": operation,
            "target_kind": target_kind,
            "target_id": target_id,
            "proposal_item_id": item.id,
        },
        idempotency_key=f"change:{item.id}:applied",
    )


@_never_fatal
async def record_proposal_item_decision(
    db: Any,
    task: Any,
    *,
    review_id: str,
    approved: bool,
    batch_id: Optional[str] = None,
    actor_kind: str = "user",
    actor_id: Optional[str] = None,
    reason: Optional[str] = None,
    reason_code: Optional[str] = None,
) -> None:
    if not getattr(task, "workspace_id", None):
        return
    event_type = et.PROPOSAL_ITEM_APPROVED if approved else et.PROPOSAL_ITEM_REJECTED
    decision = "approved" if approved else "rejected"
    payload: dict[str, Any] = {"title": task.title}
    if reason and not approved:
        payload["rejection_reason"] = reason
    # The machine-readable half of the decision. Consumers that reason
    # about *why* a proposal died (learning stats) must read this, not the
    # free prose beside it.
    if reason_code and not approved:
        payload["rejection_reason_code"] = reason_code
    await record_event(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        event_type=event_type,
        source_kind="proposal",
        source_id=_clip(task.id, 64),
        causation_id=_clip(review_id, 64),
        root_execution_id=_clip(batch_id, 64) if approved else None,
        actor_kind=actor_kind,
        actor_id=_clip(actor_id, 64),
        payload=payload,
        idempotency_key=f"proposal:{review_id}:{decision}:{task.id}",
    )
