from __future__ import annotations

from typing import Optional

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Conversation, Message

CHANNEL_HISTORY_LIMIT = 12


def channel_conversation_title(
    sender_name: Optional[str],
    sender_id: str,
    channel_type: str,
) -> str:
    base = sender_name or sender_id
    return f"{channel_type}: {base}"


async def get_or_create_channel_conversation(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_type: str,
    channel_config_id: str,
    channel_contact_id: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    agent_id: Optional[str],
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    agent_subscription_id: Optional[str] = None,
) -> Conversation:
    """Resolve the durable conversation for a channel contact.

    Channel conversation identity is keyed by ``channel_contact_id`` so display
    name changes or contact merges do not fork chat history. When an old row is
    found, newly introduced ownership/workspace fields are backfilled.
    """
    existing = await db.execute(
        select(Conversation)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.channel == channel_type,
            Conversation.meta["channel_contact_id"].astext == channel_contact_id,
        )
        .order_by(desc(Conversation.updated_at))
        .limit(1)
    )
    conv = existing.scalar_one_or_none()
    if conv:
        if user_id and not conv.user_id:
            conv.user_id = user_id
        if workspace_id and not getattr(conv, "workspace_id", None):
            conv.workspace_id = workspace_id
        if agent_subscription_id and not getattr(conv, "agent_subscription_id", None):
            conv.agent_subscription_id = agent_subscription_id
        await db.flush()
        return conv

    conv = Conversation(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        agent_subscription_id=agent_subscription_id,
        channel=channel_type,
        title=channel_conversation_title(sender_name, sender_id, channel_type),
        meta={
            "channel_contact_id": channel_contact_id,
            "channel_config_id": channel_config_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "chat_id": chat_id,
        },
    )
    db.add(conv)
    await db.flush()
    return conv


async def find_public_webchat_conversation_by_session(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_config_id: str,
    session_id: str,
) -> Conversation | None:
    return (await db.execute(
        select(Conversation).where(
            Conversation.entity_id == entity_id,
            Conversation.channel == "webchat",
            Conversation.meta["channel_config_id"].astext == channel_config_id,
            or_(
                Conversation.meta["session_id"].astext == session_id,
                Conversation.meta["sender_id"].astext == session_id,
                Conversation.meta["chat_id"].astext == session_id,
            ),
        ).order_by(desc(Conversation.updated_at)).limit(1)
    )).scalar_one_or_none()


async def load_recent_channel_messages(
    db: AsyncSession,
    conversation_id: str,
    *,
    limit: int = CHANNEL_HISTORY_LIMIT,
) -> list[dict]:
    """Return recent channel turns as plain chat-completion messages."""
    rows = (await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(desc(Message.created_at))
        .limit(limit)
    )).scalars().all()

    out: list[dict] = []
    for message in reversed(rows):
        if message.role in ("user", "assistant") and message.content:
            out.append({"role": message.role, "content": message.content})
    return out


async def list_public_webchat_messages(
    db: AsyncSession,
    conversation_id: str,
    *,
    session_id: str | None = None,
    after: str | None = None,
    limit: int = 50,
) -> list[dict]:
    q = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.role.in_(("user", "assistant")),
    ).order_by(Message.created_at.asc())

    if session_id:
        q = q.where(
            Message.meta["channel_type"].astext == "webchat",
            or_(
                Message.meta["chat_id"].astext == session_id,
                Message.meta["sender_id"].astext == session_id,
                Message.meta["source_id"].astext == session_id,
                Message.meta["session_id"].astext == session_id,
            ),
        )

    if after:
        cursor_msg = (await db.execute(
            select(Message.created_at).where(Message.id == after)
        )).scalar_one_or_none()
        if cursor_msg:
            q = q.where(Message.created_at > cursor_msg)

    rows = (await db.execute(q.limit(limit))).scalars().all()
    return [
        {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat() if message.created_at else None,
            "attachments": (
                (message.attachments or {}).get("items")
                if message.attachments
                else None
            ),
        }
        for message in rows
    ]


async def add_channel_inbound_message(
    db: AsyncSession,
    *,
    conversation_id: str,
    channel_type: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    content: str,
    attachments: list[dict] | None = None,
) -> Message:
    """Persist an inbound channel turn in the conversation transcript."""
    message = Message(
        conversation_id=conversation_id,
        role="user",
        content=content,
        attachments={"items": attachments} if attachments else None,
        meta={
            "channel_type": channel_type,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "chat_id": chat_id,
        },
    )
    db.add(message)
    await db.flush()
    return message


async def add_channel_assistant_message(
    db: AsyncSession,
    *,
    conversation_id: str,
    channel_type: str,
    chat_id: Optional[str],
    content: str,
    runtime_meta: dict | None = None,
    approved_external_message: bool = False,
    author_kind: str | None = None,
    author_subscription_id: str | None = None,
) -> Message:
    """Persist an assistant/system channel transcript row."""
    meta = {
        "channel_type": channel_type,
        "chat_id": chat_id,
    }
    if runtime_meta:
        meta["runtime"] = runtime_meta
    if approved_external_message:
        meta["approved_external_message"] = True

    message_kwargs = {
        "conversation_id": conversation_id,
        "role": "assistant",
        "content": content,
        "meta": meta,
    }
    if author_kind is not None:
        message_kwargs["author_kind"] = author_kind
    if author_subscription_id is not None:
        message_kwargs["author_subscription_id"] = author_subscription_id
    message = Message(**message_kwargs)
    db.add(message)
    await db.flush()
    return message
