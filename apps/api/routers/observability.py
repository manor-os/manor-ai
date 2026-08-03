"""Workspace observability endpoints (M14) — timeline + strategy + health.

Pure read-side queries over the existing fact tables — no new tables:

  GET /timeline            correlation-chain view over ``workspace_events``.
                           Exactly one selector required:
                           ``root_execution_id`` | ``review_id`` |
                           ``correlation_id``.
  GET /strategy/reviews    newest-first ReviewRun digests + report /
                           proposal-item aggregate counts (single grouped
                           queries — no per-review N+1).
  GET /strategy/reviews/{review_id}
                           full review detail — briefing as stored, all
                           consolidation report rows, proposal + items.
  GET /automation-health   per enabled automation (ScheduledJob +
                           WorkflowBinding): 30-day run counts from one
                           grouped ledger aggregation, failure streak,
                           revision, active experiment overlay.
  GET /tasks/{task_id}/provenance
                           execution-provenance breadcrumb for one task:
                           what triggered it (proposal item → review /
                           automation / chat / manual), the causation
                           chain, the config revisions stamped on its
                           terminal event, and its root event chain.

Auth matches the sibling governance/humans routers: the workspace must
belong to the caller's entity (404 otherwise).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.experiments.controller import EXPERIMENT_OVERLAY_KEY
from packages.core.ledger import event_types as et
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.experiment import Experiment
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.review_run import ReviewRun
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowBinding
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services.entity_service import get_workspace

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["observability"],
)

# Per-root event cap in the review-tree ``executions`` branch.
_EXECUTION_EVENTS_PER_ROOT = 50
# Payloads above this serialized size are digested to their keys only.
_SMALL_PAYLOAD_MAX_CHARS = 600


# ── helpers ───────────────────────────────────────────────────────────

async def _require_workspace(db: AsyncSession, workspace_id: str, entity_id: str):
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _event_digest(event: WorkspaceEvent) -> dict:
    """Compact event dict. Small payloads ride along verbatim; large ones
    are digested to their key list so the timeline stays light."""
    digest: dict[str, Any] = {
        "id": event.id,
        "event_type": event.event_type,
        "source_kind": event.source_kind,
        "source_id": event.source_id,
        "run_id": event.run_id,
        "status": event.status,
        "actor_kind": event.actor_kind,
        "actor_id": event.actor_id,
        "root_execution_id": event.root_execution_id,
        "causation_id": event.causation_id,
        "correlation_id": event.correlation_id,
        "occurred_at": _iso(event.occurred_at),
    }
    payload = event.payload
    if isinstance(payload, dict) and payload:
        try:
            small = len(json.dumps(payload, default=str)) <= _SMALL_PAYLOAD_MAX_CHARS
        except (TypeError, ValueError):
            small = False
        if small:
            digest["payload"] = payload
        else:
            digest["payload_keys"] = sorted(payload.keys())
    return digest


def _review_digest(review: ReviewRun) -> dict:
    return {
        "id": review.id,
        "trigger_kind": review.trigger_kind,
        "status": review.status,
        "skip_reason": review.skip_reason,
        "window_start": _iso(review.window_start),
        "window_end": _iso(review.window_end),
        "watermark_start": review.watermark_start,
        "watermark_end": review.watermark_end,
        "workspace_revision": review.workspace_revision,
        "policy_revision": review.policy_revision,
        "created_at": _iso(review.created_at),
        "completed_at": _iso(review.completed_at),
    }


def _report_digest(report: ConsolidationReport) -> dict:
    return {
        "id": report.id,
        "domain": report.domain,
        "status": report.status,
        "summary": report.summary,
        "analyzer_version": report.analyzer_version,
        "created_at": _iso(report.created_at),
    }


def _item_digest(item: ProposalItemRecord) -> dict:
    return {
        "id": item.id,
        "item_key": item.item_key,
        "kind": item.kind,
        "status": item.status,
        "risk_level": item.risk_level,
        "action_key": item.action_key,
        "decision": item.decision,
        "execution_root_id": item.execution_root_id,
        "created_at": _iso(item.created_at),
        "decided_at": _iso(item.decided_at),
    }


def _item_full(item: ProposalItemRecord) -> dict:
    return {
        **_item_digest(item),
        "payload": item.payload,
        "basis": item.basis,
        "correlation_key": item.correlation_key,
        "depends_on_item_keys": item.depends_on_item_keys,
        "approval_request_id": item.approval_request_id,
        "expected_revision": item.expected_revision,
        "finished_at": _iso(item.finished_at),
    }


async def _proposal_with_items(
    db: AsyncSession, workspace_id: str, review_id: str, *, full_items: bool,
) -> Optional[dict]:
    proposal = (await db.execute(
        select(ProposalRecord).where(
            ProposalRecord.workspace_id == workspace_id,
            ProposalRecord.review_id == review_id,
        ).order_by(ProposalRecord.created_at)
    )).scalars().first()
    if proposal is None:
        return None
    items = (await db.execute(
        select(ProposalItemRecord).where(
            ProposalItemRecord.proposal_id == proposal.id,
        ).order_by(ProposalItemRecord.created_at, ProposalItemRecord.id)
    )).scalars().all()
    render = _item_full if full_items else _item_digest
    return {
        "id": proposal.id,
        "summary": proposal.summary,
        "notes": proposal.notes,
        "status": proposal.status,
        "created_at": _iso(proposal.created_at),
        "resolved_at": _iso(proposal.resolved_at),
        "items": [render(item) for item in items],
    }


# ── GET /timeline ─────────────────────────────────────────────────────

@router.get("/timeline")
async def get_timeline(
    workspace_id: str,
    root_execution_id: Optional[str] = Query(default=None),
    review_id: Optional[str] = Query(default=None),
    correlation_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Correlation-chain timeline. Exactly one selector is required."""
    await _require_workspace(db, workspace_id, user.entity_id)

    selectors = {
        "root_execution_id": root_execution_id,
        "review_id": review_id,
        "correlation_id": correlation_id,
    }
    provided = [name for name, value in selectors.items() if value]
    if len(provided) != 1:
        raise HTTPException(
            400,
            "Exactly one selector is required: root_execution_id, "
            "review_id, or correlation_id",
        )

    if root_execution_id:
        return await _timeline_by_root(db, workspace_id, root_execution_id, limit)
    if review_id:
        return await _timeline_by_review(db, workspace_id, review_id)
    return await _timeline_by_correlation(db, workspace_id, correlation_id, limit)


