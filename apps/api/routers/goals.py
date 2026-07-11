"""Goals API — CRUD over persistent business goals.

The old goals router (/api/v1/goals) returned ad-hoc GoalRun rows.
That table is gone; this rewrite exposes the persistent ``Goal`` model
(packages/core/models/goal.py) so the dashboard, the Strategist, and
the agent's create_goal tool all read/write the same shape.

Measurement APIs (POST /goals/{id}/measurements,
GET /goals/{id}/measurements) live on the same router for convenience.
ExecutionPlan endpoints land on a separate /api/v1/plans router so
the URL doesn't pretend a Plan is a Goal.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.goals import service as goal_service
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal, GoalTaskLink
from packages.core.models.task import Task
from packages.core.models.user import User


router = APIRouter(prefix="/api/v1/goals", tags=["goals"])
logger = logging.getLogger(__name__)


# ── Schemas ────────────────────────────────────────────────────────────

class GoalResponse(BaseModel):
    id: str
    entity_id: str
    workspace_id: Optional[str]
    title: str
    description: Optional[str]
    metric_key: str
    target_value: float
    baseline_value: Optional[float]
    current_value: Optional[float]
    current_value_updated_at: Optional[datetime]
    deadline: Optional[date]
    pace_status: Optional[str]
    pace_computed_at: Optional[datetime]
    status: str
    measurement_source: Optional[dict]
    measurement_cadence: Optional[str]
    priority: int
    achieved_at: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]
    linked_task_ids: list[str] = Field(default_factory=list)
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    task_progress_fraction: Optional[float] = None
    estimated_impact_total: Optional[float] = None
    actual_impact_total: Optional[float] = None


class GoalCreateRequest(BaseModel):
    title: Optional[str] = None
    goal: Optional[str] = None
    goal_id: Optional[str] = None
    description: Optional[str] = None
    metric_key: str = "completion"
    target_value: float = 1.0
    baseline_value: Optional[float] = None
    deadline: Optional[date] = None
    workspace_id: Optional[str] = None
    measurement_source: Optional[dict] = None
    measurement_cadence: Optional[str] = None
    priority: int = Field(default=3, ge=1, le=5)
    context: Optional[dict] = None
    steps: Optional[list[dict]] = None


class GoalUpdateRequest(BaseModel):
    title: Optional[str] = None
    goal: Optional[str] = None
    description: Optional[str] = None
    target_value: Optional[float] = None
    deadline: Optional[date] = None
    status: Optional[str] = None
    measurement_source: Optional[dict] = None
    measurement_cadence: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=1, le=5)
    current_step_id: Optional[str] = None
    current_agent_id: Optional[str] = None
    steps: Optional[list[dict]] = None


class MeasurementResponse(BaseModel):
    measured_at: datetime
    value: float
    source: Optional[str]
    meta: Optional[dict]


class MeasurementCreateRequest(BaseModel):
    value: float
    source: str = "manual"
    note: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────

def _to_response(g: Goal, link_summary: Optional[dict[str, Any]] = None) -> GoalResponse:
    link_summary = link_summary or {}
    return GoalResponse(
        id=g.id,
        entity_id=g.entity_id,
        workspace_id=g.workspace_id,
        title=g.title,
        description=g.description,
        metric_key=g.metric_key,
        target_value=float(g.target_value),
        baseline_value=float(g.baseline_value) if g.baseline_value is not None else None,
        current_value=float(g.current_value) if g.current_value is not None else None,
        current_value_updated_at=g.current_value_updated_at,
        deadline=g.deadline,
        pace_status=g.pace_status,
        pace_computed_at=g.pace_computed_at,
        status=g.status,
        measurement_source=g.measurement_source,
        measurement_cadence=g.measurement_cadence,
        priority=g.priority,
        achieved_at=g.achieved_at,
        created_at=g.created_at,
        updated_at=g.updated_at,
        linked_task_ids=list(link_summary.get("linked_task_ids") or []),
        task_status_counts=dict(link_summary.get("task_status_counts") or {}),
        task_progress_fraction=link_summary.get("task_progress_fraction"),
        estimated_impact_total=link_summary.get("estimated_impact_total"),
        actual_impact_total=link_summary.get("actual_impact_total"),
    )


_LEGACY_GOAL_RUNS: dict[str, dict[str, Any]] = {}
_LEGACY_STEP_RUNS: dict[str, list[dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_goal_for_user(goal_id: str, user: User) -> dict[str, Any] | None:
    row = _LEGACY_GOAL_RUNS.get(goal_id)
    if not row or row.get("entity_id") != user.entity_id:
        return None
    return row


def _legacy_goal_response(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "steps": list(row.get("steps") or [])}


async def _goal_link_summaries(
    db: AsyncSession,
    *,
    entity_id: str,
    goal_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Return compact task linkage/progress summaries for goal canvas wiring."""
    if not goal_ids:
        return {}

    rows = (await db.execute(
        select(GoalTaskLink, Task)
        .outerjoin(Task, Task.id == GoalTaskLink.task_id)
        .where(GoalTaskLink.goal_id.in_(goal_ids))
        .order_by(GoalTaskLink.created_at.asc(), GoalTaskLink.task_id.asc())
    )).all()

    summaries: dict[str, dict[str, Any]] = {}
    for link, task in rows:
        if task is not None and task.entity_id != entity_id:
            continue
        if task is not None:
            try:
                from packages.core.services.task_execution_reconcile import reconcile_task_from_latest_completed_plan

                await reconcile_task_from_latest_completed_plan(db, task)
            except Exception:
                logger.debug("Goal task reconciliation skipped for task %s", task.id, exc_info=True)
        summary = summaries.setdefault(
            link.goal_id,
            {
                "linked_task_ids": [],
                "task_status_counts": {},
                "_estimated": 0.0,
                "_estimated_seen": False,
                "_actual": 0.0,
                "_actual_seen": False,
            },
        )
        if link.task_id and link.task_id not in summary["linked_task_ids"]:
            summary["linked_task_ids"].append(link.task_id)
        status = task.status if task is not None else None
        status_key = str(status or "missing")
        summary["task_status_counts"][status_key] = summary["task_status_counts"].get(status_key, 0) + 1
        if link.estimated_impact is not None:
            summary["_estimated"] += float(link.estimated_impact)
            summary["_estimated_seen"] = True
        if link.actual_impact is not None:
            summary["_actual"] += float(link.actual_impact)
            summary["_actual_seen"] = True

    terminal_success = {"completed"}
    for summary in summaries.values():
        total = len(summary["linked_task_ids"])
        completed = sum(
            count
            for status, count in summary["task_status_counts"].items()
            if status in terminal_success
        )
        summary["task_progress_fraction"] = (completed / total) if total else None
        summary["estimated_impact_total"] = summary["_estimated"] if summary["_estimated_seen"] else None
        summary["actual_impact_total"] = summary["_actual"] if summary["_actual_seen"] else None
        summary.pop("_estimated", None)
        summary.pop("_estimated_seen", None)
        summary.pop("_actual", None)
        summary.pop("_actual_seen", None)

    return summaries


