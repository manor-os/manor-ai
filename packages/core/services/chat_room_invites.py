"""Validate chat invitation capabilities without granting company access."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.chat_room import ChatRoom, ChatRoomInvite
from packages.core.models.user import Entity, User


async def valid_chat_registration_invite(db: AsyncSession, token: str | None, email: str) -> bool:
    """A pending exact-email chat invite can admit signup, never auto-join a room."""
    if not token or len(token) > 256:
        return False
    normalized = email.strip().casefold()
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = (await db.execute(
        select(ChatRoom)
        .join(ChatRoomInvite, ChatRoomInvite.room_id == ChatRoom.id)
        .where(
            ChatRoomInvite.token_hash == digest,
            ChatRoomInvite.status == "pending",
            ChatRoomInvite.expires_at > datetime.now(timezone.utc),
            func.lower(ChatRoomInvite.email) == normalized,
            ChatRoom.status == "active",
        )
    )).scalar_one_or_none()
    if row is None:
        return False
    if row.owner_entity_id:
        return (await db.scalar(select(Entity.id).where(
            Entity.id == row.owner_entity_id, Entity.deleted_at.is_(None),
        ))) is not None
    return (await db.scalar(select(User.id).where(
        User.id == row.owner_user_id, User.deleted_at.is_(None), User.status == "active",
    ))) is not None