async def _timeline_by_root(
    db: AsyncSession, workspace_id: str, root_execution_id: str, limit: int,
) -> dict:
    events = (await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.root_execution_id == root_execution_id,
        ).order_by(WorkspaceEvent.id).limit(limit)
    )).scalars().all()

    # Follow-up echo: for each distinct causation_id in the chain, surface
    # the ids of the ledger events that carry that id as their source/run —
    # ids only, no recursion (the client re-queries if it wants to walk up).
    causation_ids = sorted({e.causation_id for e in events if e.causation_id})
    causes: dict[str, list[str]] = {}
    if causation_ids:
        rows = (await db.execute(
            select(
                WorkspaceEvent.id,
                WorkspaceEvent.source_id,
                WorkspaceEvent.run_id,
            ).where(
                WorkspaceEvent.workspace_id == workspace_id,
                (WorkspaceEvent.source_id.in_(causation_ids))
                | (WorkspaceEvent.run_id.in_(causation_ids)),
            ).order_by(WorkspaceEvent.id)
        )).all()
        causes = {cid: [] for cid in causation_ids}
        for event_id, source_id, run_id in rows:
            for cid in (source_id, run_id):
                if cid in causes and event_id not in causes[cid]:
                    causes[cid].append(event_id)
    return {
        "selector": {"root_execution_id": root_execution_id},
        "events": [_event_digest(e) for e in events],
        "causes": causes,
    }


