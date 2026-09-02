"""Exact-id helpers for Marketplace ↔ local resource relationships."""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.services.reusable_resource_locks import (
    RESOURCE_AGENT as LOCK_RESOURCE_AGENT,
    RESOURCE_SKILL as LOCK_RESOURCE_SKILL,
    RESOURCE_WORKFLOW as LOCK_RESOURCE_WORKFLOW,
    lock_reusable_resource_reference,
    lock_reusable_resource_lifecycle,
)


RELATIONSHIP_INSTALLED_FROM = "installed_from"
RELATIONSHIP_INSTALLED_COMPONENT = "installed_component"
RELATIONSHIP_PUBLISHED_AS = "published_as"

RESOURCE_AGENT = "agent"
RESOURCE_SKILL = "skill"
RESOURCE_WORKFLOW = "workflow"
RESOURCE_WORKSPACE = "workspace"
RESOURCE_WORKSPACE_BLUEPRINT = "workspace_blueprint"

SCOPE_ENTITY = "entity"
SCOPE_RESOURCE = "resource"
SCOPE_WORKSPACE = "workspace"
ROOT_COMPONENT = "root"
MARKETPLACE_SOURCE_PLATFORM = "platform"
MARKETPLACE_SOURCE_MANOR = "manor"
_REUSABLE_LOCAL_RESOURCE_TYPES = {
    LOCK_RESOURCE_AGENT,
    LOCK_RESOURCE_SKILL,
    LOCK_RESOURCE_WORKFLOW,
}


class MarketplaceIdentityConflictError(ValueError):
    """Exact Marketplace and local identities cannot be resolved uniquely."""


