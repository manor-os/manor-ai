"""Answer a Task's open ``request_input`` question.

A Task run (the Task agent loop) parks on the question: the Task goes
``waiting_on_customer`` with the question recorded (``details["_hitl_question"]``
+ an ``ai_hitl_requested`` log whose ``meta.question`` the task page renders),
and the answer becomes the parked call's result when the run resumes.

Every answer surface — the task page, a task comment, the workspace-chat card,
``answer_task_blocker`` from chat — funnels through :func:`apply_task_input_answer`
so they cannot drift.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Task

logger = logging.getLogger(__name__)

#: ``task.details`` key holding the question the agent asked (request_input payload).
TASK_INPUT_QUESTION_KEY = "_hitl_question"


def task_input_log_meta(question: dict[str, Any]) -> dict[str, Any]:
    """``ai_hitl_requested`` log meta: what TaskDetail / TaskRecoveryPanel read."""
    fields = question.get("fields") if isinstance(question.get("fields"), list) else []
    hitl_fields = [
        {
            "name": str(item.get("name") or ""),
            "label": str(item.get("label") or item.get("name") or ""),
            "type": str(item.get("type") or "text"),
            "required": bool(item.get("required", True)),
            **({"options": list(item["options"])} if item.get("options") else {}),
        }
        for item in fields
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ] or [{"name": "response", "type": "textarea", "required": True}]
    meta: dict[str, Any] = {
        "question": str(question.get("question") or ""),
        "hitl": {"kind": "human_input", "fields": hitl_fields},
    }
    if question.get("options"):
        meta["options"] = list(question["options"])
    if question.get("context"):
        meta["context"] = str(question["context"])
    return meta


async def apply_task_input_answer(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    user_label: str,
    response_text: str,
    payload: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Hand the answer to the Task run parked on the question.

    Returns ``{"resumed": bool, "dispatch_agent_id": str|None}``; the caller
    commits and calls :func:`dispatch_task_input_resume`. The run's wake is
    staged with the answer, so ``dispatch_agent_id`` is a marker that
    ``dispatch_task_input_resume`` treats as already dispatched. A surface
    that answers a card passes its ``request_id``: the answer then reaches
    exactly the run (the root loop or a delegated agent) that asked it.
    Without it (a comment, the Task page) it answers the earliest open
    question. A Task no run is waiting on is not resumed.
    """
    from packages.core.services.task_agent_loop_hitl import apply_task_agent_loop_input_answer

    # The loop re-checks the Task's status under the Task lock itself.
    loop_answer = await apply_task_agent_loop_input_answer(
        db,
        task=task,
        user_id=user_id,
        user_label=user_label,
        response_text=response_text,
        payload=dict(payload or {}),
        request_id=request_id,
    )
    if loop_answer is not None:
        return loop_answer
    return {"resumed": False, "dispatch_agent_id": None}


def dispatch_task_input_resume(task_id: str, agent_id: str | None) -> bool:
    """Whether the resumed run's wake is on its way (it was committed with the answer)."""
    from packages.core.services.task_agent_loop_hitl import TASK_AGENT_LOOP_INPUT_DISPATCH

    if agent_id == TASK_AGENT_LOOP_INPUT_DISPATCH:
        # A Task agent loop's wake was committed with the answer; the outbox
        # delivers it. There is no Agent run to dispatch.
        return True
    logger.warning("Task %s input resume has no waiting run (dispatch=%r)", task_id, agent_id)
    return False


__all__ = [
    "TASK_INPUT_QUESTION_KEY",
    "apply_task_input_answer",
    "dispatch_task_input_resume",
    "task_input_log_meta",
]
