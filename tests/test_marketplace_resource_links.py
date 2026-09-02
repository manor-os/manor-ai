from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.skill import Skill
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
    RELATIONSHIP_INSTALLED_FROM,
    RESOURCE_SKILL,
    SCOPE_ENTITY,
    record_marketplace_resource_link,
    rekey_marketplace_resource_link,
)
from packages.core.services.reusable_resource_locks import (
    ReusableResourceUnavailableError,
)


async def _add_local_skill(
    db_session: AsyncSession,
    *,
    entity_id: str,
    skill_id: str,
) -> None:
    db_session.add(Skill(
        id=skill_id,
        entity_id=entity_id,
        name=f"Local skill {skill_id[-6:]}",
        system_prompt="Exercise exact Marketplace identity locking.",
        status="active",
    ))
    await db_session.flush()


async def test_record_rejects_missing_reusable_local_resource(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    missing_skill_id = generate_ulid()

    with pytest.raises(
        ReusableResourceUnavailableError,
        match="Skill is no longer available",
    ):
        await record_marketplace_resource_link(
            db_session,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_SKILL,
            marketplace_resource_id="missing-local-skill",
            relationship=RELATIONSHIP_INSTALLED_FROM,
            scope_type=SCOPE_ENTITY,
            scope_id=entity_id,
            local_resource_type=RESOURCE_SKILL,
            local_resource_id=missing_skill_id,
        )

    rows = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.local_resource_id == missing_skill_id,
        )
    )).scalars().all())
    assert rows == []


async def test_marketplace_namespace_is_part_of_exact_identity(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    shared_catalog_id = "shared-readable-id"
    platform_local_id = generate_ulid()
    manor_local_id = generate_ulid()
    await _add_local_skill(
        db_session,
        entity_id=entity_id,
        skill_id=platform_local_id,
    )
    await _add_local_skill(
        db_session,
        entity_id=entity_id,
        skill_id=manor_local_id,
    )

    await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_source="platform",
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id=shared_catalog_id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=platform_local_id,
    )
    await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_source="manor",
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id=shared_catalog_id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=manor_local_id,
    )
    await db_session.commit()

    rows = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
        )
    )).scalars().all())
    assert {
        (row.marketplace_source, row.marketplace_resource_id, row.local_resource_id)
        for row in rows
    } == {
        ("platform", shared_catalog_id, platform_local_id),
        ("manor", shared_catalog_id, manor_local_id),
    }


async def test_normal_record_cannot_reassign_identity_but_explicit_rekey_can(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    local_id = generate_ulid()
    replacement_local_id = generate_ulid()
    await _add_local_skill(
        db_session,
        entity_id=entity_id,
        skill_id=local_id,
    )
    await _add_local_skill(
        db_session,
        entity_id=entity_id,
        skill_id=replacement_local_id,
    )
    original = await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id="legacy-source-id",
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=local_id,
    )

    with pytest.raises(
        MarketplaceIdentityConflictError,
        match="cannot be reassigned",
    ):
        await record_marketplace_resource_link(
            db_session,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_SKILL,
            marketplace_resource_id="legacy-source-id",
            relationship=RELATIONSHIP_INSTALLED_FROM,
            scope_type=SCOPE_ENTITY,
            scope_id=entity_id,
            local_resource_type=RESOURCE_SKILL,
            local_resource_id=replacement_local_id,
        )

    canonical = await rekey_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id="canonical-source-id",
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=local_id,
    )
    await db_session.commit()

    assert canonical.id != original.id
    assert canonical.marketplace_resource_id == "canonical-source-id"
    assert canonical.local_resource_id == local_id
    rows = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
        )
    )).scalars().all())
    assert [row.id for row in rows] == [canonical.id]


async def test_normal_record_cannot_assign_two_sources_to_one_local_resource(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    local_id = generate_ulid()
    await _add_local_skill(
        db_session,
        entity_id=entity_id,
        skill_id=local_id,
    )
    await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id="first-source-id",
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=local_id,
    )

    with pytest.raises(
        MarketplaceIdentityConflictError,
        match="different Marketplace identity",
    ):
        await record_marketplace_resource_link(
            db_session,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_SKILL,
            marketplace_resource_id="second-source-id",
            relationship=RELATIONSHIP_INSTALLED_FROM,
            scope_type=SCOPE_ENTITY,
            scope_id=entity_id,
            local_resource_type=RESOURCE_SKILL,
            local_resource_id=local_id,
        )
