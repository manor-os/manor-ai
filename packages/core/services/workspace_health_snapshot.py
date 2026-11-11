"""Current health per Workspace (design §1.5, §3.2).

``refresh_workspace_health`` re-rolls today into ``metrics_workspace_daily``,
then for every in-scope Workspace sums its daily rows for the 24h / 7d rates,
adds the live counts (stalled work, missed schedules, approval backlog,
readiness gaps, service coverage), derives the status with the shared rule
and upserts its snapshot. ``status_since`` moves only when the status changes.

Only today is re-rolled: yesterday and earlier are the daily beat's job, so
the 7d window reads whatever that beat last wrote.

A part that fails to evaluate is marked unknown for that refresh instead of
failing it; the derive rule never matches an unknown part. Every part runs
in its own savepoint because a failed statement aborts the Postgres
transaction, and with it every part after it and the final COMMIT.

An unknown part never changes a status: a Workspace that already has a
snapshot keeps its status, status_since and reasons, plus a
``<part>_unknown`` reason per failed part, while the parts that did
evaluate still update their columns. Without this one failed query would
flip every Workspace to idle and reset status_since fleet-wide. A
Workspace seen for the first time gets the status of what is known.

Every query here is one grouped statement over all scoped Workspaces. The
one exception is the Blueprint setup gate, which is evaluated per Workspace
by nature.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Collection, Optional, TypeVar

from sqlalchemy import (
    ARRAY, String, all_, and_, case, delete, exists, func, literal, or_, select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.freshness import BLUEPRINT_SETTINGS_KEY
from packages.core.constants.approvals import ApprovalStatus
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_health import MetricsWorkspaceDaily, WorkspaceHealthSnapshot
from packages.core.services.workspace_health_metrics import (
    APPROVAL_BACKLOG_AFTER,
    MISSED_FIRE_AFTER,
    SUCCEEDED,
    TASK_OUTCOMES,
    HealthInputs,
    derive_status,
    ids_param,
    judged_rate,
    stalled_task_clause,
    stalled_workflow_run_clause,
    statuses_with,
    success_rate,
)
from packages.core.services.workspace_health_rollup import (
    MODE_SIMULATION,
    RUN_SURFACES,
    SURFACE_TASK,
    SURFACE_WORKFLOW,
    SURFACE_WORKSPACE,
    rollup_workspace_day,
    workspace_dimensions,
)
from packages.core.services.workspace_readiness import (
    evaluate_workspace_blocking_setup,
    iter_declared_service_keys,
)

logger = logging.getLogger(__name__)

SCOPE_ACTIVITY_DAYS = 30
"""A Workspace with a Task or Workflow run created this recently is watched."""
COVERAGE_WINDOW = timedelta(days=30)
"""A service counts as used when one of its Tasks succeeded this recently."""
_UPSERT_CHUNK = 500
"""Rows per upsert statement, well under asyncpg's bind-parameter limit."""

_COUNT_COLUMNS = (
    "succeeded", "failed", "cancelled", "retried", "user_cancelled",
    "reviews_accepted", "reviews_rejected", "reviews_ignored",
    "feedback_up", "feedback_down", "proposals_accepted", "proposals_rejected",
)
_STALL_SURFACES = (SURFACE_TASK, SURFACE_WORKFLOW)
_UNKNOWN = {"unknown": True}

T = TypeVar("T")
Sums = dict[str, dict[str, Counter]]
"""{workspace_id: {surface: Counter(column → 7d sum, "<column>_24h" → 24h sum)}}"""


