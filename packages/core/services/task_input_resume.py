"""Park a legacy (non-plan) agent task on a ``request_input`` question and
resume it with the answer.

The legacy task loop keeps no transcript between runs, so "resume with the
answer" here means: the task goes ``waiting_on_customer`` with the question
recorded (``details["_hitl_question"]`` + an ``ai_hitl_requested`` log whose
``meta.question`` the task page renders), and the answer re-dispatches the
runner with both the question and the answer rendered into the prompt
(:func:`runtime_task_user_prompt`).

Every answer surface — the task page, a task comment, the workspace-chat card,
``answer_task_blocker`` from chat — funnels through :func:`apply_task_input_answer`
so they cannot drift.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.task import Task

logger = logging.getLogger(__name__)

#: ``task.details`` key holding the question the agent asked (request_input payload).
TASK_INPUT_QUESTION_KEY = "_hitl_question"


def task_input_question(task: Any) -> dict[str, Any] | None:
    details = getattr(task, "details", None)
    if not isinstance(details, dict):
        return None
    question = details.get(TASK_INPUT_QUESTION_KEY)
    if isinstance(question, dict) and str(question.get("question") or "").strip():
        return question
    return None


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


async def legacy_task_can_resume_input(
    db: AsyncSession,
    task: Task,
    *,
    agent_id: str | None = None,
) -> bool:
    """Only an exact factory-selected legacy Agent resumes through its loop."""
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.models.execution import ExecutionPlan
    from packages.core.services.task_execution import resolve_task_execution
    from packages.core.services.task_service import task_executing_agent_id

    dispatch = await resolve_task_execution(db, task)
    if dispatch.runtime_kind is not TaskRuntimeKind.LEGACY_AGENT:
        return False
    effective_agent_id = (
        agent_id
        or await task_executing_agent_id(db, task)
        or MANOR_AGENT_ID
    )
    if dispatch.target_id != effective_agent_id:
        return False

    if task.owner_subscription_id or task.owner_service_key:
        return False
    has_plan_origin = (await db.execute(
        select(ExecutionPlan.id).where(
            ExecutionPlan.task_id == task.id,
            ExecutionPlan.entity_id == task.entity_id,
        ).limit(1)
    )).scalar_one_or_none() is not None
    return not has_plan_origin


async def apply_task_input_answer(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    user_label: str,
    response_text: str,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Record the answer on a ``waiting_on_customer`` legacy task and flip it
    back to ``pending``. Returns ``{"resumed": bool, "dispatch_agent_id": str|None}``;
    the caller commits and dispatches ``run_agent_task``.

    A Task agent loop parked on the question takes the answer instead
    (``task_agent_loop_hitl``); its ``dispatch_agent_id`` is a marker that
    ``dispatch_task_input_resume`` treats as already dispatched.
    """
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.services.task_chat_hitl import resolve_task_input_hitl
    from packages.core.services.task_service import (
        add_task_log,
        task_executing_agent_id,
        update_task,
    )

    from packages.core.services.task_agent_loop_hitl import apply_task_agent_loop_input_answer

    # A Task agent loop parked on this question resumes in place: the answer
    # is its pending call's result and its wake is staged right here. It
    # re-checks the Task's status under the Task lock itself.
    loop_answer = await apply_task_agent_loop_input_answer(
        db,
        task=task,
        user_id=user_id,
        user_label=user_label,
        response_text=response_text,
        payload=dict(payload or {}),
    )
    if loop_answer is not None:
        return loop_answer
    if task.status != TaskStatus.WAITING_ON_CUSTOMER:
        return {"resumed": False, "dispatch_agent_id": None}
    dispatch_id = await task_executing_agent_id(db, task) or MANOR_AGENT_ID
    if not await legacy_task_can_resume_input(
        db,
        task,
        agent_id=dispatch_id,
    ):
        return {"resumed": False, "dispatch_agent_id": None}

    clean_payload = dict(payload or {})
    details = dict(task.details or {})
    details["_hitl_response"] = response_text
    details["_hitl_payload"] = clean_payload
    details["_hitl_responded_by"] = user_label
    await update_task(db, task.id, task.entity_id, status="pending", details=details)
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_HITL_RESUMED,
        f"Human input received: {response_text[:300]}" if response_text else "Human input received.",
        actor=TaskActor.USER,
        created_by=user_label,
        metadata={
            "response": response_text,
            "choice": clean_payload.get("choice"),
            "fields": clean_payload.get("fields") or {},
            "submitted_by": user_id,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "mode": "agent",
        },
    )
    await resolve_task_input_hitl(
        db, task=task, user_id=user_id, answer=response_text, payload=clean_payload,
    )
    return {"resumed": True, "dispatch_agent_id": dispatch_id}


def dispatch_task_input_resume(task_id: str, agent_id: str | None) -> bool:
    """Re-run the task after the answer is committed. Returns dispatch success."""
    from packages.core.services.task_agent_loop_hitl import TASK_AGENT_LOOP_INPUT_DISPATCH

    if agent_id == TASK_AGENT_LOOP_INPUT_DISPATCH:
        # A Task agent loop's wake was committed with the answer; the outbox
        # delivers it. There is no Agent run to dispatch.
        return True
    try:
        from packages.core.tasks.ai_tasks import run_agent_task

        run_agent_task.delay(task_id, agent_id)
        return True
    except Exception as exc:  # noqa: BLE001 — the caller reports, never raises
        logger.warning("Agent HITL resume dispatch failed: task=%s error=%s", task_id, exc)
        return False


__all__ = [
    "TASK_INPUT_QUESTION_KEY",
    "apply_task_input_answer",
    "dispatch_task_input_resume",
    "legacy_task_can_resume_input",
    "task_input_log_meta",
    "task_input_question",
]
