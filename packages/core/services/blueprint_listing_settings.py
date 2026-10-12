"""Durable Marketplace listing overrides for official Blueprints."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.platform_setting import PlatformSetting


BLUEPRINT_LISTING_SETTINGS_KEY = "blueprint_listing_overrides"


async def lock_blueprint_listing_settings(
    db: AsyncSession,
) -> PlatformSetting:
    """Lock the shared settings row so seed and Admin edits serialize."""
    row = (await db.execute(
        select(PlatformSetting)
        .where(PlatformSetting.key == BLUEPRINT_LISTING_SETTINGS_KEY)
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        row = PlatformSetting(key=BLUEPRINT_LISTING_SETTINGS_KEY, value={})
        db.add(row)
        await db.flush()
    return row


def blueprint_listing_overrides(value: Any) -> dict[str, dict[str, Any]]:
    """Return only well-shaped per-slug override documents."""
    if not isinstance(value, dict):
        return {}
    return {
        str(slug): dict(override)
        for slug, override in value.items()
        if isinstance(override, dict)
    }
