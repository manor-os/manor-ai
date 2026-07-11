"""Reconcile stale workspace work batches before Strategist reviews.

The Strategist should not pile new scheduled work on top of an active batch,
but an active batch can also get stuck forever if a task never reaches a
terminal state. This module keeps that distinction explicit:

* fresh active batches still block scheduled reviews;
* completed-but-not-closed batches are closed;
* stale batches are marked ``stalled`` and surfaced to Strategist so the next
  proposal can repair, retry, or ask the operator to close the old work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Task
from packages.core.models.workspace import Workspace, WorkspaceWorkBatch
from packages.core.services.task_state_machine import TERMINAL_STATUSES


DEFAULT_STALE_AFTER_HOURS: dict[str, float] = {
    "blocked": 6.0,
    "pending": 24.0,
    "in_progress": 24.0,
    "proposed": 72.0,
}
DEFAULT_OPEN_TASK_STALE_AFTER_HOURS = 24.0


def build_work_batch_reconciliation(
    batch: Any,
    tasks: Iterable[Any],
    *,
    now: datetime | None = None,
    stale_after_hours: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return a serializable reconciliation snapshot for one work batch."""
    now = _aware(now or datetime.now(timezone.utc))
    thresholds = dict(DEFAULT_STALE_AFTER_HOURS)
    thresholds.update(stale_after_hours or {})

    task_ids = _unique_strings(getattr(batch, "task_ids", []) or [])
    rows_by_id = {str(getattr(task, "id", "")): task for task in tasks if getattr(task, "id", None)}
    statuses: dict[str, str] = {}
    open_task_ids: list[str] = []
    stale_tasks: list[dict[str, Any]] = []
    missing_task_ids = [task_id for task_id in task_ids if task_id not in rows_by_id]

    for task_id in task_ids:
        task = rows_by_id.get(task_id)
        if task is None:
            continue
        status = str(getattr(task, "status", "") or "unknown")
        statuses[task_id] = status
        if status in TERMINAL_STATUSES:
            continue
        open_task_ids.append(task_id)
        reference_time = _task_reference_time(task)
        age_hours = _age_hours(reference_time, now)
        threshold = thresholds.get(status, DEFAULT_OPEN_TASK_STALE_AFTER_HOURS)
        if age_hours >= threshold:
            stale_tasks.append({
                "task_id": task_id,
                "title": str(getattr(task, "title", "") or ""),
                "status": status,
                "age_hours": round(age_hours, 2),
                "stale_after_hours": threshold,
                "last_activity_at": reference_time.isoformat() if reference_time else None,
                "owner_service_key": getattr(task, "owner_service_key", None),
            })

    terminal_count = len(task_ids) - len(open_task_ids) - len(missing_task_ids)
    all_terminal = not task_ids or terminal_count == len(task_ids)
    stale = bool(stale_tasks or missing_task_ids)
    status = "completed" if all_terminal else "stalled" if stale else "active"
    return {
        "batch_id": str(getattr(batch, "id", "") or ""),
        "status": status,
        "source_kind": getattr(batch, "source_kind", None),
        "summary": getattr(batch, "summary", None),
        "task_ids": task_ids,
        "open_task_ids": open_task_ids,
        "stale_task_ids": [task["task_id"] for task in stale_tasks],
        "missing_task_ids": missing_task_ids,
        "terminal_count": terminal_count,
        "total_count": len(task_ids),
        "all_terminal": all_terminal,
        "stale": stale,
        "stale_tasks": stale_tasks,
        "statuses": statuses,
        "checked_at": now.isoformat(),
    }


async def reconcile_active_work_batches(
    db: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
    stale_after_hours: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Close completed active batches and mark stale batches as stalled."""
    now = _aware(now or datetime.now(timezone.utc))
    batches = list((await db.execute(
        select(WorkspaceWorkBatch)
        .where(
            WorkspaceWorkBatch.workspace_id == workspace.id,
            WorkspaceWorkBatch.entity_id == workspace.entity_id,
            WorkspaceWorkBatch.status == "active",
        )
        .order_by(WorkspaceWorkBatch.created_at.asc())
    )).scalars().all())
    if not batches:
        return []

    results: list[dict[str, Any]] = []
    for batch in batches:
        task_ids = _unique_strings(batch.task_ids or [])
        tasks = []
        if task_ids:
            tasks = list((await db.execute(
                select(Task).where(
                    Task.workspace_id == workspace.id,
                    Task.entity_id == workspace.entity_id,
                    Task.id.in_(task_ids),
                )
            )).scalars().all())
        snapshot = build_work_batch_reconciliation(
            batch,
            tasks,
            now=now,
            stale_after_hours=stale_after_hours,
        )
        details = dict(batch.details or {})
        details["last_reconciliation"] = snapshot
        if snapshot["all_terminal"]:
            batch.status = "completed"
            batch.completed_at = now
            details["completed_at"] = now.isoformat()
            await _record_batch_activity(
                db,
                workspace,
                batch,
                event_type="workspace_work_batch.completed",
                summary=f"Workspace task wave completed during reconciliation: {batch.summary or batch.id}",
                snapshot=snapshot,
            )
        elif snapshot["stale"]:
            batch.status = "stalled"
            details["stalled_at"] = now.isoformat()
            details["stall_reason"] = _stall_reason(snapshot)
            await _record_batch_activity(
                db,
                workspace,
                batch,
                event_type="workspace_work_batch.stalled",
                summary=f"Workspace task wave stalled: {batch.summary or batch.id}",
                snapshot=snapshot,
            )
        batch.details = details
        results.append(snapshot)

    await db.flush()
    return results


def stale_reconciliation_results(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only stalled/stale reconciliation snapshots for prompts."""
    return [result for result in results if result.get("stale")]


async def _record_batch_activity(
    db: AsyncSession,
    workspace: Workspace,
    batch: WorkspaceWorkBatch,
    *,
    event_type: str,
    summary: str,
    snapshot: dict[str, Any],
) -> None:
    try:
        from packages.core.services.workspace_service import record_activity

        await record_activity(
            db,
            workspace.id,
            workspace.entity_id,
            event_type=event_type,
            summary=summary,
            details={
                "batch_id": batch.id,
                "source_kind": batch.source_kind,
                "task_ids": list(snapshot.get("task_ids") or []),
                "open_task_ids": list(snapshot.get("open_task_ids") or []),
                "stale_task_ids": list(snapshot.get("stale_task_ids") or []),
                "missing_task_ids": list(snapshot.get("missing_task_ids") or []),
            },
            user_id=batch.created_by_user_id,
        )
    except Exception:
        pass


def _stall_reason(snapshot: dict[str, Any]) -> str:
    missing = snapshot.get("missing_task_ids") or []
    stale = snapshot.get("stale_task_ids") or []
    if missing and stale:
        return "missing_and_stale_tasks"
    if missing:
        return "missing_task_rows"
    return "stale_open_tasks"


def _task_reference_time(task: Any) -> datetime | None:
    for attr in ("updated_at", "started_at", "created_at"):
        value = getattr(task, attr, None)
        if isinstance(value, datetime):
            return _aware(value)
    return None


def _age_hours(value: datetime | None, now: datetime) -> float:
    if value is None:
        return DEFAULT_OPEN_TASK_STALE_AFTER_HOURS
    return max(0.0, (now - _aware(value)).total_seconds() / 3600.0)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unique_strings(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