async def _timeline_by_review(
    db: AsyncSession, workspace_id: str, review_id: str,
) -> dict:
    review = (await db.execute(
        select(ReviewRun).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.id == review_id,
        )
    )).scalar_one_or_none()
    if review is None:
        raise HTTPException(404, "Review not found")

    reports = (await db.execute(
        select(ConsolidationReport).where(
            ConsolidationReport.review_id == review.id,
        ).order_by(ConsolidationReport.domain)
    )).scalars().all()

    proposal = await _proposal_with_items(
        db, workspace_id, review.id, full_items=False,
    )

    # Execution branch: the ledger chain under every dispatched item root.
    executions: dict[str, list[dict]] = {}
    root_ids = sorted({
        item["execution_root_id"]
        for item in (proposal["items"] if proposal else [])
        if item["execution_root_id"]
    })
    for root_id in root_ids:
        rows = (await db.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.root_execution_id == root_id,
            ).order_by(WorkspaceEvent.id).limit(_EXECUTION_EVENTS_PER_ROOT)
        )).scalars().all()
        executions[root_id] = [_event_digest(e) for e in rows]

    return {
        "selector": {"review_id": review_id},
        "review": _review_digest(review),
        "reports": [_report_digest(r) for r in reports],
        "proposal": proposal,
        "executions": executions,
    }


async def _timeline_by_correlation(
    db: AsyncSession, workspace_id: str, correlation_id: str, limit: int,
) -> dict:
    events = (await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.correlation_id == correlation_id,
        ).order_by(WorkspaceEvent.id).limit(limit)
    )).scalars().all()
    return {
        "selector": {"correlation_id": correlation_id},
        "events": [_event_digest(e) for e in events],
    }


# ── GET /strategy/reviews ─────────────────────────────────────────────

@router.get("/strategy/reviews")
async def list_strategy_reviews(
    workspace_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Newest-first review digests with report + proposal-item aggregates."""
    await _require_workspace(db, workspace_id, user.entity_id)

    reviews = (await db.execute(
        select(ReviewRun).where(
            ReviewRun.workspace_id == workspace_id,
        ).order_by(ReviewRun.created_at.desc(), ReviewRun.id.desc()).limit(limit)
    )).scalars().all()
    review_ids = [r.id for r in reviews]

    report_counts: dict[str, dict[str, int]] = {rid: {} for rid in review_ids}
    proposal_by_review: dict[str, str] = {}
    item_counts: dict[str, list[dict]] = {rid: [] for rid in review_ids}
    if review_ids:
        # One grouped query per aggregate — no per-review N+1.
        for review_id, status, count in (await db.execute(
            select(
                ConsolidationReport.review_id,
                ConsolidationReport.status,
                func.count(),
            ).where(
                ConsolidationReport.review_id.in_(review_ids),
            ).group_by(ConsolidationReport.review_id, ConsolidationReport.status)
        )).all():
            report_counts[review_id][status] = int(count)

        proposals = (await db.execute(
            select(ProposalRecord.id, ProposalRecord.review_id).where(
                ProposalRecord.workspace_id == workspace_id,
                ProposalRecord.review_id.in_(review_ids),
            )
        )).all()
        proposal_review = {pid: rid for pid, rid in proposals}
        proposal_by_review.update({rid: pid for pid, rid in proposals})

        if proposal_review:
            for proposal_id, kind, status, count in (await db.execute(
                select(
                    ProposalItemRecord.proposal_id,
                    ProposalItemRecord.kind,
                    ProposalItemRecord.status,
                    func.count(),
                ).where(
                    ProposalItemRecord.proposal_id.in_(list(proposal_review)),
                ).group_by(
                    ProposalItemRecord.proposal_id,
                    ProposalItemRecord.kind,
                    ProposalItemRecord.status,
                )
            )).all():
                review_id = proposal_review[proposal_id]
                item_counts[review_id].append(
                    {"kind": kind, "status": status, "count": int(count)}
                )

    return {
        "reviews": [
            {
                **_review_digest(review),
                "reports": {
                    "complete": report_counts[review.id].get("complete", 0),
                    "partial": report_counts[review.id].get("partial", 0),
                    "failed": report_counts[review.id].get("failed", 0),
                },
                "proposal_id": proposal_by_review.get(review.id),
                "item_counts": sorted(
                    item_counts[review.id],
                    key=lambda c: (c["kind"], c["status"]),
                ),
            }
            for review in reviews
        ],
    }


# ── GET /strategy/reviews/{review_id} ─────────────────────────────────

@router.get("/strategy/reviews/{review_id}")
async def get_strategy_review(
    workspace_id: str,
    review_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full review detail: row + briefing as stored + full report rows +
    proposal with full items (payload / basis / decision)."""
    await _require_workspace(db, workspace_id, user.entity_id)

    review = (await db.execute(
        select(ReviewRun).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.id == review_id,
        )
    )).scalar_one_or_none()
    if review is None:
        raise HTTPException(404, "Review not found")

    reports = (await db.execute(
        select(ConsolidationReport).where(
            ConsolidationReport.review_id == review.id,
        ).order_by(ConsolidationReport.domain)
    )).scalars().all()

    proposal = await _proposal_with_items(
        db, workspace_id, review.id, full_items=True,
    )

    return {
        "review": {
            **_review_digest(review),
            "briefing": review.briefing,
            "error": review.error,
        },
        "reports": [
            {
                **_report_digest(report),
                "scope": report.scope,
                "metrics": report.metrics,
                "observations": report.observations,
                "relationships": report.relationships,
                "uncertainties": report.uncertainties,
                "evidence_refs": report.evidence_refs,
                "coverage": report.coverage,
                "input_hash": report.input_hash,
            }
            for report in reports
        ],
        "proposal": proposal,
    }