# ── Routes ─────────────────────────────────────────────────────────────

@router.get("")
async def list_goals(
    workspace_id: Optional[str] = None,
    status: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    legacy = [
        _legacy_goal_response(row)
        for row in _LEGACY_GOAL_RUNS.values()
        if row.get("entity_id") == user.entity_id
        and (not status or row.get("status") == status)
    ]
    if legacy:
        legacy.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return {"items": legacy, "total": len(legacy)}

    rows = await goal_service.list_goals(
        db, user.entity_id, workspace_id=workspace_id, status=status,
    )
    if _LEGACY_GOAL_RUNS and not rows:
        return {"items": [], "total": 0}
    summaries = await _goal_link_summaries(
        db,
        entity_id=user.entity_id,
        goal_ids=[g.id for g in rows],
    )
    return [_to_response(g, summaries.get(g.id)) for g in rows]


@router.post("", status_code=201)
async def create_goal(
    req: GoalCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.goal is not None or req.goal_id is not None or req.steps is not None:
        now = _now_iso()
        run_id = generate_ulid()
        steps = [
            {
                "id": step.get("id") or generate_ulid(),
                "name": step.get("name") or step.get("step_name") or "",
                "status": step.get("status") or "pending",
            }
            for step in (req.steps or [])
            if isinstance(step, dict)
        ]
        row = {
            "id": run_id,
            "entity_id": user.entity_id,
            "user_id": user.id,
            "goal": req.goal or req.title or "",
            "goal_id": req.goal_id or run_id,
            "status": "pending",
            "plan_version": 1,
            "retry_count": 0,
            "context": req.context or {},
            "steps": steps,
            "current_step_id": None,
            "current_agent_id": None,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        _LEGACY_GOAL_RUNS[run_id] = row
        _LEGACY_STEP_RUNS[run_id] = []
        return _legacy_goal_response(row)

    if not req.title:
        raise HTTPException(422, "title is required")
    goal = await goal_service.create_goal(
        db,
        entity_id=user.entity_id,
        title=req.title,
        metric_key=req.metric_key,
        target_value=req.target_value,
        workspace_id=req.workspace_id,
        description=req.description,
        baseline_value=req.baseline_value,
        deadline=req.deadline,
        measurement_source=req.measurement_source,
        measurement_cadence=req.measurement_cadence,
        priority=req.priority,
    )
    await db.commit()
    await db.refresh(goal)
    return _to_response(goal)


@router.get("/{goal_id}")
async def get_goal(
    goal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    legacy = _legacy_goal_for_user(goal_id, user)
    if legacy:
        return _legacy_goal_response(legacy)
    goal = await goal_service.get_goal(db, goal_id, user.entity_id)
    if not goal:
        raise HTTPException(404, "goal not found")
    summaries = await _goal_link_summaries(
        db,
        entity_id=user.entity_id,
        goal_ids=[goal.id],
    )
    return _to_response(goal, summaries.get(goal.id))


@router.put("/{goal_id}")
async def update_goal(
    goal_id: str,
    req: GoalUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    legacy = _legacy_goal_for_user(goal_id, user)
    if legacy:
        updates = req.model_dump(exclude_unset=True)
        for key in ("goal", "status", "current_step_id", "current_agent_id", "steps"):
            if key in updates:
                legacy[key] = updates[key]
        legacy["updated_at"] = _now_iso()
        if legacy.get("status") in {"completed", "cancelled", "failed"} and not legacy.get("completed_at"):
            legacy["completed_at"] = _now_iso()
        return _legacy_goal_response(legacy)

    payload = req.model_dump(exclude_unset=True)
    goal = await goal_service.update_goal(db, goal_id, user.entity_id, **payload)
    if not goal:
        raise HTTPException(404, "goal not found")
    await db.commit()
    await db.refresh(goal)
    return _to_response(goal)


@router.post("/{goal_id}/cancel")
async def cancel_goal_run(
    goal_id: str,
    user: User = Depends(get_current_user),
):
    legacy = _legacy_goal_for_user(goal_id, user)
    if not legacy:
        raise HTTPException(404, "goal not found")
    legacy["status"] = "cancelled"
    legacy["completed_at"] = _now_iso()
    legacy["updated_at"] = _now_iso()
    return _legacy_goal_response(legacy)


@router.post("/{goal_id}/steps", status_code=201)
async def create_step_run(
    goal_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
):
    legacy = _legacy_goal_for_user(goal_id, user)
    if not legacy:
        raise HTTPException(404, "goal not found")
    step = {
        "id": generate_ulid(),
        "goal_run_id": goal_id,
        "created_at": _now_iso(),
        **body,
    }
    _LEGACY_STEP_RUNS.setdefault(goal_id, []).append(step)
    return step


@router.get("/{goal_id}/steps")
async def list_step_runs(
    goal_id: str,
    user: User = Depends(get_current_user),
):
    legacy = _legacy_goal_for_user(goal_id, user)
    if not legacy:
        raise HTTPException(404, "goal not found")
    return list(_LEGACY_STEP_RUNS.get(goal_id) or [])


@router.delete("/{goal_id}", status_code=204)
async def delete_goal(
    goal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await goal_service.delete_goal(db, goal_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "goal not found")
    await db.commit()


@router.get("/{goal_id}/measurements", response_model=list[MeasurementResponse])
async def list_measurements(
    goal_id: str,
    limit: int = 100,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    goal = await goal_service.get_goal(db, goal_id, user.entity_id)
    if not goal:
        raise HTTPException(404, "goal not found")
    rows = await goal_service.list_measurements(db, goal_id, limit=limit)
    return [
        MeasurementResponse(
            measured_at=m.measured_at,
            value=float(m.value),
            source=m.source,
            meta=m.meta,
        )
        for m in rows
    ]


@router.post("/{goal_id}/measurements", status_code=201)
async def record_measurement(
    goal_id: str,
    req: MeasurementCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    goal = await goal_service.get_goal(db, goal_id, user.entity_id)
    if not goal:
        raise HTTPException(404, "goal not found")

    measurement = await goal_service.record_measurement(
        db, goal,
        value=req.value, source=req.source,
        meta={"note": req.note} if req.note else None,
    )
    await db.commit()
    return {
        "measured_at": measurement.measured_at.isoformat(),
        "value": float(measurement.value),
        "pace_status": goal.pace_status,
    }
