from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.output_policy import (
    runtime_assistant_stream_error_content,
    runtime_assistant_stream_interrupted_content,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import AgentSubscription

logger = logging.getLogger(__name__)

STALE_STREAM_GRACE_MINUTES = 10

STREAM_MESSAGE_IDENTITY_META_KEYS = (
    "channel_type",
    "chat_id",
    "sender_id",
    "source_id",
    "session_id",
)


def assistant_stream_interrupted_meta(meta: dict | None = None) -> dict:
    updated_meta = dict(meta or {})
    updated_meta["stream_status"] = "interrupted"
    updated_meta["stream_interrupted"] = True
    return updated_meta


async def add_message(
    db: AsyncSession,
    conversation_id: str,
    *,
    role: str,
    content: str,
    tool_calls: dict | list | None = None,
    token_usage: dict | None = None,
    attachments: dict | list | None = None,
    author_subscription_id: str | None = None,
    meta: dict | None = None,
    message_kind: str = "text",
    pending_action: dict | None = None,
    refs: list[dict] | None = None,
) -> Message:
    """Persist a conversation message and notify workspace chat listeners."""

    author_kind = "agent" if role == "assistant" else "system" if role == "system" else "user"
    action = pending_action if isinstance(pending_action, dict) and pending_action.get("kind") else None
    msg = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        attachments=attachments,
        token_usage=token_usage,
        meta=meta or {},
        author_kind=author_kind,
        author_subscription_id=author_subscription_id if author_kind == "agent" else None,
        message_kind=message_kind,
        pending_action=action,
        refs=refs,
    )
    db.add(msg)
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=func.now())
    )
    await db.flush()

    await _publish_workspace_chat_message_event(
        db,
        conversation_id=conversation_id,
        message_id=msg.id,
        message_kind=msg.message_kind,
        author_kind=msg.author_kind,
        has_pending_action=action is not None,
    )
    return msg


async def _publish_workspace_chat_message_event(
    db: AsyncSession,
    *,
    conversation_id: str,
    message_id: str,
    message_kind: str,
    author_kind: str,
    has_pending_action: bool,
) -> None:
    """Notify other workspace clients that a chat message changed.

    Best-effort: looks up the conversation's workspace, and if it is a
    workspace conversation, publishes a ``workspace_chat_message`` event so
    every connected member's UI refetches. Non-workspace conversations are
    skipped (personal chat streams over SSE to its single owner)."""
    try:
        conv = (await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )).scalar_one_or_none()
        if not (conv and conv.workspace_id):
            return
        from packages.core.cache import _get_redis
        event_entity_id = conv.entity_id
        event_workspace_id = conv.workspace_id

        async def _push():
            r = await _get_redis()
            if r:
                await r.publish("manor:ws_broadcast", json.dumps({
                    "entity_id": event_entity_id,
                    "event": "workspace_chat_message",
                    "data": {
                        "workspace_id": event_workspace_id,
                        "message_id": message_id,
                        "message_kind": message_kind,
                        "author_kind": author_kind,
                        "has_pending_action": has_pending_action,
                    },
                }))

        asyncio.ensure_future(_push())
    except Exception:
        logger.debug("Failed to publish workspace chat message event", exc_info=True)


async def resolve_author_subscription_id(
    db: AsyncSession,
    *,
    entity_id: str | None,
    workspace_id: str | None,
    agent_id: str | None,
) -> str | None:
    """Find the workspace deployment row for an agent response."""

    if not entity_id or not workspace_id or not agent_id:
        return None
    sub = (await db.execute(
        select(AgentSubscription.id).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.agent_id == agent_id,
            AgentSubscription.status == "active",
        ).limit(1)
    )).scalar_one_or_none()
    return sub


def assistant_stream_started_content() -> str:
    return (
        "The assistant started this response and is still working. "
        "If this remains after a reload, the stream was interrupted before "
        "it could finish."
    )


