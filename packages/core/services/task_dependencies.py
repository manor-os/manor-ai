"""Task dependency helpers for workspace work waves.

These helpers keep task-to-task handoff explicit without adding a schema
migration: dependency ids and predecessor outputs live in ``Task.details``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Task
from packages.core.services.task_state_machine import TERMINAL_STATUSES

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def unique_dependency_ids(value: Any) -> list[str]:
    """Return normalized dependency task ids while preserving order."""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def dependency_ids_from_details(details: Any) -> list[str]:
    return unique_dependency_ids(_as_dict(details).get("depends_on_task_ids"))


async def dependency_status(
    db: AsyncSession,
    *,
    entity_id: str,
    dependency_ids: list[str],
) -> tuple[str, dict[str, str]]:
    """Return ``completed`` / ``blocked`` / ``waiting`` for dependency ids."""
    dep_ids = unique_dependency_ids(dependency_ids)
    if not dep_ids:
        return "completed", {}

    rows = list((await db.execute(
        select(Task.id, Task.status).where(
            Task.entity_id == entity_id,
            Task.id.in_(dep_ids),
        )
    )).all())
    statuses = {task_id: status for task_id, status in rows}
    if any(dep_id not in statuses for dep_id in dep_ids):
        return "blocked", statuses
    if any(statuses.get(dep_id) in {"failed", "cancelled"} for dep_id in dep_ids):
        return "blocked", statuses
    if all(statuses.get(dep_id) == "completed" for dep_id in dep_ids):
        return "completed", statuses
    return "waiting", statuses


async def build_dependency_outputs(
    db: AsyncSession,
    *,
    entity_id: str,
    dependency_ids: list[str],
) -> list[dict[str, Any]]:
    """Build compact predecessor output summaries for planner/runner prompts."""
    dep_ids = unique_dependency_ids(dependency_ids)
    if not dep_ids:
        return []

    rows = list((await db.execute(
        select(Task).where(
            Task.entity_id == entity_id,
            Task.id.in_(dep_ids),
        )
    )).scalars().all())
    by_id = {row.id: row for row in rows}
    outputs: list[dict[str, Any]] = []
    for dep_id in dep_ids:
        dep = by_id.get(dep_id)
        if dep is None:
            outputs.append({"task_id": dep_id, "status": "missing"})
            continue
        try:
            from packages.core.services.task_execution_reconcile import reconcile_task_from_latest_completed_plan

            await reconcile_task_from_latest_completed_plan(db, dep)
        except Exception:
            logger.debug("Dependency output reconciliation skipped for task %s", dep_id, exc_info=True)
        actual = _as_dict(dep.actual_output)
        entry: dict[str, Any] = {
            "task_id": dep.id,
            "task_title": dep.title,
            "status": dep.status,
        }
        summary = _output_summary(actual)
        if summary:
            entry["result_summary"] = summary
        files = _collect_output_files(actual)
        if files:
            entry["files"] = files[:20]
        plan_id = actual.get("plan_id")
        if plan_id:
            entry["plan_id"] = plan_id
        outputs.append(_json_safe(entry))
    return outputs


async def details_with_dependency_state(
    db: AsyncSession,
    task: Task,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach current dependency status and completed predecessor outputs."""
    merged = dict(details or task.details or {})
    dep_ids = dependency_ids_from_details(merged)
    if not dep_ids:
        merged.pop("dependency_status", None)
        merged.pop("dependency_statuses", None)
        return merged

    gate_status, statuses = await dependency_status(
        db,
        entity_id=task.entity_id,
        dependency_ids=dep_ids,
    )
    merged["dependency_status"] = gate_status
    merged["dependency_statuses"] = statuses
    merged["dependency_checked_at"] = _utcnow().isoformat()
    if gate_status == "completed":
        merged["dep_outputs"] = await build_dependency_outputs(
            db,
            entity_id=task.entity_id,
            dependency_ids=dep_ids,
        )
        merged["dependency_released_at"] = _utcnow().isoformat()
    return _json_safe(merged)


async def release_or_block_dependents(
    db: AsyncSession,
    task: Task,
    *,
    trigger_source: str,
) -> list[str]:
    """Release pending dependents when predecessors finish.

    If a predecessor fails/cancels, dependent pending tasks are moved to
    ``blocked`` so the operator can decide whether to retry or modify them.
    """
    if not task.workspace_id or task.status not in TERMINAL_STATUSES:
        return []

    candidates = list((await db.execute(
        select(Task).where(
            Task.entity_id == task.entity_id,
            Task.workspace_id == task.workspace_id,
            Task.status.in_(["pending", "on_hold", "blocked"]),
            Task.details["depends_on_task_ids"].astext.isnot(None),
        )
    )).scalars().all())
    if not candidates:
        return []

    from packages.core.services.task_service import update_task

    changed: list[str] = []
    for candidate in candidates:
        details = _as_dict(candidate.details)
        dep_ids = dependency_ids_from_details(details)
        if task.id not in dep_ids:
            continue
        gate_status, statuses = await dependency_status(
            db,
            entity_id=candidate.entity_id,
            dependency_ids=dep_ids,
        )
        details["dependency_statuses"] = statuses
        details["dependency_checked_at"] = _utcnow().isoformat()
        details["dependency_trigger_source"] = trigger_source

        if gate_status == "completed":
            details["dependency_status"] = "completed"
            details["dep_outputs"] = await build_dependency_outputs(
                db,
                entity_id=candidate.entity_id,
                dependency_ids=dep_ids,
            )
            details["dependency_released_at"] = _utcnow().isoformat()
            await update_task(
                db,
                candidate.id,
                candidate.entity_id,
                status="in_progress",
                details=_json_safe(details),
            )
            changed.append(candidate.id)
        elif gate_status == "blocked" and candidate.status != "blocked":
            details["dependency_status"] = "blocked"
            details["dependency_blocked_at"] = _utcnow().isoformat()
            await update_task(
                db,
                candidate.id,
                candidate.entity_id,
                status="blocked",
                details=_json_safe(details),
            )
            changed.append(candidate.id)
    if changed:
        logger.info(
            "Released/blocked %d dependent task(s) after %s via %s",
            len(changed),
            task.id,
            trigger_source,
        )
    return changed


def _output_summary(actual: dict[str, Any]) -> str:
    for key in ("summary", "result_summary", "response", "message", "result", "text", "value"):
        value = actual.get(key)
        if value:
            return str(value)[:1200]
    steps = actual.get("steps")
    if isinstance(steps, list):
        bits = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            text = (
                step.get("result_summary")
                or step.get("summary")
                or step.get("response")
                or step.get("message")
                or step.get("text")
                or step.get("value")
            )
            if text:
                bits.append(str(text))
        if bits:
            return "\n".join(bits)[:1200]
    return ""


def _collect_output_files(actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect file refs from both task-level and step-level outputs.

    PlanExecutor normally mirrors step artifacts onto ``actual_output.files``.
    Legacy TaskRunner paths and older rows may only have ``steps[].files``;
    dependents should still receive those artifacts as predecessor inputs.
    """
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        key = str(
            raw.get("document_id")
            or raw.get("fs_path")
            or raw.get("path")
            or raw.get("url")
            or raw.get("file_url")
            or raw.get("name")
            or raw.get("filename")
            or raw
        )
        if key in seen:
            return
        seen.add(key)
        files.append(_json_safe(raw))

    for item in actual.get("files") or []:
        add(item)
    for step in actual.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("files") or []:
            add(item)
    return files
