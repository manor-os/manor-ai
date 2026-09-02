from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.resolver import RuntimeResolver
from packages.core.ai.runtime.skills import resolve_skill_descriptors_for_envelope
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.models.base import generate_ulid
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.agent_provisioning_service import (
    CustomAgentSpec,
    provision_custom_agent,
)
from packages.core.services.marketplace_skill_service import (
    ensure_marketplace_skill_installed,
)
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
)
from packages.core.services.skill_service import invoke_skill


async def test_runtime_requester_skill_discovery_and_invocation_enforce_resource_scope(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    foreign_entity_id = generate_ulid()
    requester = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"runtime-requester-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    owner = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"runtime-owner-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Runtime Skill Scope",
        status="active",
    )
    private_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner.id,
        visibility="private",
        name="Owner Private Runtime Skill",
        slug=f"owner-private-runtime-{generate_ulid()}",
        system_prompt="Private runtime behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    entity_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner.id,
        visibility="entity",
        name="Entity Runtime Skill",
        slug=f"entity-runtime-{generate_ulid()}",
        system_prompt="Entity-visible runtime behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    foreign_skill = Skill(
        id=generate_ulid(),
        entity_id=foreign_entity_id,
        visibility="entity",
        name="Foreign Runtime Skill",
        slug=f"foreign-runtime-{generate_ulid()}",
        system_prompt="Foreign behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    db_session.add_all([
        requester,
        owner,
        workspace,
        private_skill,
        entity_skill,
        foreign_skill,
    ])
    await db_session.commit()

    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id=entity_id,
            user_id=requester.id,
            workspace_id=workspace.id,
        ),
        allowed_tool_names={"invoke_skill"},
    )
    descriptors = await resolve_skill_descriptors_for_envelope(
        db_session,
        envelope,
        limit=100,
    )
    descriptor_ids = {descriptor.id for descriptor in descriptors}

    assert entity_skill.id in descriptor_ids
    assert private_skill.id not in descriptor_ids

    denied_private = await invoke_skill(
        db_session,
        private_skill.id,
        entity_id,
        "Run the private Skill.",
        user_id=requester.id,
        workspace_id=workspace.id,
    )
    denied_foreign = await invoke_skill(
        db_session,
        foreign_skill.id,
        entity_id,
        "Run the foreign Skill.",
        user_id=requester.id,
        workspace_id=workspace.id,
    )
    denied_manual_private = await invoke_skill(
        db_session,
        private_skill.id,
        entity_id,
        "Run the manually selected private Skill.",
        agent_id=generate_ulid(),
        user_id=requester.id,
        workspace_id=workspace.id,
        manual_skill_selected=True,
    )

    assert denied_private["code"] == "skill_not_allowed"
    assert denied_foreign["code"] == "skill_not_found"
    assert denied_manual_private["code"] == "skill_not_allowed"


async def test_runtime_requester_cannot_bind_another_users_private_skill_by_id(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    requester = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"requester-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    owner = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"owner-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    private_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner.id,
        workspace_id=generate_ulid(),
        visibility="private",
        name="Private Skill",
        slug=f"private-{generate_ulid()}",
        system_prompt="Private behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    entity_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner.id,
        visibility="entity",
        name="Entity Skill",
        slug=f"entity-{generate_ulid()}",
        system_prompt="Entity-visible behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    colliding_slug_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner.id,
        visibility="entity",
        name="Collision Skill",
        slug=private_skill.id,
        system_prompt="Must not replace a denied exact identity.",
        tools=[],
        is_public=False,
        status="active",
    )
    db_session.add_all([
        requester,
        owner,
        private_skill,
        entity_skill,
        colliding_slug_skill,
    ])
    await db_session.commit()

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        requester_user_id=requester.id,
        spec=CustomAgentSpec(
            agent_name="Authorized Skills Only",
            system_prompt="Bind only Skills the requesting user may read.",
            skill_bindings=[private_skill.id, entity_skill.id],
        ),
    )
    await db_session.commit()

    binding_skill_ids = set((await db_session.execute(
        select(AgentSkillBinding.skill_id).where(
            AgentSkillBinding.agent_id == result.agent_id,
        )
    )).scalars().all())
    assert binding_skill_ids == {entity_skill.id}
    assert result.bound_skills == [entity_skill.slug]
    assert result.warnings == [f"skill not found: {private_skill.id}"]


