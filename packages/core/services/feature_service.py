"""
Feature gating service — check, enable, disable features for entities.

Ported from Java FeaturePackageServiceImpl / UserFeatureServiceImpl.
Simplified: features are key-based, packages hold a JSON list of keys,
entity-level overrides live in entity_features.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.feature import EntityFeature, Feature, FeaturePackage
from packages.core.models.user import Entity


# ── Feature catalogue ───────────────────────────────────────────────────

async def list_features(
    db: AsyncSession,
    *,
    category: str | None = None,
    visible_only: bool = True,
) -> list[Feature]:
    """List platform features, optionally filtered."""
    q = select(Feature).where(Feature.status == "active")
    if visible_only:
        q = q.where(Feature.is_visible.is_(True))
    if category:
        q = q.where(Feature.category == category)
    q = q.order_by(Feature.sort_order.asc(), Feature.name.asc())
    result = await db.execute(q)
    return list(result.scalars().all())


# ── Packages ────────────────────────────────────────────────────────────

async def list_packages(db: AsyncSession) -> list[FeaturePackage]:
    result = await db.execute(
        select(FeaturePackage)
        .where(FeaturePackage.status == "active")
        .order_by(FeaturePackage.price_monthly.asc().nulls_first())
    )
    return list(result.scalars().all())


async def get_package(db: AsyncSession, package_id: str) -> FeaturePackage | None:
    result = await db.execute(
        select(FeaturePackage).where(FeaturePackage.id == package_id)
    )
    return result.scalar_one_or_none()


# ── Entity feature resolution ──────────────────────────────────────────

async def get_entity_features(db: AsyncSession, entity_id: str) -> dict[str, bool]:
    """Return a dict of feature_key -> enabled for an entity.

    Merges:
    1. Features inherited from the entity's assigned package (plan_id).
    2. Entity-level overrides from entity_features table.
    """
    features: dict[str, bool] = {}

    # 1. Package features
    entity_result = await db.execute(
        select(Entity).where(Entity.id == entity_id)
    )
    entity = entity_result.scalar_one_or_none()
    if entity and entity.plan_id:
        pkg = await get_package(db, entity.plan_id)
        if pkg and pkg.features:
            for key in pkg.features:
                features[key] = True

    # 2. Entity-level overrides (may enable extras or disable inherited)
    now = datetime.now(timezone.utc)
    override_result = await db.execute(
        select(EntityFeature).where(EntityFeature.entity_id == entity_id)
    )
    for ef in override_result.scalars().all():
        # Skip expired overrides
        if ef.expires_at and ef.expires_at < now:
            continue
        features[ef.feature_key] = ef.enabled

    return features


async def check_feature(db: AsyncSession, entity_id: str, feature_key: str) -> bool:
    """Check whether an entity has access to a specific feature."""
    all_features = await get_entity_features(db, entity_id)
    return all_features.get(feature_key, False)


# ── Enable / disable ───────────────────────────────────────────────────

async def enable_feature(
    db: AsyncSession,
    entity_id: str,
    feature_key: str,
    *,
    config: dict | None = None,
    expires_at: datetime | None = None,
) -> EntityFeature:
    """Enable a feature for an entity (upsert)."""
    result = await db.execute(
        select(EntityFeature).where(
            EntityFeature.entity_id == entity_id,
            EntityFeature.feature_key == feature_key,
        )
    )
    ef = result.scalar_one_or_none()
    if ef:
        ef.enabled = True
        ef.config = config if config is not None else ef.config
        ef.expires_at = expires_at
    else:
        ef = EntityFeature(
            id=generate_ulid(),
            entity_id=entity_id,
            feature_key=feature_key,
            enabled=True,
            config=config,
            expires_at=expires_at,
        )
        db.add(ef)
    await db.flush()
    return ef


async def disable_feature(
    db: AsyncSession, entity_id: str, feature_key: str
) -> bool:
    """Disable a feature override for an entity."""
    result = await db.execute(
        select(EntityFeature).where(
            EntityFeature.entity_id == entity_id,
            EntityFeature.feature_key == feature_key,
        )
    )
    ef = result.scalar_one_or_none()
    if not ef:
        return False
    ef.enabled = False
    await db.flush()
    return True


# ── Package assignment ──────────────────────────────────────────────────

async def assign_package(db: AsyncSession, entity_id: str, package_id: str) -> None:
    """Assign a feature package to an entity by updating Entity.plan_id."""
    result = await db.execute(select(Entity).where(Entity.id == entity_id))
    entity = result.scalar_one_or_none()
    if not entity:
        raise ValueError(f"Entity {entity_id} not found")
    entity.plan_id = package_id
    await db.flush()


# ── Limits ──────────────────────────────────────────────────────────────

async def get_entity_limits(db: AsyncSession, entity_id: str) -> dict:
    """Return the entity's resource limits from their assigned package.

    Returns dict with keys: max_tokens, max_credit, package_name.
    All values are None if no package is assigned.
    """
    entity_result = await db.execute(
        select(Entity).where(Entity.id == entity_id)
    )
    entity = entity_result.scalar_one_or_none()
    if not entity or not entity.plan_id:
        return {"max_tokens": None, "max_credit": None, "package_name": None}

    pkg = await get_package(db, entity.plan_id)
    if not pkg:
        return {"max_tokens": None, "max_credit": None, "package_name": None}

    return {
        "max_tokens": pkg.max_tokens,
        "max_credit": float(pkg.max_credit) if pkg.max_credit is not None else None,
        "package_name": pkg.name,
    }
