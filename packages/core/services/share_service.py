"""Conversation sharing service — create, access, and revoke shared links."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.conversation_share import ConversationShare
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_records import get_conversation, list_messages


async def create_share(
    db: AsyncSession,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    *,
    expires_hours: int | None = None,
) -> ConversationShare:
    """Create a shareable link for a conversation."""
    conv = await get_conversation(db, conversation_id, entity_id)
    if not conv:
        raise ValueError("Conversation not found")

    share = ConversationShare(
        id=generate_ulid(),
        conversation_id=conversation_id,
        entity_id=entity_id,
        shared_by=user_id,
        share_token=secrets.token_urlsafe(32),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        ) if expires_hours else None,
    )
    db.add(share)
    await db.flush()
    return share


async def get_shared_conversation(
    db: AsyncSession, share_token: str,
) -> tuple[ConversationShare, Conversation, list[Message]] | None:
    """Load a shared conversation by its token.

    Returns None if the share is invalid, inactive, or expired.
    """
    result = await db.execute(
        select(ConversationShare).where(
            ConversationShare.share_token == share_token,
            ConversationShare.is_active.is_(True),
        )
    )
    share = result.scalar_one_or_none()
    if not share:
        return None

    # Check expiry
    if share.expires_at and share.expires_at < datetime.now(timezone.utc):
        return None

    # Load conversation (using entity_id from share to bypass auth)
    conv = await get_conversation(db, share.conversation_id, share.entity_id)
    if not conv:
        return None

    messages = await list_messages(db, share.conversation_id, limit=500)
    return share, conv, messages


async def revoke_share(
    db: AsyncSession, share_id: str, entity_id: str, conversation_id: str | None = None,
) -> bool:
    """Revoke a shared link."""
    q = select(ConversationShare).where(
        ConversationShare.id == share_id,
        ConversationShare.entity_id == entity_id,
    )
    if conversation_id:
        q = q.where(ConversationShare.conversation_id == conversation_id)
    result = await db.execute(q)
    share = result.scalar_one_or_none()
    if not share:
        return False

    share.is_active = False
    await db.flush()
    return True


async def list_shares(
    db: AsyncSession, entity_id: str, conversation_id: str | None = None,
) -> list[ConversationShare]:
    """List all active shares for an entity, optionally filtered by conversation."""
    q = select(ConversationShare).where(
        ConversationShare.entity_id == entity_id,
        ConversationShare.is_active.is_(True),
    )
    if conversation_id:
        q = q.where(ConversationShare.conversation_id == conversation_id)
    q = q.order_by(ConversationShare.created_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())
