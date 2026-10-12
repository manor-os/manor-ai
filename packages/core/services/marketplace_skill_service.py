"""Resolve platform Marketplace Skills for entity use."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.permission import Visibility
from packages.core.models.skill import Skill
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
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
    """Return the Skill for one exact Marketplace Skill id.

    Local Skill IDs are resolved by the normal binding path instead; accepting
    one here would let a local resource masquerade as a catalog identity.
    Built-ins remain platform-owned and are bound directly, without a copy.
    """
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    # See the Agent installer: source templates are immutable and intentionally
    # read without row locks so recursive installs cannot form a catalog cycle.
    if skill_id.startswith("builtin:"):
        from packages.core.services.builtin_skill_loader import resolve_packaged_builtin_skill

        return await resolve_packaged_builtin_skill(db, skill_id.removeprefix("builtin:"))
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
    if (source.config or {}).get("source") == "builtin":
        return source

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


def portable_skill_marketplace_source(
    skill: Skill, *, source_link: MarketplaceResourceLink | None = None,
) -> tuple[str | None, str | None]:
    """Project the existing Marketplace provenance fields used by Blueprint export."""
    config = skill.config or {}
    if source_link is not None:
        return source_link.marketplace_source, str(source_link.marketplace_resource_id).strip() or None
    # Platform copies inherit the source config before adding their own exact
    # source ID. An inherited Manor catalog hint is not the copy's identity.
    if config.get("source") == "marketplace" and config.get("source_skill_id"):
        return "platform", str(config["source_skill_id"]).strip() or None
    source_id = str(config.get("marketplace_id") or config.get("source_skill_id") or
                    (skill.id if skill.entity_id is None else "")).strip()
    source = ("manor" if config.get("marketplace_id") else "platform" if source_id else None)
    return source, source_id or None



async def resolve_bound_blueprint_skill(
    db: AsyncSession, *, entity_id: str, skills: Iterable[Skill],
    marketplace_id: str, marketplace_source: str | None,
) -> str | None:
    """Return one eligible bound identity, never a slug or ambiguous install."""
    eligible = {skill.id: skill for skill in skills if skill.status == "active" and (
        skill.entity_id == entity_id or (skill.entity_id is None and skill.is_public)
    )}
    if marketplace_source not in {None, "manor", "platform"} or not eligible:
        return None
    if marketplace_id.startswith("builtin:"):
        if marketplace_source not in {None, "platform"}:
            return None
        from packages.core.services.builtin_skill_loader import resolve_packaged_builtin_skill

        try:
            canonical = await resolve_packaged_builtin_skill(db, marketplace_id.removeprefix("builtin:"))
        except ValueError:
            return None
        return canonical.id if canonical.id in eligible else None

    links = list((await db.scalars(select(MarketplaceResourceLink).where(
        MarketplaceResourceLink.entity_id == entity_id,
        MarketplaceResourceLink.relationship == "installed_from",
        MarketplaceResourceLink.local_resource_type == "skill",
        MarketplaceResourceLink.local_resource_id.in_(eligible),
    ))).all())
    by_skill = defaultdict(list)
    for link in links:
        by_skill[link.local_resource_id].append(link)
    matches = set()
    for skill in eligible.values():
        source_links = by_skill[skill.id]
        identities = {(link.marketplace_source, link.marketplace_resource_id)
                      for link in source_links}
        if len(identities) > 1 or any(link.marketplace_resource_type != "skill" for link in source_links):
            continue
        config = skill.config or {}
        # Explicit source hints must agree with the exporter's established
        # Manor/platform provenance fields; conflicting evidence fails closed.
        config_identity = (portable_skill_marketplace_source(skill)
                           if config.get("marketplace_id") or config.get("source_skill_id") else None)
        if config_identity and identities and identities != {config_identity}:
            continue
        source, source_id = portable_skill_marketplace_source(
            skill, source_link=source_links[0] if source_links else None,
        )
        platform_copy = config.get("source") == "marketplace" and config.get("source_skill_id")
        if not platform_copy and config.get("marketplace_source") and config["marketplace_source"] != source:
            continue
        if source in {"manor", "platform"} and source_id == marketplace_id and (
            marketplace_source is None or source == marketplace_source
        ):
            matches.add(skill.id)
    return next(iter(matches)) if len(matches) == 1 else None
