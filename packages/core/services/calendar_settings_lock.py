"""Transaction lock shared by every Calendar settings writer."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User


async def lock_calendar_settings_user(
    db: AsyncSession,
    user_id: str,
) -> User | None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:calendar_key, 0))"),
        {"calendar_key": f"calendar-settings:{user_id}"},
    )
    return (await db.execute(
        select(User)
        .where(User.id == user_id, User.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
