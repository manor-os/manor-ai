"""Settings service — entity settings and user preferences (JSONB merge)."""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import cache
from packages.core.models.user import Entity, User

logger = logging.getLogger(__name__)


# ── Entity settings ──

async def get_entity_settings(db: AsyncSession, entity_id: str) -> dict:
    # Check cache first
    cached = await cache.get(f"settings:{entity_id}")
    if cached is not None:
        return cached

    result = await db.execute(
        select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None))
    )
    entity = result.scalar_one_or_none()
    if not entity:
        return {}
    settings = entity.settings or {}
    await cache.set(f"settings:{entity_id}", settings, ttl=300)
    return settings


async def update_entity_settings(db: AsyncSession, entity_id: str, settings: dict) -> dict:
    result = await db.execute(
        select(Entity).where(Entity.id == entity_id, Entity.deleted_at.is_(None))
    )
    entity = result.scalar_one_or_none()
    if not entity:
        return {}
    merged = {**(entity.settings or {}), **settings}
    entity.settings = merged
    await db.flush()
    # Invalidate cache
    await cache.delete(f"settings:{entity_id}")
    await cache.delete(f"entity:{entity_id}")
    return merged


# ── User preferences ──

async def get_user_preferences(db: AsyncSession, user_id: str) -> dict:
    # Check cache first
    cached = await cache.get(f"prefs:{user_id}")
    if cached is not None:
        return cached

    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    user = result.scalar_one_or_none()
    if not user:
        return {}
    prefs = user.preferences or {}
    await cache.set(f"prefs:{user_id}", prefs, ttl=300)
    return prefs


async def update_user_preferences(db: AsyncSession, user_id: str, preferences: dict) -> dict:
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    user = result.scalar_one_or_none()
    if not user:
        return {}
    merged = {**(user.preferences or {}), **preferences}
    user.preferences = merged
    await db.flush()
    try:
        from packages.core.briefing.scheduling import (
            briefing_schedule_preferences_changed,
            sync_user_briefing_schedules,
        )

        if briefing_schedule_preferences_changed(preferences):
            await sync_user_briefing_schedules(db, user)
    except Exception as exc:
        logger.warning("failed to sync briefing schedules after preference update: %s", exc)
    # Invalidate cache
    await cache.delete(f"prefs:{user_id}")
    return merged
