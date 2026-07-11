"""Feature flag evaluation + admin CRUD.

Runtime contract — call ``is_enabled(key, ...)`` from anywhere code
needs to gate a feature. Returns the flag's effective value for the
caller's ``(entity_id, user_id)`` tuple, applying overrides in the
documented precedence order.

Precedence (highest first):
  1. user override          (FeatureFlagOverride scope='user')
  2. tenant override        (scope='tenant')
  3. percent rollout        (scope='percent', deterministic by entity_id hash)
  4. flag's default_enabled
  5. caller-supplied fallback (when the flag itself doesn't exist)

Expired overrides are ignored. Unknown keys never raise — they return
the fallback so a code path gated on an as-yet-unregistered flag fails
closed.

To keep this cheap on hot paths, evaluations use a 60s in-process LRU
cache keyed by ``(flag_key, entity_id, user_id)``. Admin mutations
bump the global cache version which invalidates everything — no need
for fan-out cache invalidation per change.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag, FeatureFlagOverride

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 60.0
_cache: dict[tuple[str, Optional[str], Optional[str]], tuple[bool, float]] = {}
_cache_version = 0


def _bump_cache() -> None:
    global _cache_version, _cache
    _cache_version += 1
    _cache.clear()


# ── Reads ────────────────────────────────────────────────────────────

@dataclass
class FlagEvaluation:
    enabled: bool
    source: str  # "user_override" | "tenant_override" | "percent" | "default" | "missing"
    detail: Optional[str] = None


async def is_enabled(
    db: AsyncSession,
    key: str,
    *,
    entity_id: Optional[str] = None,
    user_id: Optional[str] = None,
    fallback: bool = False,
) -> bool:
    """Hot-path evaluator. Returns the boolean only.

    ``fallback`` is the value used when the flag isn't registered AT ALL
    in ``feature_flags``. Lets a new code path be wrapped before the
    ops team has had a chance to register the flag.
    """
    cache_key = (key, entity_id, user_id)
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    evaluation = await evaluate(db, key, entity_id=entity_id, user_id=user_id)
    if evaluation.source == "missing":
        result = fallback
    else:
        result = evaluation.enabled
    _cache[cache_key] = (result, time.time())
    return result


async def evaluate(
    db: AsyncSession,
    key: str,
    *,
    entity_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> FlagEvaluation:
    """Detailed evaluator — returns enabled + which rule fired. Used by
    the admin UI to explain "why is this on for this tenant?"."""
    flag = (await db.execute(
        select(FeatureFlag).where(
            FeatureFlag.key == key,
            FeatureFlag.status == "active",
        )
    )).scalar_one_or_none()
    if flag is None:
        return FlagEvaluation(enabled=False, source="missing")

    now = datetime.now(timezone.utc)

    # 1. User override
    if user_id:
        user_ov = await _override_for(db, key, "user", user_id, now)
        if user_ov is not None:
            return FlagEvaluation(
                enabled=user_ov.enabled, source="user_override",
                detail=user_ov.set_reason,
            )

    # 2. Tenant override
    if entity_id:
        tenant_ov = await _override_for(db, key, "tenant", entity_id, now)
        if tenant_ov is not None:
            return FlagEvaluation(
                enabled=tenant_ov.enabled, source="tenant_override",
                detail=tenant_ov.set_reason,
            )

    # 3. Percent rollout — only meaningful when an entity_id is known.
    if entity_id:
        percent_ov = await _percent_match(db, key, entity_id, now)
        if percent_ov is not None:
            return FlagEvaluation(
                enabled=percent_ov, source="percent",
                detail=f"deterministic match for entity {entity_id}",
            )

    return FlagEvaluation(enabled=bool(flag.default_enabled), source="default")


async def _override_for(
    db: AsyncSession, key: str, scope: str, scope_id: str, now: datetime,
) -> Optional[FeatureFlagOverride]:
    row = (await db.execute(
        select(FeatureFlagOverride).where(
            FeatureFlagOverride.flag_key == key,
            FeatureFlagOverride.scope == scope,
            FeatureFlagOverride.scope_id == scope_id,
        ).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at and row.expires_at <= now:
        return None
    return row


async def _percent_match(
    db: AsyncSession, key: str, entity_id: str, now: datetime,
) -> Optional[bool]:
    """Look at percent overrides for this flag — find the highest-target
    one that includes this entity. Deterministic so an entity stays
    inside or outside the rollout across requests."""
    rows = list((await db.execute(
        select(FeatureFlagOverride).where(
            FeatureFlagOverride.flag_key == key,
            FeatureFlagOverride.scope == "percent",
        ).order_by(desc(FeatureFlagOverride.scope_id))  # higher % first
    )).scalars().all())
    if not rows:
        return None

    bucket = _entity_bucket(key, entity_id)
    for row in rows:
        if row.expires_at and row.expires_at <= now:
            continue
        try:
            threshold = int(row.scope_id)
        except (TypeError, ValueError):
            continue
        if bucket < threshold:
            return bool(row.enabled)
    return None


def _entity_bucket(key: str, entity_id: str) -> int:
    """Stable 0-99 bucket for an entity, namespaced per flag so different
    flags pick different cohorts even at the same percentage."""
    h = hashlib.sha256(f"{key}:{entity_id}".encode()).hexdigest()
    return int(h[:8], 16) % 100


# ── Admin CRUD ───────────────────────────────────────────────────────

async def list_flags(db: AsyncSession) -> list[FeatureFlag]:
    return list((await db.execute(
        select(FeatureFlag).order_by(FeatureFlag.key)
    )).scalars().all())


async def list_overrides(
    db: AsyncSession, flag_key: Optional[str] = None,
) -> list[FeatureFlagOverride]:
    stmt = select(FeatureFlagOverride).order_by(desc(FeatureFlagOverride.created_at))
    if flag_key:
        stmt = stmt.where(FeatureFlagOverride.flag_key == flag_key)
    return list((await db.execute(stmt)).scalars().all())


async def create_flag(
    db: AsyncSession,
    *,
    key: str,
    description: Optional[str],
    default_enabled: bool,
) -> FeatureFlag:
    existing = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == key)
    )).scalar_one_or_none()
    if existing:
        raise ValueError(f"flag {key!r} already exists")
    flag = FeatureFlag(
        id=generate_ulid(), key=key, description=description,
        default_enabled=default_enabled, status="active",
    )
    db.add(flag)
    await db.flush()
    _bump_cache()
    return flag


async def set_default(
    db: AsyncSession, key: str, enabled: bool,
) -> FeatureFlag:
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == key)
    )).scalar_one_or_none()
    if not flag:
        raise ValueError(f"unknown flag: {key!r}")
    flag.default_enabled = bool(enabled)
    await db.flush()
    _bump_cache()
    return flag


async def set_override(
    db: AsyncSession,
    *,
    key: str,
    scope: str,
    scope_id: str,
    enabled: bool,
    set_by_admin_id: Optional[str],
    set_reason: Optional[str],
    expires_at: Optional[datetime] = None,
) -> FeatureFlagOverride:
    if scope not in ("tenant", "user", "percent"):
        raise ValueError(f"unknown scope: {scope!r}")
    if scope == "percent":
        try:
            n = int(scope_id)
        except ValueError:
            raise ValueError("percent scope_id must be an integer 0-100")
        if n < 0 or n > 100:
            raise ValueError("percent scope_id must be in [0, 100]")

    existing = (await db.execute(
        select(FeatureFlagOverride).where(
            FeatureFlagOverride.flag_key == key,
            FeatureFlagOverride.scope == scope,
            FeatureFlagOverride.scope_id == scope_id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.enabled = bool(enabled)
        existing.set_by_admin_id = set_by_admin_id
        existing.set_reason = set_reason
        existing.expires_at = expires_at
        await db.flush()
        _bump_cache()
        return existing

    row = FeatureFlagOverride(
        id=generate_ulid(),
        flag_key=key, scope=scope, scope_id=scope_id, enabled=bool(enabled),
        set_by_admin_id=set_by_admin_id, set_reason=set_reason,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    _bump_cache()
    return row


async def delete_override(db: AsyncSession, override_id: str) -> bool:
    row = (await db.execute(
        select(FeatureFlagOverride).where(FeatureFlagOverride.id == override_id)
    )).scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    await db.flush()
    _bump_cache()
    return True