async def test_exact_marketplace_skill_id_installs_local_copy_before_binding(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Research",
        slug=f"marketplace-research-{entity_id}",
        system_prompt="Research from approved sources.",
        tools=[],
        is_public=True,
        status="active",
    )
    db_session.add(source)
    await db_session.commit()

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name="Research Agent",
            system_prompt="Use the selected research Skill.",
            skill_binding_refs=[
                {
                    "slug": source.slug,
                    "marketplace_source": "platform",
                    "marketplace_id": source.id,
                }
            ],
        ),
    )
    await db_session.commit()

    binding = (await db_session.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == result.agent_id,
        )
    )).scalar_one()
    installed = await db_session.get(Skill, binding.skill_id)
    assert installed is not None
    assert installed.id != source.id
    assert installed.entity_id == entity_id
    assert installed.config["source_skill_id"] == source.id
    assert binding.config["contexts"][0]["match"] == {
        "type": "exact_marketplace_skill_binding",
        "marketplace_source": "platform",
        "marketplace_id": source.id,
        "requested_slug": source.slug,
    }
    link = (await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_id == source.id,
            MarketplaceResourceLink.local_resource_id == installed.id,
        )
    )).scalar_one()
    assert link.relationship == "installed_from"


async def test_exact_marketplace_skill_id_rejects_local_skill(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    local = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        name="Local Skill",
        slug=f"local-skill-{entity_id}",
        system_prompt="Local behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    db_session.add(local)
    await db_session.flush()

    with pytest.raises(ValueError, match="local, not a Marketplace Skill"):
        await ensure_marketplace_skill_installed(
            db_session,
            entity_id=entity_id,
            skill_id=local.id,
        )

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name="Strict Marketplace Agent",
            system_prompt="Do not bind a local Skill as Marketplace content.",
            skill_binding_refs=[{
                "slug": local.slug,
                "marketplace_source": "platform",
                "marketplace_id": local.id,
            }],
        ),
    )
    bindings = list((await db_session.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == result.agent_id,
        )
    )).scalars().all())
    assert bindings == []
    assert result.warnings == [f"skill not found: platform:{local.id}"]


async def test_marketplace_skill_install_rejects_duplicate_historical_copies(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=f"duplicate-skill-source-{entity_id}",
        system_prompt="Marketplace behavior.",
        tools=[],
        is_public=True,
        status="active",
    )
    historical = [
        Skill(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=None,
            name=f"Historical Skill {index}",
            slug=f"historical-skill-{index}-{entity_id}",
            system_prompt="Historical behavior.",
            tools=[],
            is_public=False,
            config={"source_skill_id": source.id},
            status="active",
        )
        for index in range(2)
    ]
    db_session.add_all([source, *historical])
    await db_session.flush()

    with pytest.raises(
        MarketplaceIdentityConflictError,
        match="multiple historical local installs",
    ):
        await ensure_marketplace_skill_installed(
            db_session,
            entity_id=entity_id,
            skill_id=source.id,
        )


async def test_legacy_exact_marketplace_skill_conflict_fails_provisioning(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=f"legacy-conflict-source-{entity_id}",
        system_prompt="Marketplace behavior.",
        tools=[],
        is_public=True,
        status="active",
    )
    historical = [
        Skill(
            id=generate_ulid(),
            entity_id=entity_id,
            name=f"Historical Skill {index}",
            slug=f"legacy-conflict-install-{index}-{entity_id}",
            system_prompt="Historical behavior.",
            tools=[],
            is_public=False,
            config={"source_skill_id": source.id},
            status="active",
        )
        for index in range(2)
    ]
    db_session.add_all([source, *historical])
    await db_session.flush()

    with pytest.raises(
        MarketplaceIdentityConflictError,
        match="multiple historical local installs",
    ):
        await provision_custom_agent(
            db_session,
            entity_id=entity_id,
            spec=CustomAgentSpec(
                agent_name="Conflict-aware Agent",
                system_prompt="Do not hide Marketplace identity conflicts.",
                skill_bindings=[source.id],
            ),
        )


async def test_marketplace_skill_reuses_exact_historical_copy_with_home_workspace(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=f"workspace-home-source-{entity_id}",
        system_prompt="Marketplace behavior.",
        tools=[],
        is_public=True,
        status="active",
    )
    historical = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        name="Historical Skill",
        slug=f"workspace-home-install-{entity_id}",
        system_prompt="Historical behavior.",
        tools=[],
        is_public=False,
        config={"source_skill_id": source.id},
        status="active",
    )
    db_session.add_all([source, historical])
    await db_session.flush()

    installed = await ensure_marketplace_skill_installed(
        db_session,
        entity_id=entity_id,
        skill_id=source.id,
    )

    assert installed.id == historical.id
    link = (await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_id == source.id,
        )
    )).scalar_one()
    assert link.local_resource_id == historical.id


