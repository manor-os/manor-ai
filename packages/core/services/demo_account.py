"""OSS demo account seed and public login hint."""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import DEFAULT_PLAN_ID
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User
from packages.core.services.auth_service import (
    ensure_user_membership,
    hash_password,
    register_user,
    verify_password,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

DEFAULT_DEMO_EMAIL = "demo@manor.local"
DEFAULT_DEMO_PASSWORD = "manor-demo"
DEFAULT_DEMO_ENTITY_NAME = "Manor OSS Demo"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def demo_account_config() -> dict[str, Any]:
    """Public demo account config for OSS login screens.

    The password is intentionally returned only for OSS mode, where the account
    is a throwaway local demo identity. Cloud mode always returns disabled.
    """
    deployment_mode = os.getenv("DEPLOYMENT_MODE", "oss").strip().lower()
    enabled = deployment_mode == "oss" and _env_bool("MANOR_DEMO_ACCOUNT_ENABLED", True)
    email = os.getenv("MANOR_DEMO_ACCOUNT_EMAIL", DEFAULT_DEMO_EMAIL).strip().lower()
    password = os.getenv("MANOR_DEMO_ACCOUNT_PASSWORD", DEFAULT_DEMO_PASSWORD)
    entity_name = os.getenv("MANOR_DEMO_ACCOUNT_ENTITY_NAME", DEFAULT_DEMO_ENTITY_NAME).strip()

    if not email or not password:
        enabled = False

    return {
        "enabled": enabled,
        "email": email if enabled else None,
        "password": password if enabled else None,
        "entity_name": entity_name or DEFAULT_DEMO_ENTITY_NAME,
    }


def _demo_entity_settings() -> dict[str, Any]:
    return {
        "plan": DEFAULT_PLAN_ID,
        "demo_account": True,
    }


async def ensure_demo_account(db: AsyncSession) -> dict[str, Any]:
    """Create or repair the default OSS demo account.

    Idempotent and safe to call on every boot. If the account already exists,
    it is kept active and its password is synchronized with the configured demo
    password so a fresh OSS checkout always has a known login.
    """
    cfg = demo_account_config()
    if not cfg["enabled"]:
        return {"enabled": False, "created": False, "email": None}

    email = str(cfg["email"]).lower()
    password = str(cfg["password"])
    entity_name = str(cfg["entity_name"] or DEFAULT_DEMO_ENTITY_NAME)

    user = (
        await db.execute(
            select(User).where(func.lower(User.email) == email)
        )
    ).scalar_one_or_none()

    if user is None:
        user, entity = await register_user(
            db,
            email=email,
            password=password,
            entity_name=entity_name,
            display_name="Demo User",
        )
        entity.settings = _demo_entity_settings()
        user.preferences = {**dict(user.preferences or {}), "demo_account": True}
        await db.flush()
        return {"enabled": True, "created": True, "email": email}

    entity = (
        await db.execute(select(Entity).where(Entity.id == user.entity_id))
    ).scalar_one_or_none()
    if entity is None:
        entity = Entity(
            id=generate_ulid(),
            name=entity_name,
            plan_id=DEFAULT_PLAN_ID,
            settings=_demo_entity_settings(),
        )
        db.add(entity)
        await db.flush()
        user.entity_id = entity.id
    else:
        entity.name = entity.name or entity_name
        entity.plan_id = entity.plan_id or DEFAULT_PLAN_ID
        settings = dict(entity.settings or {})
        settings.setdefault("plan", DEFAULT_PLAN_ID)
        settings["demo_account"] = True
        entity.settings = settings

    if user.deleted_at is not None:
        user.deleted_at = None
    user.status = "active"
    user.role = "owner"
    user.display_name = user.display_name or "Demo User"
    preferences = dict(user.preferences or {})
    preferences["demo_account"] = True
    user.preferences = preferences
    if not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)

    await ensure_user_membership(
        db,
        user=user,
        entity_id=user.entity_id,
        role="owner",
        status="active",
        is_primary=True,
    )
    await db.flush()
    return {"enabled": True, "created": False, "email": email}
