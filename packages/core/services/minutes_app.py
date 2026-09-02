"""Meeting Minutes app availability and subscription checks."""
from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.minutes_app import MEETING_MINUTES_APP_FEATURE_KEY
from packages.core.services.feature_service import check_feature

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def minutes_app_available_in_environment() -> bool:
    """The Minutes app is live in production; an env override can hide it."""
    override = os.getenv("MEETING_MINUTES_APP_AVAILABLE")
    if override is not None:
        value = override.strip().lower()
        if value in _TRUTHY:
            return True
        if value in _FALSY:
            return False
    return True


async def minutes_app_subscribed(db: AsyncSession, entity_id: str) -> bool:
    if not minutes_app_available_in_environment():
        return False
    return await check_feature(db, entity_id, MEETING_MINUTES_APP_FEATURE_KEY)