# ── GET /tasks/{task_id}/provenance ───────────────────────────────────

# Task.details keys that identify the chat message a task came from.
_CHAT_ORIGIN_KEYS = ("source_message_id", "chat_message_id", "message_id")
# Terminal execution events carry the M11 config_versions stamp.
_TERMINAL_EXECUTION_EVENTS = (
    et.EXECUTION_COMPLETED,
    et.EXECUTION_FAILED,
    et.EXECUTION_CANCELLED,
)


def _step(kind: str, ident: Optional[str], label: Optional[str]) -> dict:
    return {"kind": kind, "id": ident, "label": label}


async def _resolve_trigger(
    db: AsyncSession, workspace_id: str, task: Task,
) -> tuple[dict, list[dict]]:
    """Deterministic trigger + causation chain for a task.

    Mirrors the ledger adapter's ``_task_causation_id`` precedence
    (proposal item → strategist review → scheduled job), then falls back
    to a chat origin, then to ``manual``. The chain is ordered from the
    root cause down to the task itself, so the UI can render it as a
    breadcrumb without any further lookups.
    """
    details = task.details or {}
    task_step = _step("task", task.id, task.title)

    item_id = details.get("proposal_item_id")
    if item_id:
        item = (await db.execute(
            select(ProposalItemRecord).where(
                ProposalItemRecord.workspace_id == workspace_id,
                ProposalItemRecord.id == str(item_id),
            )
        )).scalar_one_or_none()
        label = None
        review_id = details.get("strategist_review_id")
        if item is not None:
            payload = item.payload if isinstance(item.payload, dict) else {}
            label = payload.get("title") or item.item_key
            proposal = (await db.execute(
                select(ProposalRecord).where(
                    ProposalRecord.id == item.proposal_id,
                )
            )).scalar_one_or_none()
            if proposal is not None and proposal.review_id:
                review_id = proposal.review_id
        trigger = _step("proposal_item", str(item_id), label)
        chain: list[dict] = []
        if review_id:
            chain.append(_step("review", str(review_id), f"Review {review_id}"))
        chain.append(trigger)
        chain.append(task_step)
        return trigger, chain

    review_id = details.get("strategist_review_id")
    if review_id:
        trigger = _step("review", str(review_id), f"Review {review_id}")
        return trigger, [trigger, task_step]

    job_id = details.get("scheduled_job_id")
    if job_id:
        job = (await db.execute(
            select(ScheduledJob).where(ScheduledJob.id == str(job_id))
        )).scalar_one_or_none()
        trigger = _step(
            "scheduled_job",
            str(job_id),
            (job.name or job.job_id) if job is not None else None,
        )
        return trigger, [trigger, task_step]

    chat_id = next(
        (str(details[key]) for key in _CHAT_ORIGIN_KEYS if details.get(key)),
        None,
    ) or task.conversation_id
    if chat_id:
        trigger = _step("chat", str(chat_id), None)
        return trigger, [trigger, task_step]

    return _step("manual", None, None), [task_step]


