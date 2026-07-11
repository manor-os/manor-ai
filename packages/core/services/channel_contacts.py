from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.task import Conversation
from packages.core.models.user import User


async def upsert_channel_contact(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_type: str,
    channel_config_id: str,
    source_id: str,
    sender_name: Optional[str],
) -> ChannelContact:
    """Idempotently resolve the durable external identity for a channel."""
    now = datetime.now(timezone.utc)

    insert_stmt = pg_insert(ChannelContact).values(
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        channel_type=channel_type,
        source_id=source_id,
        display_name=sender_name,
        last_seen_at=now,
    )
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["channel_config_id", "source_id"],
        set_={
            "last_seen_at": now,
            "display_name": func.coalesce(
                func.nullif(insert_stmt.excluded.display_name, ""),
                ChannelContact.display_name,
            ),
        },
    ).returning(ChannelContact.id)

    result = await db.execute(stmt)
    contact_id = result.scalar_one()

    contact = (await db.execute(
        select(ChannelContact).where(ChannelContact.id == contact_id)
    )).scalar_one()
    return contact


async def find_channel_session_contact(
    db: AsyncSession,
    *,
    cc: ChannelConfig,
    session_id: str,
    conversation: Conversation | None = None,
) -> ChannelContact | None:
    contact_id = None
    if conversation is not None:
        contact_id = (conversation.meta or {}).get("channel_contact_id")
    if contact_id:
        contact = (await db.execute(
            select(ChannelContact).where(
                ChannelContact.id == contact_id,
                ChannelContact.entity_id == cc.entity_id,
                ChannelContact.channel_config_id == cc.id,
            )
        )).scalar_one_or_none()
        if contact:
            return contact

    return (await db.execute(
        select(ChannelContact).where(
            ChannelContact.entity_id == cc.entity_id,
            ChannelContact.channel_config_id == cc.id,
            ChannelContact.source_id == session_id,
        )
    )).scalar_one_or_none()


async def find_claimed_webchat_contact_for_user(
    db: AsyncSession,
    *,
    cc: ChannelConfig,
    user: User | None,
) -> ChannelContact | None:
    """Find a previously claimed webchat contact for this signed-in visitor."""
    if not user:
        return None

    filters = [
        ChannelContact.entity_id == cc.entity_id,
        ChannelContact.channel_config_id == cc.id,
        ChannelContact.status == "active",
    ]
    if user.entity_id == cc.entity_id:
        filters.append(ChannelContact.user_id == user.id)
    else:
        filters.append(ChannelContact.profile["verified_customer_user_id"].astext == user.id)

    return (await db.execute(
        select(ChannelContact)
        .where(*filters)
        .order_by(ChannelContact.updated_at.desc())
        .limit(1)
    )).scalar_one_or_none()


def channel_contact_claimed_by_other_user(contact: ChannelContact, user: User) -> bool:
    if contact.user_id and contact.user_id != user.id:
        return True
    profile = contact.profile or {}
    verified_user_id = profile.get("verified_customer_user_id")
    return bool(verified_user_id and verified_user_id != user.id)


def channel_contact_requires_claimed_user(contact: ChannelContact) -> bool:
    profile = contact.profile or {}
    return bool(contact.user_id or profile.get("verified_customer_user_id"))


def link_channel_contact_to_user(
    contact: ChannelContact,
    user: User,
    cc: ChannelConfig,
    *,
    display_name: str,
) -> None:
    """Attach the visitor identity without granting cross-entity workspace power."""
    if user.entity_id == cc.entity_id:
        contact.user_id = user.id
        contact.role = user.role or "member"
    else:
        profile = dict(contact.profile or {})
        profile.update({
            "verified_customer_user_id": user.id,
            "verified_customer_entity_id": user.entity_id,
            "verified_customer_email": user.email,
            "verified_customer_name": display_name,
        })
        contact.profile = profile
        contact.role = contact.role or "external"

    contact.username = user.email
    contact.display_name = contact.display_name or display_name
