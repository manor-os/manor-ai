"""Settings service — entity settings and user preferences (JSONB merge)."""
from __future__ import annotations

import logging

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.constants.integrations import (
    INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE_KEY,
)
from packages.core.models.user import Entity, User

logger = logging.getLogger(__name__)


# ── Entity settings ──

async def get_entity_settings(db: AsyncSession, entity_id: str) -> dict:
    # Settings participate in caller-owned transactions. A shared cache can be
    # repopulated from the old row after pre-commit invalidation, then remain
    # stale after the writer commits, so reads intentionally stay DB-local.
    result = await db.execute(
        select(Entity.settings).where(
            Entity.id == entity_id,
            Entity.deleted_at.is_(None),
        )
    )
    settings = result.scalar_one_or_none()
    return settings if isinstance(settings, dict) else {}


async def update_entity_settings(db: AsyncSession, entity_id: str, settings: dict) -> dict:
    public_settings = settings
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql" and public_settings:
        current = func.coalesce(Entity.settings, literal({}, type_=JSONB))
        updated = await db.execute(
            update(Entity)
            .where(Entity.id == entity_id, Entity.deleted_at.is_(None))
            .values(
                settings=current.op("||")(
                    literal(public_settings, type_=JSONB)
                )
            )
            .returning(Entity.settings)
            .execution_options(synchronize_session=False)
        )
        merged = updated.scalar_one_or_none()
        if merged is None:
            return {}
        await db.flush()
        await cache.delete(f"settings:{entity_id}")
        await cache.delete(f"entity:{entity_id}")
        return merged

    result = await db.execute(
        select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None))
    )
    entity = result.scalar_one_or_none()
    if not entity:
        return {}
    merged = {**(entity.settings or {}), **public_settings}
    if public_settings:
        entity.settings = merged
        await db.flush()
    # Invalidate cache
    await cache.delete(f"settings:{entity_id}")
    await cache.delete(f"entity:{entity_id}")
    return merged


# ── User preferences ──

async def get_user_preferences(db: AsyncSession, user_id: str) -> dict:
    # Preferences share the caller's transaction boundary. Caching them here
    # lets another request repopulate stale data after a pre-commit
    # invalidation, so preference reads intentionally stay transaction-local.
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    user = result.scalar_one_or_none()
    if not user:
        return {}
    return user.preferences or {}


async def update_user_preferences(db: AsyncSession, user_id: str, preferences: dict) -> dict:
    # Integration account defaults are an internal runtime pointer. General
    # settings writes must neither accept nor overwrite that reserved branch.
    public_preferences = {
        key: value
        for key, value in preferences.items()
        if key != INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE_KEY
    }
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    user = result.scalar_one_or_none()
    if not user:
        return {}
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql" and public_preferences:
        current = func.coalesce(User.preferences, literal({}, type_=JSONB))
        updated = await db.execute(
            update(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .values(
                preferences=current.op("||")(
                    literal(public_preferences, type_=JSONB)
                )
            )
            .returning(User.preferences)
            .execution_options(synchronize_session=False)
        )
        merged = updated.scalar_one_or_none()
        if merged is None:
            return {}
        await db.flush()
        # Briefing schedule synchronization reads the ORM instance below.
        await db.refresh(user, attribute_names=["preferences"])
    else:
        merged = {**(user.preferences or {}), **public_preferences}
        if public_preferences:
            user.preferences = merged
            await db.flush()
    try:
        from packages.core.briefing.scheduling import (
            briefing_schedule_preferences_changed,
            sync_user_briefing_schedules,
        )

        if briefing_schedule_preferences_changed(public_preferences):
            await sync_user_briefing_schedules(db, user)
    except Exception as exc:
        logger.warning("failed to sync briefing schedules after preference update: %s", exc)
    return merged