@router.get("/tasks/{task_id}/provenance")
async def get_task_provenance(
    workspace_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Where did this task come from? — the M14 execution-provenance
    breadcrumb rendered above the task's execution timeline."""
    await _require_workspace(db, workspace_id, user.entity_id)

    task = (await db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.workspace_id == workspace_id,
            Task.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")

    task_events = (await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.source_kind == "task",
            WorkspaceEvent.source_id == task.id,
        ).order_by(WorkspaceEvent.id)
    )).scalars().all()

    details = task.details or {}
    root_execution_id = next(
        (e.root_execution_id for e in task_events if e.root_execution_id),
        None,
    ) or details.get("root_execution_id") or details.get(
        "workspace_work_batch_id"
    ) or task.id

    # M11 config stamp: the terminal event wins, any stamped event is the
    # fallback (a still-running task may only have a started event).
    config_versions: dict = {}
    for event in reversed(task_events):
        if event.event_type in _TERMINAL_EXECUTION_EVENTS and event.config_versions:
            config_versions = dict(event.config_versions)
            break
    if not config_versions:
        for event in reversed(task_events):
            if event.config_versions:
                config_versions = dict(event.config_versions)
                break

    trigger, causation_chain = await _resolve_trigger(db, workspace_id, task)

    root_events = (await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.root_execution_id == str(root_execution_id),
        ).order_by(WorkspaceEvent.id).limit(_EXECUTION_EVENTS_PER_ROOT)
    )).scalars().all()

    return {
        "task": {"id": task.id, "title": task.title, "status": task.status},
        "root_execution_id": str(root_execution_id),
        "trigger": trigger,
        "causation_chain": causation_chain,
        "config_versions": config_versions,
        "events": [_event_digest(e) for e in root_events],
    }


# ── GET /automation-health ────────────────────────────────────────────

# ledger event → runs_30d bucket, per automation kind.
_RUN_BUCKETS: dict[str, dict[str, str]] = {
    "scheduled_job": {
        et.AUTOMATION_RUN_DISPATCHED: "dispatched",
        et.AUTOMATION_RUN_COMPLETED: "completed",
        et.AUTOMATION_RUN_FAILED: "failed",
        et.AUTOMATION_RUN_MISSED: "missed",
    },
    # Workflow runs have no dispatch/missed ledger phases — started counts
    # as dispatched so both kinds share the same runs_30d shape.
    "workflow": {
        et.WORKFLOW_RUN_STARTED: "dispatched",
        et.WORKFLOW_RUN_COMPLETED: "completed",
        et.WORKFLOW_RUN_FAILED: "failed",
    },
}
_EMPTY_RUNS = {"dispatched": 0, "completed": 0, "failed": 0, "missed": 0}


def _overlay_experiment_id(config: Any) -> Optional[str]:
    if not isinstance(config, dict):
        return None
    overlay = config.get(EXPERIMENT_OVERLAY_KEY)
    if isinstance(overlay, dict) and overlay.get("experiment_id"):
        return str(overlay["experiment_id"])
    return None


