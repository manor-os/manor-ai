"""API key service — CRUD, rotation, resolution for entity LLM credentials."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.api_key import ApiKey
from packages.core.models.base import generate_ulid
from packages.core.services.auth_service import hash_password, verify_password


async def create_api_key(
    db: AsyncSession,
    entity_id: str,
    name: str,
    provider: str,
    api_key: str,
    *,
    base_url: str | None = None,
    default_model: str | None = None,
    is_default: bool = False,
) -> tuple[ApiKey, str]:
    """Create an API key. Returns (model, clear_key) -- clear_key shown only once."""
    key_hash = hash_password(api_key)
    key_prefix = api_key[:12] + "..." if len(api_key) >= 12 else api_key[:4] + "..."

    # If setting as default, unset other defaults for this entity
    if is_default:
        await db.execute(
            update(ApiKey)
            .where(ApiKey.entity_id == entity_id, ApiKey.is_default.is_(True))
            .values(is_default=False)
        )

    key = ApiKey(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        provider=provider,
        key_hash=key_hash,
        key_prefix=key_prefix,
        base_url=base_url,
        default_model=default_model,
        is_default=is_default,
    )
    db.add(key)
    await db.flush()
    return key, api_key


async def list_api_keys(db: AsyncSession, entity_id: str) -> list[ApiKey]:
    """List keys for an entity (never exposes key_hash in the router layer)."""
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.entity_id == entity_id, ApiKey.status != "revoked")
        .order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars().all())


async def get_api_key_by_id(
    db: AsyncSession, key_id: str, entity_id: str
) -> ApiKey | None:
    """Get a single API key scoped to entity."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def get_default_key(db: AsyncSession, entity_id: str) -> ApiKey | None:
    """Get the default active API key for an entity."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.entity_id == entity_id,
            ApiKey.is_default.is_(True),
            ApiKey.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def update_api_key(
    db: AsyncSession,
    key_id: str,
    entity_id: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    default_model: str | None = None,
    is_default: bool | None = None,
) -> ApiKey | None:
    """Update mutable fields on an API key."""
    key = await get_api_key_by_id(db, key_id, entity_id)
    if not key or key.status == "revoked":
        return None

    if name is not None:
        key.name = name
    if base_url is not None:
        key.base_url = base_url
    if default_model is not None:
        key.default_model = default_model
    if is_default is True:
        # Unset other defaults first
        await db.execute(
            update(ApiKey)
            .where(ApiKey.entity_id == entity_id, ApiKey.is_default.is_(True))
            .values(is_default=False)
        )
        key.is_default = True
    elif is_default is False:
        key.is_default = False

    await db.flush()
    return key


async def rotate_api_key(
    db: AsyncSession, key_id: str, entity_id: str, new_api_key: str
) -> ApiKey | None:
    """Rotate (replace) an API key's secret."""
    key = await get_api_key_by_id(db, key_id, entity_id)
    if not key or key.status == "revoked":
        return None

    key.key_hash = hash_password(new_api_key)
    key.key_prefix = (
        new_api_key[:12] + "..." if len(new_api_key) >= 12 else new_api_key[:4] + "..."
    )
    await db.flush()
    await db.refresh(key)
    return key


async def revoke_api_key(db: AsyncSession, key_id: str, entity_id: str) -> bool:
    """Revoke an API key (soft delete via status)."""
    key = await get_api_key_by_id(db, key_id, entity_id)
    if not key:
        return False
    key.status = "revoked"
    key.is_default = False
    await db.flush()
    return True


async def record_key_usage(db: AsyncSession, key_id: str) -> None:
    """Increment usage_count and set last_used_at."""
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(
            usage_count=ApiKey.usage_count + 1,
            last_used_at=datetime.now(timezone.utc),
        )
    )


async def resolve_llm_config(db: AsyncSession, entity_id: str) -> dict:
    """Resolve the LLM configuration for an entity.

    Priority:
    1. Entity's default API key (from api_keys table)
    2. Cloud-only platform fallback from the private deployment environment

    Returns: {api_key (masked), base_url, model, provider, key_id, source}
    """
    default = await get_default_key(db, entity_id)
    if default:
        return {
            "provider": default.provider,
            "model": default.default_model,
            "base_url": default.base_url,
            "key_prefix": default.key_prefix,
            "key_id": default.id,
            "key_name": default.name,
            "source": "entity_api_key",
        }

    return {
        "provider": None,
        "model": None,
        "base_url": None,
        "key_prefix": None,
        "key_id": None,
        "key_name": None,
        "source": "missing",
    }