async def refresh_workspace_health(
    db: AsyncSession, *, now: Optional[datetime] = None,
    workspace_ids: Optional[Collection[str]] = None,
) -> dict:
    """Re-roll today, then rewrite every in-scope Workspace's snapshot and
    drop the snapshots of Workspaces no longer in scope. The caller commits.

    ``workspace_ids`` limits both the refresh and the out-of-scope delete to
    those Workspaces; production callers leave it unset."""
    now = now or datetime.now(timezone.utc)
    limit = sorted(set(workspace_ids)) if workspace_ids is not None else None
    await rollup_workspace_day(db, day=now.date(), now=now)

    workspaces = await _scoped_workspaces(db, now, limit)
    ids = [ws.id for ws in workspaces]
    failed: set[str] = set()

    async def part(name: str, fn: Callable[..., Awaitable[T]], *args: Any) -> Optional[T]:
        try:
            async with db.begin_nested():
                return await fn(*args)
        except Exception:
            logger.warning("workspace_health part failed: %s", name, exc_info=True)
            failed.add(name)
            return None

    sums = await part("daily_sums", _daily_sums, db, ids, now)
    stalled = {
        SURFACE_TASK: await part("stalled_tasks", _stalled_tasks, db, ids, now),
        SURFACE_WORKFLOW: await part("stalled_workflow_runs", _stalled_workflow_runs, db, ids, now),
    }
    missed = await part("missed_schedules", _missed_schedules, db, ids, now)
    backlog = await part("approval_backlog", _approval_backlog, db, ids, now)
    covered = await part("service_coverage", _covered_services, db, ids, now)
    previous = await _previous_snapshots(db, ids)
    failed_queries = sorted(failed)

    rows: list[dict[str, Any]] = []
    by_status: Counter = Counter()
    for ws in workspaces:
        prev = previous.get(ws.id) or {}
        unknown = list(failed_queries)
        notes: list[str] = []
        surfaces = _surfaces(sums, stalled, ws.id)
        quality = _quality(sums.get(ws.id, {})) if sums is not None else {}
        gaps = await _readiness_gaps(db, ws)
        if gaps is None:
            failed.add("readiness")
            unknown.append("readiness")
        coverage = _service_coverage(ws, covered)
        if coverage is _COVERAGE_UNKNOWN:
            # A malformed declaration is the Workspace's own state, not a
            # failed query: it is noted but does not hold the status.
            coverage = None
            notes.append("service_coverage_unknown")
        blueprint_key, blueprint_version, mode = workspace_dimensions(ws)
        live_since = _live_since(ws, mode)
        # derive_status sees only what is known; an unknown part never matches.
        inputs = HealthInputs(
            readiness_gaps=gaps or [],
            surfaces=surfaces,
            quality=quality,
            missed_schedules=(missed or {}).get(ws.id, 0),
            pending_approvals_over_24h=(backlog or {}).get(ws.id, 0),
            service_coverage=coverage,
            live_since=live_since,
            stalled_over_threshold=sum(s.get("stalled") or 0 for s in surfaces.values()),
            judged_runs_7d=sum(s.get("judged_7d", 0) for s in surfaces.values()),
            now=now,
        )
        health = derive_status(inputs)
        status, reasons = health.status, health.reasons
        if unknown and prev:
            # Hold the last status (and with it status_since) and the
            # reasons that explained it until every part evaluates again.
            status = prev["status"]
            reasons = [r for r in prev["reasons"] or [] if not _is_unknown_reason(r)]
        by_status[status] += 1

        def known(value: Any, column: str, fresh: Any, prev: dict = prev) -> Any:
            """``fresh`` when the part evaluated, else the stored value."""
            return fresh if value is not None else prev.get(column, fresh)

        rows.append({
            "workspace_id": ws.id, "entity_id": ws.entity_id,
            "blueprint_key": blueprint_key, "blueprint_version": blueprint_version, "mode": mode,
            "status": status, "status_since": now,
            "reasons": reasons + [
                {"rule": f"{name}_unknown", "surface": None, "value": None}
                for name in [*unknown, *notes]
            ],
            "surfaces": surfaces,
            "quality": known(sums, "quality", quality),
            "readiness_gaps": known(gaps, "readiness_gaps", inputs.readiness_gaps),
            "missed_schedules": known(missed, "missed_schedules", inputs.missed_schedules),
            "pending_approvals_over_24h": known(
                backlog, "pending_approvals_over_24h", inputs.pending_approvals_over_24h,
            ),
            "service_coverage": known(covered, "service_coverage", coverage),
            "live_since": live_since, "refreshed_at": now,
        })

    await _upsert(db, rows)
    # The table lists exactly what is watched. A Workspace that comes back
    # into scope starts a fresh row, and with it a new status_since.
    stale = WorkspaceHealthSnapshot.workspace_id != all_(literal(ids, type_=ARRAY(String)))
    if limit is not None:
        stale = and_(stale, WorkspaceHealthSnapshot.workspace_id == ids_param(limit))
    await db.execute(delete(WorkspaceHealthSnapshot).where(stale))
    return {
        "workspaces": len(rows), "by_status": dict(by_status), "unknown_parts": sorted(failed),
    }