@router.get("/automation-health")
async def get_automation_health(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per enabled automation: schedule digest, 30-day run counts from one
    grouped ledger aggregation, failure streak, revision, experiment."""
    await _require_workspace(db, workspace_id, user.entity_id)

    jobs = (await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.workspace_id == workspace_id,
            ScheduledJob.enabled.is_(True),
        ).order_by(ScheduledJob.created_at)
    )).scalars().all()
    bindings = (await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.enabled.is_(True),
        ).order_by(WorkflowBinding.created_at)
    )).scalars().all()

    # One grouped ledger scan for every automation's last-30d run events.
    since = datetime.now(timezone.utc) - timedelta(days=30)
    tracked_events = [
        event for buckets in _RUN_BUCKETS.values() for event in buckets
    ]
    runs: dict[tuple[str, str], dict[str, int]] = {}
    for source_kind, source_id, event_type, count in (await db.execute(
        select(
            WorkspaceEvent.source_kind,
            WorkspaceEvent.source_id,
            WorkspaceEvent.event_type,
            func.count(),
        ).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type.in_(tracked_events),
            WorkspaceEvent.occurred_at >= since,
        ).group_by(
            WorkspaceEvent.source_kind,
            WorkspaceEvent.source_id,
            WorkspaceEvent.event_type,
        )
    )).all():
        bucket = _RUN_BUCKETS.get(source_kind, {}).get(event_type)
        if bucket is None:
            continue
        entry = runs.setdefault((source_kind, source_id), dict(_EMPTY_RUNS))
        entry[bucket] += int(count)

    # Active experiment overlays, resolved in one batch.
    experiment_ids = [
        xid
        for xid in (
            [_overlay_experiment_id(job.execution_target) for job in jobs]
            + [_overlay_experiment_id(binding.config) for binding in bindings]
        )
        if xid
    ]
    experiments: dict[str, Experiment] = {}
    if experiment_ids:
        experiments = {
            row.id: row
            for row in (await db.execute(
                select(Experiment).where(Experiment.id.in_(experiment_ids))
            )).scalars().all()
        }

    def _experiment_ref(config: Any) -> Optional[dict]:
        xid = _overlay_experiment_id(config)
        if not xid:
            return None
        experiment = experiments.get(xid)
        return {"id": xid, "status": experiment.status if experiment else None}

    automations: list[dict] = []
    for job in jobs:
        automations.append({
            "id": job.id,
            "name": job.name or job.job_id,
            "kind": "scheduled_job",
            "schedule": {
                "kind": job.schedule_kind or job.job_type,
                "cron_expr": job.cron_expr,
                "every_seconds": job.every_seconds,
                "run_at": job.run_at,
                "timezone": job.timezone,
            },
            "revision": job.revision,
            "consecutive_errors": job.consecutive_errors or 0,
            "last_run_at": _iso(job.last_run_at),
            "last_status": job.last_status,
            "runs_30d": runs.get(("scheduled_job", job.id), dict(_EMPTY_RUNS)),
            "active_experiment": _experiment_ref(job.execution_target),
        })
    for binding in bindings:
        automations.append({
            "id": binding.id,
            "name": binding.name or binding.workflow_id,
            "kind": "workflow_binding",
            "schedule": {
                "kind": binding.trigger_type,
                "cron_expr": (binding.trigger_config or {}).get("cron"),
                "every_seconds": None,
                "run_at": None,
                "timezone": (binding.trigger_config or {}).get("timezone"),
            },
            "revision": binding.revision,
            "consecutive_errors": 0,
            "last_run_at": None,
            "last_status": binding.status,
            # Workflow run events key off the binding id (falling back to
            # the workflow id for definition-level runs).
            "runs_30d": runs.get(("workflow", binding.id), dict(_EMPTY_RUNS)),
            "active_experiment": _experiment_ref(binding.config),
        })

    return {"automations": automations}
