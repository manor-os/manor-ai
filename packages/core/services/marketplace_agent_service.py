"""Install Marketplace Agent templates into an entity-owned Agent catalog."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding
from packages.core.models.permission import Visibility
from packages.core.models.skill import AgentSkillBinding
from packages.core.models.workspace import Agent, AgentToolBinding
from packages.core.services.agent_runtime_config import normalize_agent_runtime_config
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
    RELATIONSHIP_INSTALLED_FROM,
    RESOURCE_AGENT,
    SCOPE_ENTITY,
    get_marketplace_resource_link,
    record_marketplace_resource_link,
)
from packages.core.services.reusable_resource_locks import (
    lock_agent_skill_binding_references,
    lock_reusable_resource_lifecycle,
)


async def ensure_marketplace_agent_installed(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    owner_user_id: Optional[str] = None,
    allow_local: bool = False,
) -> Agent:
    """Return an entity-owned Agent for one exact Marketplace Agent ID.

    Marketplace rows are immutable catalog templates. Subscribing installs one
    reusable entity-owned Agent, and Workspace subscriptions must point at that
    local identity instead of the platform template. Generic Agent subscription
    APIs may opt into accepting an already-local ID with ``allow_local=True``;
    Marketplace references must keep the default strict behavior.
    """
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    # Catalog templates are immutable. Do not retain a source-row lock here:
    # installing an Agent can recursively install Skills, and two entities may
    # otherwise lock the same Agent/Skill templates in opposite orders.
    source = (
        await db.execute(
            select(Agent)
            .where(
                Agent.id == agent_id,
                Agent.deleted_at.is_(None),
                Agent.status == "active",
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise ValueError("Agent not found")
    if source.entity_id == entity_id:
        if allow_local:
            return source
        raise ValueError("Agent id is local, not a Marketplace Agent")
    if source.entity_id is not None or not source.is_template or not source.is_public:
        raise ValueError("Agent is not available from the Marketplace")

    link = await get_marketplace_resource_link(
        db,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_AGENT,
        marketplace_resource_id=source.id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_AGENT,
        for_update=True,
    )
    installed = None
    if link is not None:
        installed = (await db.execute(
            select(Agent).where(
                Agent.id == link.local_resource_id,
                Agent.entity_id == entity_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if installed is None:
            await db.delete(link)
            await db.flush()

    if installed is None:
        # Historical installs predate the indexed relationship table but do
        # carry the exact Marketplace id. Backfill only from that id; a slug
        # or name is never sufficient to claim a local Agent. workspace_id is
        # deliberately not a predicate: it records the Agent's home Workspace,
        # while exact-ID subscriptions may reuse the Agent elsewhere.
        historical = list((await db.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.source == "marketplace",
                Agent.status == "active",
                Agent.deleted_at.is_(None),
                Agent.config["source_agent_id"].as_string() == source.id,
            ).limit(2).with_for_update()
        )).scalars().all())
        if len(historical) > 1:
            raise MarketplaceIdentityConflictError(
                "Marketplace Agent has multiple historical local installs"
            )
        installed = historical[0] if historical else None

    if installed is None:
        config = dict(normalize_agent_runtime_config(source.config))
        config.update(
            {
                "source_agent_id": source.id,
                "source_agent_slug": source.slug,
                "source_agent_version": source.version,
            }
        )
        installed = Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=owner_user_id,
            workspace_id=None,
            visibility=Visibility.ENTITY,
            name=source.name,
            slug=source.slug,
            description=source.description,
            avatar_url=source.avatar_url,
            system_prompt=source.system_prompt,
            config=config,
            is_template=False,
            is_public=False,
            category=source.category,
            tags=list(source.tags or []),
            source="marketplace",
            status="active",
            version=source.version,
        )
        db.add(installed)
        await db.flush()

    await record_marketplace_resource_link(
        db,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_AGENT,
        marketplace_resource_id=source.id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_AGENT,
        local_resource_id=installed.id,
        marketplace_version=source.version,
        linked_by=owner_user_id,
        metadata={"source_slug": source.slug},
    )

    await _copy_missing_bindings(
        db,
        entity_id=entity_id,
        source_agent_id=source.id,
        agent_id=installed.id,
    )
    return installed


async def _copy_missing_bindings(
    db: AsyncSession,
    *,
    entity_id: str,
    source_agent_id: str,
    agent_id: str,
) -> None:
    """Copy template capabilities once while preserving local customizations."""
    source_tool_ids = set(
        (
            await db.execute(
                select(AgentToolBinding.tool_id).where(
                    AgentToolBinding.agent_id == source_agent_id
                )
            )
        ).scalars()
    )
    installed_tool_ids = set(
        (
            await db.execute(
                select(AgentToolBinding.tool_id).where(
                    AgentToolBinding.agent_id == agent_id
                )
            )
        ).scalars()
    )
    db.add_all(
        AgentToolBinding(agent_id=agent_id, tool_id=tool_id)
        for tool_id in source_tool_ids - installed_tool_ids
    )

    source_skills = list(
        (
            await db.execute(
                select(AgentSkillBinding).where(
                    AgentSkillBinding.agent_id == source_agent_id
                )
            )
        )
        .scalars()
        .all()
    )
    installed_skill_ids = set(
        (
            await db.execute(
                select(AgentSkillBinding.skill_id).where(
                    AgentSkillBinding.agent_id == agent_id
                )
            )
        ).scalars()
    )
    from packages.core.services.marketplace_skill_service import (
        ensure_marketplace_skill_installed,
    )

    for binding in source_skills:
        try:
            installed_skill = await ensure_marketplace_skill_installed(
                db,
                entity_id=entity_id,
                skill_id=binding.skill_id,
            )
        except MarketplaceIdentityConflictError:
            raise
        except ValueError:
            continue
        if installed_skill.id in installed_skill_ids:
            continue
        await lock_agent_skill_binding_references(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
            skill_id=installed_skill.id,
        )
        db.add(AgentSkillBinding(
            id=generate_ulid(),
            agent_id=agent_id,
            skill_id=installed_skill.id,
            config=dict(binding.config or {}),
            status=binding.status,
        ))
        installed_skill_ids.add(installed_skill.id)

    source_mcp = list(
        (
            await db.execute(
                select(AgentMCPBinding).where(
                    AgentMCPBinding.agent_id == source_agent_id
                )
            )
        )
        .scalars()
        .all()
    )
    installed_mcp_ids = set(
        (
            await db.execute(
                select(AgentMCPBinding.mcp_server_id).where(
                    AgentMCPBinding.agent_id == agent_id
                )
            )
        ).scalars()
    )
    db.add_all(
        AgentMCPBinding(
            id=generate_ulid(),
            agent_id=agent_id,
            mcp_server_id=binding.mcp_server_id,
            allowed_tools=(
                list(binding.allowed_tools)
                if isinstance(binding.allowed_tools, list)
                else binding.allowed_tools
            ),
            config_override=dict(binding.config_override or {}),
            status=binding.status,
        )
        for binding in source_mcp
        if binding.mcp_server_id not in installed_mcp_ids
    )
    await db.flush()
