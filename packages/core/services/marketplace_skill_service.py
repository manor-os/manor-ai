"""Install platform Marketplace Skill rows as entity-owned Skill rows."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.permission import Visibility
from packages.core.models.skill import Skill
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
    RELATIONSHIP_INSTALLED_FROM,
    RESOURCE_SKILL,
    SCOPE_ENTITY,
    get_marketplace_resource_link,
    record_marketplace_resource_link,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
)


async def ensure_marketplace_skill_installed(
    db: AsyncSession,
    *,
    entity_id: str,
    skill_id: str,
    owner_user_id: Optional[str] = None,
) -> Skill:
    """Return the local Skill installed from one exact Marketplace Skill id.

    Local Skill IDs are resolved by the normal binding path instead; accepting
    one here would let a local resource masquerade as a catalog identity.
    """
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    # See the Agent installer: source templates are immutable and intentionally
    # read without row locks so recursive installs cannot form a catalog cycle.
    source = (await db.execute(
        select(Skill)
        .where(
            Skill.id == skill_id,
            Skill.status == "active",
        )
    )).scalar_one_or_none()
    if source is None:
        raise ValueError("Skill not found")
    if source.entity_id == entity_id:
        raise ValueError("Skill id is local, not a Marketplace Skill")
    if source.entity_id is not None or not source.is_public:
        raise ValueError("Skill is not available from the Marketplace")

    link = await get_marketplace_resource_link(
        db,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id=source.id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        for_update=True,
    )
    installed = None
    if link is not None:
        installed = (await db.execute(
            select(Skill).where(
                Skill.id == link.local_resource_id,
                Skill.entity_id == entity_id,
                Skill.status == "active",
            )
        )).scalar_one_or_none()
        if installed is None:
            await db.delete(link)
            await db.flush()

    if installed is None:
        # A Skill's workspace_id is its home/ownership context, not an
        # invocation boundary. Exact source identity is sufficient for reuse.
        historical = list((await db.execute(
            select(Skill).where(
                Skill.entity_id == entity_id,
                Skill.status == "active",
                Skill.config["source_skill_id"].as_string() == source.id,
            ).limit(2).with_for_update()
        )).scalars().all())
        if len(historical) > 1:
            raise MarketplaceIdentityConflictError(
                "Marketplace Skill has multiple historical local installs"
            )
        installed = historical[0] if historical else None

    if installed is None:
        installed = Skill(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=owner_user_id,
            workspace_id=None,
            visibility=Visibility.ENTITY,
            name=source.name,
            slug=source.slug,
            display_name=source.display_name,
            description=source.description,
            system_prompt=source.system_prompt,
            tools=list(source.tools or []),
            input_schema=dict(source.input_schema or {}),
            output_format=source.output_format,
            category=source.category,
            tags=list(source.tags or []),
            is_public=False,
            version=source.version,
            config={
                **dict(source.config or {}),
                "source": "marketplace",
                "source_skill_id": source.id,
                "source_skill_slug": source.slug,
                "source_skill_version": source.version,
            },
            status="active",
        )
        db.add(installed)
        await db.flush()

    await record_marketplace_resource_link(
        db,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id=source.id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=installed.id,
        marketplace_version=source.version,
        linked_by=owner_user_id,
        metadata={"source_slug": source.slug},
    )
    return installed