async def test_local_skill_id_can_be_reused_across_workspaces(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    home_workspace_id = generate_ulid()
    target_workspace_id = generate_ulid()
    local = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=home_workspace_id,
        name="Reusable Local Skill",
        slug=f"reusable-local-skill-{entity_id}",
        system_prompt="Reusable behavior.",
        tools=[],
        is_public=False,
        status="active",
    )
    db_session.add(local)
    await db_session.flush()

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name="Cross-Workspace Agent",
            system_prompt="Use the exact reusable local Skill.",
            workspace_id=target_workspace_id,
            skill_bindings=[local.id],
        ),
    )

    binding = (await db_session.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == result.agent_id,
            AgentSkillBinding.skill_id == local.id,
        )
    )).scalar_one()
    assert binding.skill_id == local.id


async def test_legacy_skill_id_does_not_cross_entity(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    foreign = Skill(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Other Entity Skill",
        slug=f"other-entity-skill-{entity_id}",
        system_prompt="Other entity behavior.",
        tools=[],
        is_public=True,
        status="active",
    )
    db_session.add(foreign)
    await db_session.flush()

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name="Entity-isolated Agent",
            system_prompt="Only use Skills owned by this entity.",
            skill_bindings=[foreign.id],
        ),
    )
    bindings = list((await db_session.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == result.agent_id,
        )
    )).scalars().all())

    assert bindings == []
    assert result.warnings == [f"skill not found: {foreign.id}"]


async def test_ambiguous_legacy_skill_slug_is_not_bound(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    shared_slug = f"ambiguous-skill-{entity_id}"
    db_session.add_all([
        Skill(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Entity Skill",
            slug=shared_slug,
            system_prompt="Entity implementation.",
            tools=[],
            is_public=False,
            status="active",
        ),
        Skill(
            id=generate_ulid(),
            entity_id=None,
            name="Marketplace Skill",
            slug=shared_slug,
            system_prompt="Marketplace implementation.",
            tools=[],
            is_public=True,
            status="active",
        ),
    ])
    await db_session.commit()

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name="Ambiguous Agent",
            system_prompt="Do not guess which Skill to use.",
            skill_bindings=[shared_slug],
        ),
    )
    await db_session.commit()

    bindings = list((await db_session.execute(
        select(AgentSkillBinding).where(
            AgentSkillBinding.agent_id == result.agent_id,
        )
    )).scalars().all())
    assert bindings == []
    assert result.warnings == [f"skill not found: {shared_slug}"]


async def test_blueprint_components_are_idempotent_per_workspace_and_isolated_across_workspaces(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    blueprint_id = f"blueprint:{generate_ulid()}"
    workspaces = [
        Workspace(
            id=generate_ulid(),
            entity_id=entity_id,
            name=f"Blueprint Workspace {index}",
            kind="general",
            operating_context="",
            primary_work="",
            operating_model={},
            settings={},
        )
        for index in range(2)
    ]
    db_session.add_all(workspaces)
    await db_session.commit()

    def spec(workspace_id: str) -> CustomAgentSpec:
        return CustomAgentSpec(
            agent_name="Blueprint Agent",
            agent_slug="blueprint-agent",
            system_prompt="Use the Blueprint-owned Skill.",
            workspace_id=workspace_id,
            source_blueprint_id=blueprint_id,
            source_blueprint_component_key="blueprint-agent",
            missing_skill_specs=[
                {
                    "slug": "blueprint-skill",
                    "name": "Blueprint Skill",
                    "system_prompt": "Perform the Blueprint procedure.",
                    "source_blueprint_component_key": "blueprint-skill",
                }
            ],
        )

    first = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=spec(workspaces[0].id),
    )
    await db_session.commit()
    retried = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=spec(workspaces[0].id),
    )
    await db_session.commit()
    second = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=spec(workspaces[1].id),
    )
    await db_session.commit()

    assert retried.agent_id == first.agent_id
    assert second.agent_id != first.agent_id
    skills = list((await db_session.execute(
        select(Skill).where(
            Skill.entity_id == entity_id,
            Skill.workspace_id.in_([workspace.id for workspace in workspaces]),
        )
    )).scalars().all())
    assert len(skills) == 2
    assert {skill.workspace_id for skill in skills} == {
        workspace.id for workspace in workspaces
    }
    links = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_id == blueprint_id,
            MarketplaceResourceLink.relationship == "installed_component",
        )
    )).scalars().all())
    assert {
        (link.scope_id, link.local_resource_type)
        for link in links
    } == {
        (workspaces[0].id, "agent"),
        (workspaces[0].id, "skill"),
        (workspaces[1].id, "agent"),
        (workspaces[1].id, "skill"),
    }


