from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Conversation, Message
from packages.core.services.hitl_requests import user_visible_hitl_action_text
from packages.core.services.hitl_options import normalize_approval_choice


CHAT_TURN_CANCEL_GENERATION_KEY = "chat_turn_cancel_generation"


def _chat_turn_cancel_generation(meta: object) -> int:
    if not isinstance(meta, dict):
        return 0
    try:
        return max(0, int(meta.get(CHAT_TURN_CANCEL_GENERATION_KEY) or 0))
    except (TypeError, ValueError):
        return 0


async def get_chat_turn_cancel_generation(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
) -> int:
    """Read the cross-process cancellation generation for one conversation."""

    meta = (await db.execute(
        select(Conversation.meta).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    return _chat_turn_cancel_generation(meta)


async def request_chat_turn_cancellation(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    reason: str = "request_stopped",
) -> int:
    """Publish an explicit stop request visible to detached Chat workers.

    A generation avoids a sticky boolean: a later turn captures the newest
    value and is allowed to run normally.  The JSONB field keeps this change
    schema-free and visible across API workers/processes.
    """

    conversation = (await db.execute(
        select(Conversation).with_for_update().where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if conversation is None:
        return 0

    meta = dict(conversation.meta or {})
    generation = _chat_turn_cancel_generation(meta) + 1
    meta[CHAT_TURN_CANCEL_GENERATION_KEY] = generation
    meta["chat_turn_cancel_requested_at"] = datetime.now(timezone.utc).isoformat()
    meta["chat_turn_cancel_reason"] = reason or "request_stopped"
    conversation.meta = meta
    flag_modified(conversation, "meta")
    await db.flush()
    return generation


async def chat_turn_cancellation_requested(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    generation: int,
) -> bool:
    """Return whether an explicit cancel happened after a Chat turn began."""

    current = await get_chat_turn_cancel_generation(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
    )
    return current > max(0, int(generation or 0))


def parse_hitl_action(message: str) -> tuple[str, str, dict | None] | None:
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
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else None
    return hitl_id, action, payload


def _hitl_revision_request(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    review = payload.get("review")
    if isinstance(review, str):
        value = review.strip()
        return value or None
    if isinstance(review, dict):
        value = str(review.get("revision_request") or "").strip()
        return value or None
    return None


async def chat_hitl_action_is_pending(
    db: AsyncSession,
    *,
    conversation_id: str | None,
    entity_id: str,
    message: str,
) -> bool:
    """Return whether a structured Chat action targets a live HITL card.

    Chat approval clicks must remain usable when the account has no remaining
    AI credits: resolving a human decision is not an LLM request.  The exact
    pending-card lookup prevents arbitrary JSON messages from bypassing the
    normal Chat plan gate.
    """

    parsed = parse_hitl_action(message)
    if parsed is None or not conversation_id:
        return False
    hitl_id, _action, _payload = parsed
    rows = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conversation_id,
            Conversation.entity_id == entity_id,
            Message.resolved_at.is_(None),
            Message.pending_action.isnot(None),
        )
    )).scalars().all()
    for row in rows:
        if row.id == hitl_id:
            return True
        meta = row.meta if isinstance(row.meta, dict) else {}
        if any(
            isinstance(request, dict) and str(request.get("id") or "") == hitl_id
            for request in meta.get("hitl_requests") or []
        ):
            return True
    return False


async def register_chat_provider_approval(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    request: dict,
) -> dict | None:
    """Register a normalized provider approval through the chat adapter."""

    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )

    return await register_provider_runtime_approval(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )


async def resolve_chat_approval_turn(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    message: str,
) -> tuple[str | None, str | None, bool, dict | None]:
    """Return replacement text, saved text, save flag, and runtime metadata."""

    from packages.core.ai.runtime.approval_service import (
        resolve_pending_runtime_approval_turn_from_reply,
        resolve_runtime_approval_turn,
    )
    from packages.core.services.ai_file_permissions import (
        resolve_file_approval_message,
        resolve_pending_file_approval_from_reply,
    )
    from packages.core.services.workspace_operation_service import (
        resolve_workspace_operation_review_message,
    )

    if hitl := parse_hitl_action(message):
        hitl_id, hitl_action, hitl_payload = hitl
        replacement = await resolve_file_approval_message(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
        )
        if replacement:
            return replacement, user_visible_hitl_action_text(hitl_action), True, None
        runtime_resolution = await resolve_runtime_approval_turn(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
            revision_request=_hitl_revision_request(hitl_payload),
        )
        if runtime_resolution:
            return (
                runtime_resolution.message,
                user_visible_hitl_action_text(hitl_action),
                True,
                runtime_resolution.runtime_metadata,
            )
        replacement = await resolve_workspace_operation_review_message(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
        )
        if replacement:
            return replacement, user_visible_hitl_action_text(hitl_action), True, None
        from packages.core.services.workflow_chat_approvals import (
            resolve_chat_workflow_wait,
        )

        replacement = await resolve_chat_workflow_wait(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            hitl_id=hitl_id,
            action=hitl_action,
            submitted_review=(hitl_payload or {}).get("review"),
        )
        if replacement:
            return (
                replacement,
                user_visible_hitl_action_text(hitl_action),
                True,
                {"approval_kind": "workflow"},
            )

    replacement = await resolve_pending_file_approval_from_reply(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=message,
    )
    if replacement:
        return replacement, None, True, None
    runtime_resolution = await resolve_pending_runtime_approval_turn_from_reply(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=message,
    )
    if runtime_resolution:
        return (
            runtime_resolution.message,
            None,
            True,
            runtime_resolution.runtime_metadata,
        )
    return None, None, True, None


async def cancel_chat_approvals(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_ids: list[str] | None = None,
    reason: str = "request_stopped",
    cancel_turn: bool = False,
) -> dict[str, int]:
    """Cancel approvals, optionally stopping the active Chat turn as well."""

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
    cancel_generation = 0
    if cancel_turn:
        cancel_generation = await request_chat_turn_cancellation(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            reason=reason,
        )
    return {
        "cancelled": file_cancelled + runtime_cancelled,
        "file_cancelled": file_cancelled,
        "runtime_cancelled": runtime_cancelled,
        "cancel_requested": int(bool(cancel_generation)),
        "cancel_generation": cancel_generation,
    }