def _is_unknown_reason(reason: Any) -> bool:
    return isinstance(reason, dict) and str(reason.get("rule") or "").endswith("_unknown")


async def _previous_snapshots(db: AsyncSession, ids: list[str]) -> dict[str, dict[str, Any]]:
    """The stored snapshot of each scoped Workspace, as plain values (no
    ORM objects, so the upsert below cannot leave a stale identity map)."""
    if not ids:
        return {}
    S = WorkspaceHealthSnapshot
    return {
        r["workspace_id"]: dict(r)
        for r in (await db.execute(select(
            S.workspace_id, S.status, S.reasons, S.quality, S.readiness_gaps,
            S.missed_schedules, S.pending_approvals_over_24h, S.service_coverage,
        ).where(S.workspace_id == ids_param(ids)))).mappings()
    }


# ── Scope ────────────────────────────────────────────────────────────

async def _scoped_workspaces(
    db: AsyncSession, now: datetime, limit: Optional[list[str]] = None,
) -> list[Workspace]:
    """Active Workspaces with recent work or an enabled schedule, by id.

    Readiness alone does not bring a Workspace into scope: finding those
    would mean evaluating the setup gate of every Workspace every hour."""
    since = now - timedelta(days=SCOPE_ACTIVITY_DAYS)
    limited = [Workspace.id == ids_param(limit)] if limit is not None else []
    return list((await db.execute(select(Workspace).where(
        *limited,
        Workspace.status == "active",
        Workspace.deleted_at.is_(None),
        or_(
            exists().where(Task.workspace_id == Workspace.id, Task.created_at >= since),
            exists().where(
                WorkflowRun.workspace_id == Workspace.id, WorkflowRun.created_at >= since,
            ),
            exists().where(
                ScheduledJob.workspace_id == Workspace.id, ScheduledJob.enabled.is_(True),
            ),
        ),
    ).order_by(Workspace.id))).scalars().all())


# ── Rates from the daily rows ────────────────────────────────────────

async def _daily_sums(db: AsyncSession, ids: list[str], now: datetime) -> Sums:
    """7d = the last seven UTC days including today; 24h ≈ yesterday plus
    today so far. Daily rows have no hour, so "24h" spans 24–48 hours
    depending on the time of day — close enough for a rate, and it never
    reads as empty just after midnight."""
    if not ids:
        return {}
    week_start = (now - timedelta(days=6)).date()
    yesterday = (now - timedelta(days=1)).date()
    D = MetricsWorkspaceDaily
    columns = [func.sum(getattr(D, c)).label(c) for c in _COUNT_COLUMNS] + [
        func.sum(case((D.day >= yesterday, getattr(D, c)), else_=0)).label(f"{c}_24h")
        for c in ("succeeded", "failed")
    ]
    sums: Sums = defaultdict(dict)
    for r in (await db.execute(select(D.workspace_id, D.surface, *columns).where(
        D.workspace_id == ids_param(ids), D.day >= week_start,
    ).group_by(D.workspace_id, D.surface))).mappings():
        sums[r["workspace_id"]][r["surface"]] = Counter({
            key: int(value or 0) for key, value in r.items()
            if key not in ("workspace_id", "surface")
        })
    return dict(sums)


