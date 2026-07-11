from __future__ import annotations

from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Conversation, Message

CHANNEL_HISTORY_CHANNELS: set[str] = {
    "discord",
    "email",
    "facebook",
    "gmail",
    "inapp",
    "slack",
    "sms",
    "telegram",
    "twilio_sms",
    "twilio_voice",
    "webchat",
    "wechat",
    "wechat_personal",
    "whatsapp",
}


def is_channel_history_conversation(conv: Conversation) -> bool:
    """External channel conversations are shared in Chat History."""

    channel = (getattr(conv, "channel", "") or "").lower()
    scope = getattr(conv, "scope", "channel") or "channel"
    return scope == "channel" and channel in CHANNEL_HISTORY_CHANNELS


async def list_messages(
    db: AsyncSession,
    conversation_id: str,
    limit: int = 100,
) -> list[Message]:
    """Fetch the newest conversation messages and return them chronologically."""

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    msgs = list(result.scalars().all())
    msgs.reverse()
    return msgs


async def list_conversations(
    db: AsyncSession,
    entity_id: str,
    user_id: str | None = None,
) -> list[Conversation]:
    """List recent conversations, sorting by newest message when available."""

    last_msg = (
        select(
            Message.conversation_id,
            func.max(Message.created_at).label("last_msg_at"),
        )
        .group_by(Message.conversation_id)
        .subquery()
    )
    q = (
        select(Conversation)
        .outerjoin(last_msg, Conversation.id == last_msg.c.conversation_id)
        .where(Conversation.entity_id == entity_id)
    )
    if user_id:
        q = q.where(
            or_(
                and_(
                    Conversation.user_id == user_id,
                    Conversation.workspace_id.is_(None),
                ),
                and_(
                    Conversation.scope == "channel",
                    func.lower(Conversation.channel).in_(CHANNEL_HISTORY_CHANNELS),
                ),
            )
        )
    q = q.order_by(
        func.coalesce(last_msg.c.last_msg_at, Conversation.created_at).desc()
    ).limit(50)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_conversation(
    db: AsyncSession,
    conversation_id: str,
    entity_id: str,
) -> Optional[Conversation]:
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()
