from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


RUNTIME_APPROVALS_KEY = "runtime_approvals"

__all__ = [
    "RUNTIME_APPROVALS_KEY",
    "load_runtime_approval_conversation",
    "mark_runtime_hitl_request_resolved",
    "mark_runtime_hitl_requests_resolved",
    "runtime_approval_exists",
    "runtime_approval_now_iso",
    "runtime_approval_workspace_context",
    "runtime_approvals",
    "set_runtime_approvals",
]


def runtime_approval_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_approvals(conv: Any) -> dict[str, dict[str, Any]]:
    meta = getattr(conv, "meta", None) or {}
    approvals = meta.get(RUNTIME_APPROVALS_KEY) or {}
    return dict(approvals) if isinstance(approvals, dict) else {}


def set_runtime_approvals(conv: Any, approvals: dict[str, dict[str, Any]]) -> None:
    meta = dict(getattr(conv, "meta", None) or {})
    meta[RUNTIME_APPROVALS_KEY] = approvals
    conv.meta = meta
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(conv, "meta")
    except Exception:
        # Plain objects in unit tests do not have SQLAlchemy instrumentation.
        pass


async def load_runtime_approval_conversation(
    db: Any,
    conversation_id: str,
    entity_id: str,
) -> Any | None:
    from sqlalchemy import select
    from packages.core.models.task import Conversation

    return (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
    )).scalar_one_or_none()


async def runtime_approval_workspace_context(db: Any, conv: Any) -> dict[str, str] | None:
    workspace_id = getattr(conv, "workspace_id", None)
    if not workspace_id:
        return None
    from sqlalchemy import select
    from packages.core.models.workspace import Workspace

    name = None
    try:
        row = (await db.execute(
            select(Workspace.id, Workspace.name).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == getattr(conv, "entity_id", None),
            )
        )).first()
        if row:
            name = row.name
    except Exception:
        name = None
    return {
        "id": workspace_id,
        "name": name or workspace_id,
    }


async def runtime_approval_exists(
    db: Any,
    *,
    conversation_id: str | None,
    entity_id: str,
    hitl_id: str,
) -> bool:
    if not conversation_id or not hitl_id:
        return False
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return False
    return hitl_id in runtime_approvals(conv)


async def mark_runtime_hitl_request_resolved(
    db: Any,
    *,
    conversation_id: str,
    hitl_id: str,
    choice: str,
) -> None:
    try:
        from packages.core.services.hitl_requests import mark_hitl_request_resolved

        await mark_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice=choice,
        )
    except Exception:
        pass


async def mark_runtime_hitl_requests_resolved(
    db: Any,
    *,
    conversation_id: str,
    hitl_ids: Iterable[str],
    choice: str,
) -> None:
    for hitl_id in hitl_ids:
        await mark_runtime_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice=choice,
        )
