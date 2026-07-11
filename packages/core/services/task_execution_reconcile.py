"""Best-effort reconciliation between task rows and their latest plan.

The executor owns the canonical write path. This module is a defensive
read-time repair layer for older rows or race windows where a completed plan
was recorded but the task's business status/output did not catch up.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import TaskLog
from packages.core.services.task_state_machine import apply_task_status_transition

logger = logging.getLogger(__name__)


_PLAN_COMPLETION_RECONCILABLE_TASK_STATUSES = {"pending", "in_progress", "waiting_on_customer", "completed"}


def _step_result_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")[:500]
    return str(result.get("text") or result.get("value") or result.get("summary") or "")[:500]


def _actual_output_from_steps(plan: ExecutionPlan, steps: list[ExecutionStep]) -> dict[str, Any]:
    # Keep artifact extraction identical to the executor so task output,
    # dependency handoff, and UI display agree on file references.
    from packages.core.plans.executor import _artifact_refs_from_result, _dedupe_task_artifact_refs

    step_summaries: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    for step in steps:
        entry: dict[str, Any] = {
            "key": step.step_key,
            "kind": step.kind,
            "status": step.step_status,
        }
        if step.result:
            entry["result_summary"] = _step_result_summary(step.result)
            if isinstance(step.result, dict):
                refs = _artifact_refs_from_result(step.result, step_key=step.step_key)
                if refs:
                    entry["files"] = refs
                    all_files.extend(refs)
                if step.result.get("document_id"):
                    entry["document_id"] = step.result["document_id"]
                if step.result.get("fs_path"):
                    entry["fs_path"] = step.result["fs_path"]
        if step.error:
            entry["error"] = {
                "type": step.error.get("type", "unknown"),
                "message": str(step.error.get("message", ""))[:300],
            }
        step_summaries.append(entry)

    return {
        "plan_id": plan.id,
        "plan_status": plan.status,
        "steps": step_summaries,
        "files": _dedupe_task_artifact_refs(all_files) if all_files else None,
        "reconciled_from_plan": True,
    }


def _has_duplicate_file_refs(actual: dict[str, Any]) -> bool:
    from packages.core.plans.executor import _artifact_ref_identity

    seen: set[tuple[str, str]] = set()
    for item in actual.get("files") or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("type") or ""), _artifact_ref_identity(item))
        if key in seen:
            return True
        seen.add(key)
    return False


async def _has_open_supervisor_input_request(db: AsyncSession, *, task_id: str, plan_id: str) -> bool:
    """Return True when a completed plan deliberately left the task waiting.

    The plan row can be ``completed`` even when the supervisor determined that
    the business task still needs operator input, for example when a user-visible
    artifact was expected but no file/document reference was saved. In that
    case read-time reconciliation must not "helpfully" flip the task back to
    completed just because the execution plan ended.
    """
    rows = list((await db.execute(
        select(TaskLog).where(
            TaskLog.task_id == task_id,
            TaskLog.log_type.in_(("ai_hitl_requested", "ai_hitl_reminder", "ai_hitl_resumed")),
        ).order_by(TaskLog.created_at)
    )).scalars().all())

    open_request = False
    for log in rows:
        meta = log.meta or {}
        meta_plan_id = str(meta.get("plan_id") or "")
        if meta_plan_id and meta_plan_id != plan_id:
            continue
        if log.log_type == "ai_hitl_resumed":
            open_request = False
            continue
        if log.log_type in {"ai_hitl_requested", "ai_hitl_reminder"} and (
            meta.get("verdict") == "needs_human"
            or bool(meta.get("artifact_required"))
        ):
            open_request = True
    return open_request


async def reconcile_task_from_latest_completed_plan(db: AsyncSession, task: Any) -> bool:
    """Repair stale task output/status from the latest completed plan.

    Returns True when the ORM task object was mutated. This intentionally only
    uses completed plans. A failed plan may legitimately leave the task in
    waiting_on_customer if the supervisor requested human input.
    """
    task_id = getattr(task, "id", None)
    entity_id = getattr(task, "entity_id", None)
    if not task_id or not entity_id:
        return False

    task_status = str(getattr(task, "status", "") or "")
    if task_status not in _PLAN_COMPLETION_RECONCILABLE_TASK_STATUSES:
        return False

    plan = (await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.task_id == task_id,
            ExecutionPlan.status == "completed",
        ).order_by(ExecutionPlan.completed_at.desc().nullslast(), ExecutionPlan.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not plan:
        return False

    actual = getattr(task, "actual_output", None) if isinstance(getattr(task, "actual_output", None), dict) else {}
    if (
        task_status == "waiting_on_customer"
        and (
            actual.get("supervisor_verdict") == "needs_human"
            or await _has_open_supervisor_input_request(db, task_id=task_id, plan_id=plan.id)
        )
    ):
        return False

    if (
        actual.get("plan_id") == plan.id
        and actual.get("plan_status") == "completed"
        and task_status == "completed"
        and not _has_duplicate_file_refs(actual)
    ):
        return False

    steps = list((await db.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        .order_by(ExecutionStep.created_at)
    )).scalars().all())
    task.actual_output = _actual_output_from_steps(plan, steps)

    if task_status != "completed":
        try:
            apply_task_status_transition(task, "completed")
        except Exception:
            logger.debug(
                "Could not reconcile task %s status %s from completed plan %s",
                task_id,
                task_status,
                plan.id,
                exc_info=True,
            )
    return True