async def create_assistant_stream_placeholder(
    db: AsyncSession,
    conversation_id: str,
    *,
    entity_id: str | None,
    workspace_id: str | None,
    agent_id: str | None,
    meta: dict | None = None,
) -> Message:
    """Create a durable assistant row before a long-running stream starts."""

    author_subscription_id = await resolve_author_subscription_id(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    return await add_message(
        db,
        conversation_id,
        role="assistant",
        content=assistant_stream_started_content(),
        author_subscription_id=author_subscription_id,
        meta={"stream_status": "running", **(meta or {})},
    )


async def save_or_update_assistant_stream_message(
    *,
    conversation_id: str,
    entity_id: str | None,
    workspace_id: str | None,
    agent_id: str | None,
    content: str,
    message_id: str | None = None,
    tool_calls: dict | list | None = None,
    token_usage: dict | None = None,
    attachments: dict | list | None = None,
    meta: dict | None = None,
    message_kind: str = "text",
    pending_action: dict | None = None,
) -> str | None:
    """Update the stream placeholder, or create a row if older callers lack one."""
    try:
        from packages.core.database import async_session as _session_factory

        async with _session_factory() as save_db:
            author_subscription_id = await resolve_author_subscription_id(
                save_db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
            )
            action = (
                pending_action
                if isinstance(pending_action, dict) and pending_action.get("kind")
                else None
            )
            if message_id:
                existing_meta = (await save_db.execute(
                    select(Message.meta).where(
                        Message.id == message_id,
                        Message.conversation_id == conversation_id,
                    )
                )).scalar_one_or_none()
                merged_meta = dict(meta or {})
                if isinstance(existing_meta, dict):
                    for key in STREAM_MESSAGE_IDENTITY_META_KEYS:
                        if key in existing_meta and key not in merged_meta:
                            merged_meta[key] = existing_meta[key]
                result = await save_db.execute(
                    update(Message)
                    .where(
                        Message.id == message_id,
                        Message.conversation_id == conversation_id,
                    )
                    .values(
                        content=content,
                        tool_calls=tool_calls,
                        attachments=attachments,
                        token_usage=token_usage,
                        meta=merged_meta,
                        author_kind="agent",
                        author_subscription_id=author_subscription_id,
                        message_kind=message_kind,
                        pending_action=action,
                    )
                )
                if result.rowcount:
                    await save_db.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .values(updated_at=func.now())
                    )
                    await save_db.commit()
                    # The placeholder's creation already broadcast a
                    # workspace_chat_message; mid-stream checkpoints would
                    # spam refetches, so only re-broadcast once the reply is
                    # finalized (terminal/absent stream_status). Without this,
                    # other members keep the empty placeholder until refresh.
                    if (meta or {}).get("stream_status") != "streaming":
                        await _publish_workspace_chat_message_event(
                            save_db,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            message_kind=message_kind,
                            author_kind="agent",
                            has_pending_action=action is not None,
                        )
                    return message_id

            msg = await add_message(
                save_db,
                conversation_id,
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                token_usage=token_usage,
                attachments=attachments,
                author_subscription_id=author_subscription_id,
                meta=meta,
                message_kind=message_kind,
                pending_action=action,
            )
            await save_db.commit()
            return msg.id
    except Exception as save_err:
        logger.error("Failed to save assistant stream message: %s", save_err)
        return None


async def save_assistant_stream_error_message(
    *,
    conversation_id: str,
    entity_id: str | None,
    workspace_id: str | None,
    agent_id: str | None,
    error_message: str,
    message_id: str | None = None,
    meta: dict | None = None,
) -> str | None:
    """Persist a streaming failure so workspace chat/history keeps it."""
    return await save_or_update_assistant_stream_message(
        conversation_id=conversation_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        message_id=message_id,
        content=runtime_assistant_stream_error_content(error_message),
        meta={
            "stream_status": "error",
            "stream_error": True,
            "error_message": error_message,
            **(meta or {}),
        },
    )


async def save_assistant_stream_interrupted_message(
    *,
    conversation_id: str,
    entity_id: str | None,
    workspace_id: str | None,
    agent_id: str | None,
    tool_results: list[dict] | None = None,
    attachments: dict | list | None = None,
    message_id: str | None = None,
    partial_content: str | None = None,
    meta: dict | None = None,
) -> str | None:
    """Persist a cancelled stream so partial tool side effects stay auditable."""
    return await save_or_update_assistant_stream_message(
        conversation_id=conversation_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        message_id=message_id,
        content=runtime_assistant_stream_interrupted_content(partial_content),
        tool_calls=tool_results if tool_results else None,
        attachments=attachments,
        meta=assistant_stream_interrupted_meta(meta),
    )


async def mark_stale_assistant_streams_interrupted(
    db: AsyncSession,
    *,
    grace_minutes: int = STALE_STREAM_GRACE_MINUTES,
) -> int:
    """Mark durable stream checkpoints abandoned by process restarts.

    During SSE chat, partial assistant content is periodically persisted as a
    checkpoint with ``stream_status=streaming``. If the API process is killed
    mid-stream, the normal cancellation handler may not run, leaving the UI
    with a forever-streaming partial response. This startup repair makes those
    old checkpoints terminal while preserving their partial content.
    """

    try:
        minutes = max(1, int(grace_minutes))
    except (TypeError, ValueError):
        minutes = STALE_STREAM_GRACE_MINUTES

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = (await db.execute(
        select(Message.id, Message.meta)
        .where(
            Message.role == "assistant",
            Message.created_at < cutoff,
            Message.meta["stream_status"].astext.in_(["running", "streaming"]),
        )
    )).all()
    if not rows:
        return 0

    for message_id, meta in rows:
        await db.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(meta=assistant_stream_interrupted_meta(meta))
        )
    await db.commit()
    count = len(rows)
    if count:
        logger.info("Marked %d stale assistant stream checkpoint(s) interrupted", count)
    return count