def _identity_lock_key(*parts: str) -> int:
    """Stable signed bigint for a PostgreSQL transaction advisory lock."""
    digest = hashlib.sha256("\0".join(parts).encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


async def get_marketplace_resource_link(
    db: AsyncSession,
    *,
    entity_id: str,
    marketplace_source: str = MARKETPLACE_SOURCE_PLATFORM,
    marketplace_resource_type: str,
    marketplace_resource_id: str,
    relationship: str,
    scope_type: str,
    scope_id: str,
    local_resource_type: str,
    component_key: str = ROOT_COMPONENT,
    for_update: bool = False,
) -> MarketplaceResourceLink | None:
    """Resolve an exact Marketplace source within an exact install scope."""
    if for_update and local_resource_type in _REUSABLE_LOCAL_RESOURCE_TYPES:
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    if for_update and db.get_bind().dialect.name == "postgresql":
        # SELECT ... FOR UPDATE cannot lock an absent row. The transaction
        # lock serializes the first installation of this exact source/scope so
        # concurrent requests cannot mint two local resources and then race on
        # the unique link constraint.
        await db.execute(select(func.pg_advisory_xact_lock(_identity_lock_key(
            entity_id,
            marketplace_source,
            marketplace_resource_type,
            marketplace_resource_id,
            relationship,
            scope_type,
            scope_id,
            local_resource_type,
            component_key,
        ))))
    stmt = select(MarketplaceResourceLink).where(
        MarketplaceResourceLink.entity_id == entity_id,
        MarketplaceResourceLink.marketplace_source == marketplace_source,
        MarketplaceResourceLink.marketplace_resource_type
        == marketplace_resource_type,
        MarketplaceResourceLink.marketplace_resource_id
        == marketplace_resource_id,
        MarketplaceResourceLink.relationship == relationship,
        MarketplaceResourceLink.scope_type == scope_type,
        MarketplaceResourceLink.scope_id == scope_id,
        MarketplaceResourceLink.local_resource_type == local_resource_type,
        MarketplaceResourceLink.component_key == component_key,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def record_marketplace_resource_link(
    db: AsyncSession,
    *,
    entity_id: str,
    marketplace_source: str = MARKETPLACE_SOURCE_PLATFORM,
    marketplace_resource_type: str,
    marketplace_resource_id: str,
    relationship: str,
    scope_type: str,
    scope_id: str,
    local_resource_type: str,
    local_resource_id: str,
    component_key: str = ROOT_COMPONENT,
    marketplace_version: Optional[str] = None,
    linked_by: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> MarketplaceResourceLink:
    """Create or refresh one exact source/local identity relationship."""
    marketplace_resource_id = str(marketplace_resource_id or "").strip()
    marketplace_source = str(marketplace_source or "").strip()
    local_resource_id = str(local_resource_id or "").strip()
    if not marketplace_source or not marketplace_resource_id or not local_resource_id:
        raise ValueError("Marketplace source and Marketplace/local resource ids are required")

    if local_resource_type in _REUSABLE_LOCAL_RESOURCE_TYPES:
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    row = await get_marketplace_resource_link(
        db,
        entity_id=entity_id,
        marketplace_source=marketplace_source,
        marketplace_resource_type=marketplace_resource_type,
        marketplace_resource_id=marketplace_resource_id,
        relationship=relationship,
        scope_type=scope_type,
        scope_id=scope_id,
        local_resource_type=local_resource_type,
        component_key=component_key,
        for_update=True,
    )
    if local_resource_type in _REUSABLE_LOCAL_RESOURCE_TYPES:
        await lock_reusable_resource_reference(
            db,
            entity_id=entity_id,
            resource_type=local_resource_type,
            resource_id=local_resource_id,
        )
    if row is None:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(select(func.pg_advisory_xact_lock(
                _identity_lock_key(
                    "local",
                    entity_id,
                    relationship,
                    scope_type,
                    scope_id,
                    local_resource_type,
                    local_resource_id,
                )
            )))
        local_link = (await db.execute(
            select(MarketplaceResourceLink).where(
                MarketplaceResourceLink.entity_id == entity_id,
                MarketplaceResourceLink.relationship == relationship,
                MarketplaceResourceLink.scope_type == scope_type,
                MarketplaceResourceLink.scope_id == scope_id,
                MarketplaceResourceLink.local_resource_type == local_resource_type,
                MarketplaceResourceLink.local_resource_id == local_resource_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if local_link is not None:
            raise MarketplaceIdentityConflictError(
                "Local resource already has a different Marketplace identity "
                "in this scope"
            )
        row = MarketplaceResourceLink(
            entity_id=entity_id,
            marketplace_source=marketplace_source,
            marketplace_resource_type=marketplace_resource_type,
            marketplace_resource_id=marketplace_resource_id,
            relationship=relationship,
            scope_type=scope_type,
            scope_id=scope_id,
            local_resource_type=local_resource_type,
            local_resource_id=local_resource_id,
            component_key=component_key,
            marketplace_version=marketplace_version,
            linked_by=linked_by,
            link_metadata=dict(metadata or {}),
        )
        db.add(row)
    else:
        if row.local_resource_id != local_resource_id:
            raise MarketplaceIdentityConflictError(
                "Marketplace identity link cannot be reassigned to a different "
                "local resource"
            )
        if marketplace_version is not None:
            row.marketplace_version = marketplace_version
        if linked_by is not None:
            row.linked_by = linked_by
        if metadata:
            row.link_metadata = {
                **dict(row.link_metadata or {}),
                **dict(metadata),
            }
    await db.flush()
    return row


async def marketplace_link_for_local_resource(
    db: AsyncSession,
    *,
    entity_id: str,
    local_resource_type: str,
    local_resource_id: str,
    relationship: str,
) -> MarketplaceResourceLink | None:
    """Find the Marketplace counterpart recorded for a local resource id."""
    return (await db.execute(
        select(MarketplaceResourceLink)
        .where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.local_resource_type == local_resource_type,
            MarketplaceResourceLink.local_resource_id == local_resource_id,
            MarketplaceResourceLink.relationship == relationship,
        )
        .order_by(MarketplaceResourceLink.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def rekey_marketplace_resource_link(
    db: AsyncSession,
    *,
    entity_id: str,
    marketplace_source: str = MARKETPLACE_SOURCE_PLATFORM,
    marketplace_resource_type: str,
    marketplace_resource_id: str,
    relationship: str,
    scope_type: str,
    scope_id: str,
    local_resource_type: str,
    local_resource_id: str,
    component_key: str = ROOT_COMPONENT,
    marketplace_version: Optional[str] = None,
    linked_by: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> MarketplaceResourceLink:
    """Explicitly migrate one local resource to a canonical source id.

    Normal recording never changes identity. This separate operation is only
    for controlled historical alias migration (for example ``builtin:slug``
    to the resolved Marketplace row id) during Blueprint upgrade.
    """
    if local_resource_type in _REUSABLE_LOCAL_RESOURCE_TYPES:
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    target_predicate = and_(
        MarketplaceResourceLink.marketplace_source == marketplace_source,
        MarketplaceResourceLink.marketplace_resource_type
        == marketplace_resource_type,
        MarketplaceResourceLink.marketplace_resource_id
        == marketplace_resource_id,
        MarketplaceResourceLink.relationship == relationship,
        MarketplaceResourceLink.scope_type == scope_type,
        MarketplaceResourceLink.scope_id == scope_id,
        MarketplaceResourceLink.local_resource_type == local_resource_type,
        MarketplaceResourceLink.component_key == component_key,
    )
    local_predicate = and_(
        MarketplaceResourceLink.relationship == relationship,
        MarketplaceResourceLink.scope_type == scope_type,
        MarketplaceResourceLink.scope_id == scope_id,
        MarketplaceResourceLink.local_resource_type == local_resource_type,
        MarketplaceResourceLink.local_resource_id == local_resource_id,
    )
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(select(func.pg_advisory_xact_lock(_identity_lock_key(
            entity_id,
            marketplace_source,
            marketplace_resource_type,
            marketplace_resource_id,
            relationship,
            scope_type,
            scope_id,
            local_resource_type,
            component_key,
        ))))
        await db.execute(select(func.pg_advisory_xact_lock(_identity_lock_key(
            "local",
            entity_id,
            relationship,
            scope_type,
            scope_id,
            local_resource_type,
            local_resource_id,
        ))))
    locked_rows = list((await db.execute(
        select(MarketplaceResourceLink)
        .where(
            MarketplaceResourceLink.entity_id == entity_id,
            or_(target_predicate, local_predicate),
        )
        .order_by(MarketplaceResourceLink.id)
        .with_for_update()
    )).scalars().all())
    target = next(
        (
            row
            for row in locked_rows
            if row.marketplace_source == marketplace_source
            and row.marketplace_resource_type == marketplace_resource_type
            and row.marketplace_resource_id == marketplace_resource_id
            and row.relationship == relationship
            and row.scope_type == scope_type
            and row.scope_id == scope_id
            and row.local_resource_type == local_resource_type
            and row.component_key == component_key
        ),
        None,
    )
    if local_resource_type in _REUSABLE_LOCAL_RESOURCE_TYPES:
        await lock_reusable_resource_reference(
            db,
            entity_id=entity_id,
            resource_type=local_resource_type,
            resource_id=local_resource_id,
        )
    if target is not None and target.local_resource_id != local_resource_id:
        raise MarketplaceIdentityConflictError(
            "Canonical Marketplace identity already maps to a different local resource"
        )
    existing = next(
        (
            row
            for row in locked_rows
            if row.relationship == relationship
            and row.scope_type == scope_type
            and row.scope_id == scope_id
            and row.local_resource_type == local_resource_type
            and row.local_resource_id == local_resource_id
        ),
        None,
    )
    if existing is not None and (
        existing.marketplace_source != marketplace_source
        or existing.marketplace_resource_type != marketplace_resource_type
        or existing.marketplace_resource_id != marketplace_resource_id
        or existing.component_key != component_key
    ):
        await db.delete(existing)
        await db.flush()
    return await record_marketplace_resource_link(
        db,
        entity_id=entity_id,
        marketplace_source=marketplace_source,
        marketplace_resource_type=marketplace_resource_type,
        marketplace_resource_id=marketplace_resource_id,
        relationship=relationship,
        scope_type=scope_type,
        scope_id=scope_id,
        local_resource_type=local_resource_type,
        local_resource_id=local_resource_id,
        component_key=component_key,
        marketplace_version=marketplace_version,
        linked_by=linked_by,
        metadata=metadata,
    )
