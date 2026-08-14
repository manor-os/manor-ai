"""Runtime-owned facade for answering task blockers from workspace chat.

The tool layer (``workspace_agent_tools.py``) stays a thin adapter; the
actual resolution + resume logic lives in
``packages.core.services.task_blockers`` and is shared verbatim with the
chat card's resolve endpoint.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def runtime_workspace_answer_task_blocker_action(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Answer (or refuse) one open task blocker on the user's behalf.

    The tool relays a decision the user just expressed in chat — it is not
    itself gated (``workspace_resolve_hitl`` precedent): the human already
    decided; this only routes that decision back to the paused task.
    """
    del conversation_id  # blockers are workspace-scoped, not conversation-scoped

    raw = dict(params or {})
    request_id = str(raw.get("request_id") or "").strip()
    if not entity_id or not workspace_id:
        return _dumps({"error": "answer_task_blocker requires workspace context"})
    if not user_id:
        return _dumps({"error": "answer_task_blocker requires a user_id"})
    if not request_id:
        return _dumps({"error": "request_id is required"})

    answer = raw.get("answer")
    answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else None
    confirm = raw.get("confirm")
    refuse = bool(raw.get("refuse"))

    from packages.core.database import async_session
    from packages.core.services.task_blockers import answer_task_blocker

    async with async_session() as db:
        try:
            result = await answer_task_blocker(
                db,
                entity_id=entity_id,
                user_id=user_id,
                request_id=request_id,
                answer=str(answer) if answer is not None else None,
                answers=answers,
                confirm=bool(confirm) if confirm is not None else None,
                refuse=refuse,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("answer_task_blocker failed for request %s", request_id)
            return _dumps({
                "error": "answer_task_blocker_failed",
                "request_id": request_id,
            })
    return _dumps(result)
