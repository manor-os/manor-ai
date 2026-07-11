from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.hitl_requests import user_visible_hitl_action_text
from packages.core.services.hitl_options import normalize_approval_choice


def parse_hitl_action(message: str) -> tuple[str, str] | None:
    """Parse the approval-card response sent by the web client."""

    try:
        data = json.loads(message)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    hitl_id = str(data.get("hitl_id") or "").strip()
    action = normalize_approval_choice(data.get("action"))
    if not hitl_id or not action:
        return None
    return hitl_id, action


async def resolve_chat_approval_turn(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    message: str,
) -> tuple[str | None, str | None, bool]:
    """Return (replacement_llm_message, saved_text, save_user_message)."""

    from packages.core.ai.runtime.approval_service import (
        resolve_pending_runtime_approval_from_reply,
        resolve_runtime_approval_message,
    )
    from packages.core.services.ai_file_permissions import (
        resolve_file_approval_message,
        resolve_pending_file_approval_from_reply,
    )
    from packages.core.services.workspace_operation_service import (
        resolve_workspace_operation_review_message,
    )

    if hitl := parse_hitl_action(message):
        hitl_id, hitl_action = hitl
        replacement = await resolve_file_approval_message(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
        )
        if replacement:
            return replacement, user_visible_hitl_action_text(hitl_action), True
        replacement = await resolve_runtime_approval_message(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
        )
        if replacement:
            return replacement, user_visible_hitl_action_text(hitl_action), True
        replacement = await resolve_workspace_operation_review_message(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
        )
        if replacement:
            return replacement, user_visible_hitl_action_text(hitl_action), True

    replacement = await resolve_pending_file_approval_from_reply(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=message,
    )
    if replacement:
        return replacement, None, True
    replacement = await resolve_pending_runtime_approval_from_reply(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=message,
    )
    if replacement:
        return replacement, None, True
    return None, None, True


async def cancel_chat_approvals(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_ids: list[str] | None = None,
    reason: str = "request_stopped",
) -> dict[str, int]:
    """Cancel pending file and runtime approvals for a chat conversation."""

    from packages.core.ai.runtime.approval_service import cancel_pending_runtime_approvals
    from packages.core.services.ai_file_permissions import cancel_pending_file_approvals

    file_cancelled = await cancel_pending_file_approvals(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=hitl_ids,
        reason=reason,
    )
    runtime_cancelled = await cancel_pending_runtime_approvals(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=hitl_ids,
        reason=reason,
    )
    return {
        "cancelled": file_cancelled + runtime_cancelled,
        "file_cancelled": file_cancelled,
        "runtime_cancelled": runtime_cancelled,
    }