def _surfaces(
    sums: Optional[Sums], stalled: dict[str, Optional[dict[str, int]]], workspace_id: str,
) -> dict[str, dict[str, Any]]:
    """Rates are unknown when the daily sums are; a stall count that could
    not be read is ``None`` without touching the rates beside it."""
    out: dict[str, dict[str, Any]] = {}
    for surface in RUN_SURFACES:
        if sums is None:
            out[surface] = dict(_UNKNOWN)
        else:
            c = sums.get(workspace_id, {}).get(surface, Counter())
            out[surface] = {
                "success_rate_24h": success_rate(c["succeeded_24h"], c["failed_24h"]),
                "success_rate_7d": success_rate(c["succeeded"], c["failed"]),
                "judged_7d": c["succeeded"] + c["failed"],
            }
        if surface in _STALL_SURFACES:
            stall = stalled.get(surface)
            out[surface]["stalled"] = stall.get(workspace_id, 0) if stall is not None else None
    return out


def _quality(by_surface: dict[str, Counter]) -> dict[str, Optional[float]]:
    ws = by_surface.get(SURFACE_WORKSPACE, Counter())
    task = by_surface.get(SURFACE_TASK, Counter())
    flow = by_surface.get(SURFACE_WORKFLOW, Counter())
    accepted, rejected = ws["reviews_accepted"], ws["reviews_rejected"]

    def finished(c: Counter) -> int:
        return c["succeeded"] + c["failed"] + c["cancelled"]

    return {
        "acceptance_rate": judged_rate(accepted, accepted + rejected),
        "ignored_rate": judged_rate(ws["reviews_ignored"], accepted + rejected + ws["reviews_ignored"]),
        "feedback_rate": judged_rate(ws["feedback_up"], ws["feedback_up"] + ws["feedback_down"]),
        "proposal_acceptance_rate": judged_rate(
            ws["proposals_accepted"], ws["proposals_accepted"] + ws["proposals_rejected"],
        ),
        "retry_rate": judged_rate(task["retried"], task["succeeded"] + task["failed"]),
        "user_cancel_rate": judged_rate(
            task["user_cancelled"] + flow["user_cancelled"], finished(task) + finished(flow),
        ),
    }


# ── Live counts ──────────────────────────────────────────────────────

async def _grouped_count(db: AsyncSession, column, *where) -> dict[str, int]:
    return {
        r[0]: int(r[1])
        for r in (await db.execute(
            select(column, func.count()).where(*where).group_by(column)
        )).all()
    }


async def _stalled_tasks(db: AsyncSession, ids: list[str], now: datetime) -> dict[str, int]:
    if not ids:
        return {}
    return await _grouped_count(
        db, Task.workspace_id, Task.workspace_id == ids_param(ids), stalled_task_clause(now),
    )


async def _stalled_workflow_runs(
    db: AsyncSession, ids: list[str], now: datetime,
) -> dict[str, int]:
    if not ids:
        return {}
    return await _grouped_count(
        db, WorkflowRun.workspace_id, WorkflowRun.workspace_id == ids_param(ids),
        stalled_workflow_run_clause(now),
    )


async def _missed_schedules(db: AsyncSession, ids: list[str], now: datetime) -> dict[str, int]:
    """Enabled jobs whose next fire is more than ``MISSED_FIRE_AFTER`` past,
    whatever their cadence."""
    if not ids:
        return {}
    return await _grouped_count(
        db, ScheduledJob.workspace_id, ScheduledJob.workspace_id == ids_param(ids),
        ScheduledJob.enabled.is_(True), ScheduledJob.next_run_at < now - MISSED_FIRE_AFTER,
    )


