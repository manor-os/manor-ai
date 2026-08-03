"""One-shot backfill: seed ``workspace_events`` from historical execution rows.

Background (M1 回填)
--------------------
The workspace event ledger only records facts from the moment its adapters
shipped. Review / Timeline / outcome evaluation need a baseline, so this
script replays the recent history (default 90 days) of the five source
tables into the ledger:

* ``tasks``               (terminal: completed / failed / cancelled) → execution_*
* ``scheduled_job_runs``  (finished)                                 → automation_run_*
* ``workflow_runs``       (terminal: completed / failed)             → workflow_run_*
* ``approval_requests``   (decided: granted / denied / consumed / expired) → approval_*
  — the table is now ``hitl_requests``; this name is kept as the key
  namespace only (see ``_approval_candidates``)
* ``goal_measurements``   (capped at the most recent 200 per goal)   → goal_measured

Every backfilled event uses ``idempotency_key = f"backfill:{table}:{row_id}:{event_type}"``
so re-running is safe. Rows whose LIVE adapter event already exists (e.g. a
task completed after the adapters shipped) are skipped too — the fact is
already on the ledger under its live key, and writing a second copy would
double-count it.

Usage
-----
    # Dry-run: print per-source counts, write nothing
    PYTHONPATH=. python scripts/backfill_workspace_events.py --dry-run

    # Live backfill (last 90 days)
    PYTHONPATH=. python scripts/backfill_workspace_events.py

    # Custom window / single entity
    PYTHONPATH=. python scripts/backfill_workspace_events.py --days 30 --entity 01J...

Writes are committed in batches of 500.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.core.ledger import event_types as et
from packages.core.ledger.adapters import (
    _AUTOMATION_FINAL_EVENTS,
    _TASK_STATUS_EVENTS,
    _automation_scope,
    _clip,
    _task_causation_id,
    _task_root_execution_id,
)
from packages.core.ledger.service import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.goal import Goal, GoalMeasurement
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace_event import WorkspaceEvent

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill_workspace_events")

BATCH_SIZE = 500
GOAL_MEASUREMENT_CAP = 200  # per goal — bounds ledger volume for chatty metrics

_TASK_TERMINAL_STATUSES = ("completed", "failed", "cancelled")

_WORKFLOW_TERMINAL_EVENTS = {
    "completed": et.WORKFLOW_RUN_COMPLETED,
    "failed": et.WORKFLOW_RUN_FAILED,
}

_APPROVAL_STATUS_EVENTS = {
    "granted": et.APPROVAL_GRANTED,
    "denied": et.APPROVAL_DENIED,
    "consumed": et.APPROVAL_CONSUMED,
    "expired": et.APPROVAL_EXPIRED,
}


def _backfill_key(table: str, row_id: str, event_type: str) -> str:
    return _clip(f"backfill:{table}:{row_id}:{event_type}", 200)


# ── candidate builders (one per source table) ──────────────────────
# Each returns a list of dicts: record_event kwargs + a "live_key" (the
# idempotency key the live adapter would have used for the same fact).


async def _task_candidates(db: AsyncSession, *, cutoff, entity_id) -> list[dict]:
    stmt = select(Task).where(
        Task.status.in_(_TASK_TERMINAL_STATUSES),
        Task.workspace_id.is_not(None),
        func.coalesce(Task.updated_at, Task.created_at) >= cutoff,
    )
    if entity_id:
        stmt = stmt.where(Task.entity_id == entity_id)
    tasks = (await db.execute(stmt)).scalars().all()

    out: list[dict] = []
    for task in tasks:
        event_type = _TASK_STATUS_EVENTS.get(task.status)
        if event_type is None:
            continue
        occurred = task.completed_at or task.updated_at or task.created_at
        out.append(dict(
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            event_type=event_type,
            source_kind="task",
            source_id=_clip(task.id, 64),
            status=task.status,
            root_execution_id=_task_root_execution_id(task),
            causation_id=_task_causation_id(task),
            actor_kind="system",
            occurred_at=occurred,
            idempotency_key=_backfill_key("tasks", task.id, event_type),
            live_key=f"task:{task.id}:{event_type}",
        ))
    return out


async def _scheduled_run_candidates(db: AsyncSession, *, cutoff, entity_id) -> list[dict]:
    stmt = select(ScheduledJobRun).where(
        ScheduledJobRun.status.in_(tuple(_AUTOMATION_FINAL_EVENTS)),
        ScheduledJobRun.completed_at.is_not(None),
        ScheduledJobRun.completed_at >= cutoff,
    )
    runs = (await db.execute(stmt)).scalars().all()
    job_keys = {run.job_id for run in runs}
    jobs: dict[str, ScheduledJob] = {}
    if job_keys:
        rows = (await db.execute(
            select(ScheduledJob).where(ScheduledJob.job_id.in_(job_keys))
        )).scalars().all()
        jobs = {job.job_id: job for job in rows}

    out: list[dict] = []
    for run in runs:
        job = jobs.get(run.job_id)
        if job is None:
            continue
        if entity_id and job.entity_id != entity_id:
            continue
        ent, ws = _automation_scope(job)
        if not ent or not ws:
            continue  # entity-level automation — not a workspace fact
        event_type = _AUTOMATION_FINAL_EVENTS[run.status]
        out.append(dict(
            entity_id=ent,
            workspace_id=ws,
            event_type=event_type,
            source_kind="scheduled_job",
            source_id=_clip(job.id, 64),
            run_id=_clip(run.id, 64),
            root_execution_id=_clip(run.id, 64),
            status=run.status,
            occurred_at=run.completed_at,
            idempotency_key=_backfill_key("scheduled_job_runs", run.id, event_type),
            live_key=f"sjrun:{run.id}:{event_type}",
        ))
    return out


async def _workflow_run_candidates(db: AsyncSession, *, cutoff, entity_id) -> list[dict]:
    stmt = select(WorkflowRun).where(
        WorkflowRun.status.in_(tuple(_WORKFLOW_TERMINAL_EVENTS)),
        WorkflowRun.workspace_id.is_not(None),
        func.coalesce(WorkflowRun.completed_at, WorkflowRun.updated_at, WorkflowRun.created_at) >= cutoff,
    )
    if entity_id:
        stmt = stmt.where(WorkflowRun.entity_id == entity_id)
    runs = (await db.execute(stmt)).scalars().all()

    out: list[dict] = []
    for run in runs:
        event_type = _WORKFLOW_TERMINAL_EVENTS[run.status]
        trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
        payload = None
        if run.status == "failed" and run.error:
            payload = {"error": str(run.error)[:500]}
        out.append(dict(
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
            occurred_at=run.completed_at or run.updated_at or run.created_at,
            idempotency_key=_backfill_key("workflow_runs", run.id, event_type),
            live_key=f"wfrun:{run.id}:{event_type}",
        ))
    return out


async def _approval_candidates(db: AsyncSession, *, cutoff, entity_id) -> list[dict]:
    stmt = select(HitlRequest).where(
        HitlRequest.status.in_(tuple(_APPROVAL_STATUS_EVENTS)),
        HitlRequest.workspace_id.is_not(None),
        func.coalesce(
            HitlRequest.consumed_at,
            HitlRequest.decided_at,
            HitlRequest.created_at,
        ) >= cutoff,
    )
    if entity_id:
        stmt = stmt.where(HitlRequest.entity_id == entity_id)
    requests = (await db.execute(stmt)).scalars().all()

    out: list[dict] = []
    for req in requests:
        event_type = _APPROVAL_STATUS_EVENTS[req.status]
        if req.status == "consumed":
            occurred = req.consumed_at or req.decided_at or req.created_at
        else:
            occurred = req.decided_at or req.created_at
        if event_type in (et.APPROVAL_GRANTED, et.APPROVAL_DENIED):
            actor_kind, actor_id = "user", req.decided_by_user_id
        else:
            actor_kind, actor_id = "system", None
        out.append(dict(
            entity_id=req.entity_id,
            workspace_id=req.workspace_id,
            event_type=event_type,
            source_kind="approval",
            source_id=_clip(req.id, 64),
            causation_id=_clip(req.origin_step_id or req.origin_task_id, 64),
            actor_kind=actor_kind,
            actor_id=_clip(actor_id, 64),
            status=req.status,
            payload={
                k: v
                for k, v in {
                    "action_key": req.action_key,
                    "capability_id": req.capability_id,
                    "matched_rule": req.matched_rule,
                    "origin_kind": req.origin_kind,
                }.items()
                if v
            } or None,
            occurred_at=occurred,
            # The literal "approval_requests" is FROZEN: it is baked into
            # idempotency keys already persisted on the ledger. The table is
            # now ``hitl_requests`` (20260802_02), but changing this string
            # would make every previously backfilled row look new and
            # double-count it.
            idempotency_key=_backfill_key("approval_requests", req.id, event_type),
            live_key=f"approval:{req.id}:{event_type}",
        ))
    return out


async def _goal_measurement_candidates(db: AsyncSession, *, cutoff, entity_id) -> list[dict]:
    stmt = select(Goal).where(Goal.workspace_id.is_not(None))
    if entity_id:
        stmt = stmt.where(Goal.entity_id == entity_id)
    goals = (await db.execute(stmt)).scalars().all()

    out: list[dict] = []
    for goal in goals:
        measurements = (await db.execute(
            select(GoalMeasurement)
            .where(
                GoalMeasurement.goal_id == goal.id,
                GoalMeasurement.measured_at >= cutoff,
            )
            .order_by(GoalMeasurement.measured_at.desc())
            .limit(GOAL_MEASUREMENT_CAP)
        )).scalars().all()
        for m in measurements:
            measured_iso = m.measured_at.isoformat()
            out.append(dict(
                entity_id=goal.entity_id,
                workspace_id=goal.workspace_id,
                event_type=et.GOAL_MEASURED,
                source_kind="goal",
                source_id=_clip(goal.id, 64),
                goal_refs=[goal.id],
                payload={"value": float(m.value), "source": m.source},
                occurred_at=m.measured_at,
                idempotency_key=_backfill_key(
                    "goal_measurements", f"{goal.id}:{measured_iso}", et.GOAL_MEASURED,
                ),
                live_key=f"goal:{goal.id}:measured:{measured_iso}",
            ))
    return out


# ── shared emit path ───────────────────────────────────────────────


async def _existing_keys(
    db: AsyncSession, entity_ids: set[str], keys: set[str],
) -> set[tuple[str, str]]:
    """(entity_id, idempotency_key) pairs already on the ledger."""
    found: set[tuple[str, str]] = set()
    key_list = list(keys)
    for i in range(0, len(key_list), 1000):
        chunk = key_list[i : i + 1000]
        rows = (await db.execute(
            select(WorkspaceEvent.entity_id, WorkspaceEvent.idempotency_key).where(
                WorkspaceEvent.entity_id.in_(list(entity_ids)),
                WorkspaceEvent.idempotency_key.in_(chunk),
            )
        )).all()
        found.update((row[0], row[1]) for row in rows)
    return found


async def _emit_candidates(
    db: AsyncSession, candidates: list[dict], *, dry_run: bool,
) -> tuple[int, int]:
    """Write candidates through record_event. Returns (created, skipped).

    A candidate is skipped when its backfill key OR its live-adapter key is
    already on the ledger (the fact exists). Commits every BATCH_SIZE writes.
    """
    if not candidates:
        return 0, 0
    entity_ids = {c["entity_id"] for c in candidates}
    all_keys: set[str] = set()
    for c in candidates:
        all_keys.add(c["idempotency_key"])
        if c.get("live_key"):
            all_keys.add(c["live_key"])
    existing = await _existing_keys(db, entity_ids, all_keys)

    created = skipped = pending = 0
    for c in candidates:
        candidate = dict(c)
        live_key = candidate.pop("live_key", None)
        eid = candidate["entity_id"]
        if (eid, candidate["idempotency_key"]) in existing or (
            live_key and (eid, live_key) in existing
        ):
            skipped += 1
            continue
        if dry_run:
            created += 1  # would-create
            existing.add((eid, candidate["idempotency_key"]))  # intra-run dedup
            continue
        row = await record_event(db, **candidate)
        if row is None:
            skipped += 1  # raced/duplicate — record_event dropped it
            continue
        created += 1
        pending += 1
        if pending >= BATCH_SIZE:
            await db.commit()
            pending = 0
    if not dry_run and pending:
        await db.commit()
    return created, skipped


async def run_backfill(
    db: AsyncSession,
    *,
    days: int = 90,
    entity_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, dict[str, int]]:
    """Backfill all five sources into workspace_events on ``db``.

    Returns per-source counts: {source: {"created": n, "skipped": m}}.
    ``dry_run`` computes counts without writing. Importable for tests /
    admin tooling; the CLI ``main`` below owns engine bootstrapping.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    builders = (
        ("tasks", _task_candidates),
        ("scheduled_job_runs", _scheduled_run_candidates),
        ("workflow_runs", _workflow_run_candidates),
        ("approval_requests", _approval_candidates),
        ("goal_measurements", _goal_measurement_candidates),
    )
    summary: dict[str, dict[str, int]] = {}
    prefix = "DRY " if dry_run else ""
    for name, builder in builders:
        candidates = await builder(db, cutoff=cutoff, entity_id=entity_id)
        created, skipped = await _emit_candidates(db, candidates, dry_run=dry_run)
        summary[name] = {"created": created, "skipped": skipped}
        logger.info(
            "%s%s: %d created, %d skipped (duplicate)", prefix, name, created, skipped,
        )
    return summary


async def main(*, days: int, entity_id: str | None, dry_run: bool) -> int:
    from packages.core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as db:
            summary = await run_backfill(
                db, days=days, entity_id=entity_id, dry_run=dry_run,
            )
    finally:
        await engine.dispose()

    total_created = sum(s["created"] for s in summary.values())
    total_skipped = sum(s["skipped"] for s in summary.values())
    logger.info(
        "%stotal: %d created, %d skipped", "DRY " if dry_run else "",
        total_created, total_skipped,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90, help="backfill window in days (default 90)")
    parser.add_argument("--entity", help="restrict to a single entity id")
    parser.add_argument("--dry-run", action="store_true", help="report only, no DB writes")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(days=args.days, entity_id=args.entity, dry_run=args.dry_run)))
