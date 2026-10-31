"""Manual execution lineage for the Workspace automatic-running switch.

These receipts exempt only a concrete Task/Plan from automatic pause. They do
not grant tools, approve actions, or change the original automation provenance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, exists, func, or_

from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task


MANUAL_EXECUTION_KEY = "_manual_execution"
_AUTOMATIC_TASK_KEYS = (
    "strategist_review_id", "scheduled_job_id", "scheduled_run_id", "automation_origin",
)
# The Task whose run created this one (host provenance, from the Runtime
# envelope). Protected like the receipts above, but not an automation marker:
# it never changes manual/automatic admission.
ORIGIN_TASK_KEY = "origin_task_id"
TASK_EXECUTION_ORIGIN_KEYS = frozenset({MANUAL_EXECUTION_KEY, ORIGIN_TASK_KEY, *_AUTOMATIC_TASK_KEYS})


def record_task_origin(task: Any, origin_task_id: str | None) -> None:
    """Link a Task created inside another Task's run to that Task.

    ``details.origin_task_id`` is host-only (model detail patches cannot set or
    clear it); ``parent_task_id`` is the user-facing link and is only
    defaulted, never overwritten.
    """
    if not origin_task_id or origin_task_id == task.id:
        return
    task.details = {**(task.details or {}), ORIGIN_TASK_KEY: origin_task_id}
    if not getattr(task, "parent_task_id", None):
        task.parent_task_id = origin_task_id


def _manual_receipt(container: Any) -> bool:
    receipt = container.get(MANUAL_EXECUTION_KEY) if isinstance(container, dict) else None
    return bool(
        isinstance(receipt, dict)
        and isinstance(receipt.get("user_id"), str) and receipt["user_id"].strip()
        and isinstance(receipt.get("action"), str) and receipt["action"].strip()
    )


def task_has_manual_execution(task: Any) -> bool:
    if task is None:
        return False
    details = task.details if isinstance(getattr(task, "details", None), dict) else {}
    if _manual_receipt(details):
        return True
    # Preserve human-created Tasks that predate the explicit receipt. A
    # scheduler's user attribution alone never makes its Task human-triggered.
    return bool(getattr(task, "creator_id", None)) and not any(
        details.get(key) is not None for key in _AUTOMATIC_TASK_KEYS
    )


def _receipt(*, user_id: str, action: str) -> dict[str, str]:
    if not user_id.strip() or not action.strip():
        raise ValueError("Manual execution requires a user and action")
    return {"user_id": user_id, "action": action, "requested_at": datetime.now(timezone.utc).isoformat()}


def record_manual_task_execution(task: Any, *, user_id: str, action: str) -> None:
    task.details = {**(task.details or {}), MANUAL_EXECUTION_KEY: _receipt(user_id=user_id, action=action)}


def record_manual_plan_execution(plan: Any, *, user_id: str, action: str) -> None:
    plan.dispatcher_state = {
        **(getattr(plan, "dispatcher_state", None) or {}),
        MANUAL_EXECUTION_KEY: _receipt(user_id=user_id, action=action),
    }


def _manual_receipt_predicate(container):
    receipt = container[MANUAL_EXECUTION_KEY]
    return and_(
        func.jsonb_typeof(receipt) == "object",
        func.jsonb_typeof(receipt["user_id"]) == "string",
        func.btrim(receipt["user_id"].astext) != "",
        func.jsonb_typeof(receipt["action"]) == "string",
        func.btrim(receipt["action"].astext) != "",
    )


def task_manual_execution_predicate(task=Task):
    """SQL counterpart used before the Dispatcher's bounded candidate LIMIT."""
    return or_(
        _manual_receipt_predicate(task.details),
        and_(
            task.creator_id.is_not(None),
            task.creator_id != "",
            *(task.details[key].astext.is_(None) for key in _AUTOMATIC_TASK_KEYS),
        ),
    )


def plan_manual_execution_predicate(plan=ExecutionPlan):
    return or_(
        _manual_receipt_predicate(plan.dispatcher_state),
        exists().where(
            Task.id == plan.task_id,
            Task.entity_id == plan.entity_id,
            Task.workspace_id == plan.workspace_id,
            task_manual_execution_predicate(),
        ).correlate(plan),
    )