async def _approval_backlog(db: AsyncSession, ids: list[str], now: datetime) -> dict[str, int]:
    if not ids:
        return {}
    return await _grouped_count(
        db, HitlRequest.workspace_id, HitlRequest.workspace_id == ids_param(ids),
        HitlRequest.status == ApprovalStatus.PENDING.value,
        HitlRequest.created_at < now - APPROVAL_BACKLOG_AFTER,
    )


async def _covered_services(
    db: AsyncSession, ids: list[str], now: datetime,
) -> dict[str, set[str]]:
    """{workspace_id: service keys with a Task that succeeded recently}."""
    if not ids:
        return {}
    covered: dict[str, set[str]] = defaultdict(set)
    for r in (await db.execute(select(Task.workspace_id, Task.owner_service_key).where(
        Task.workspace_id == ids_param(ids),
        Task.owner_service_key.is_not(None),
        Task.status.in_(statuses_with(TASK_OUTCOMES, SUCCEEDED)),
        Task.completed_at >= now - COVERAGE_WINDOW,
    ).distinct())).all():
        covered[r.workspace_id].add(r.owner_service_key)
    return dict(covered)


_COVERAGE_UNKNOWN: Any = object()


def _service_coverage(ws: Workspace, covered: Optional[dict[str, set[str]]]) -> Any:
    """Share of declared services used recently; None when nothing is
    declared, ``_COVERAGE_UNKNOWN`` when the declaration is malformed."""
    if covered is None:
        return None
    try:
        declared = iter_declared_service_keys(
            ws.operating_model if isinstance(ws.operating_model, dict) else {},
        )
    except ValueError:
        logger.warning("workspace_health service keys unreadable for %s", ws.id, exc_info=True)
        return _COVERAGE_UNKNOWN
    if not declared:
        return None
    return round(len(declared & covered.get(ws.id, set())) / len(declared), 4)


async def _readiness_gaps(db: AsyncSession, ws: Workspace) -> Optional[list[str]]:
    """Keys of the failing Blueprint setup checks; None when the gate could
    not be evaluated. A Workspace with no gate has no gaps."""
    try:
        async with db.begin_nested():
            part = await evaluate_workspace_blocking_setup(db, ws)
    except Exception:
        logger.warning("workspace_health readiness failed for %s", ws.id, exc_info=True)
        return None
    if part is None or not part.blocks_work:
        return []
    incomplete = (part.details or {}).get("incomplete_checks") or []
    keys = [str(check.get("key") or "") for check in incomplete if isinstance(check, dict)]
    return [key for key in keys if key] or [part.missing_setup_key]


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _live_since(ws: Workspace, mode: str) -> Optional[datetime]:
    """When the Workspace started doing real work: its promotion out of
    simulation, else its live install, else its creation. None while it is
    still a simulation."""
    if mode == MODE_SIMULATION:
        return None
    settings = ws.settings if isinstance(ws.settings, dict) else {}
    blueprint = settings.get(BLUEPRINT_SETTINGS_KEY)
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    return (
        _parse_time(blueprint.get("promoted_at"))
        or _parse_time(blueprint.get("installed_at"))
        or _parse_time(ws.created_at)
    )


# ── Write ────────────────────────────────────────────────────────────

async def _upsert(db: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Rows arrive sorted by Workspace id, so concurrent refreshes take row
    locks in the same order."""
    S = WorkspaceHealthSnapshot
    for start in range(0, len(rows), _UPSERT_CHUNK):
        stmt = pg_insert(S).values(rows[start:start + _UPSERT_CHUNK])
        excluded = stmt.excluded
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[S.workspace_id],
            set_={
                **{
                    column: excluded[column] for column in rows[0]
                    if column not in ("workspace_id", "status_since")
                },
                "status_since": case(
                    (S.status == excluded.status, S.status_since),
                    else_=excluded.status_since,
                ),
            },
        ))
