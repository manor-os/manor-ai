"""Marketplace Agents install into an entity before Workspace deployment."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from packages.core.ai.tools.workspace_arch_tools import (
    PROPOSE_AGENT_MAPPING_SCHEMA,
    _lint_draft,
    _propose_agent_mapping,
    _search_capabilities,
    _search_entity_agents,
)
from packages.core.blueprints.installer import install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
)
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.marketplace_agent_service import (
    ensure_marketplace_agent_installed,
)
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
)
from packages.core.services.workspace_setup_service import (
    WorkspaceSetupSession,
    finalize_setup,
)
from tests.test_blueprint_installer_embedded import _base_payload


async def test_marketplace_install_reuses_local_agent_and_copies_bindings(db_session):
    entity_id = generate_ulid()
    tool = ToolDefinition(
        id=generate_ulid(),
        name=f"marketplace.tool.{entity_id}",
        display_name="Marketplace Tool",
    )
    skill = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=f"marketplace-skill-{entity_id}",
        system_prompt="Use the Marketplace Skill.",
        tools=[],
        is_public=True,
        status="active",
    )
    server = MCPServer(
        id=generate_ulid(),
        server_key=f"marketplace_{entity_id.lower()}",
        name="Marketplace MCP",
        transport="http",
        endpoint="https://example.test/mcp",
        auth_type="none",
        status="active",
    )
    template = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Operator",
        slug=f"marketplace-operator-{entity_id}",
        description="Operate the Marketplace workflow.",
        avatar_url="https://example.test/operator.png",
        system_prompt="Operate carefully.",
        config={"temperature": 0.2},
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
        version="2.0",
    )
    db_session.add_all([tool, skill, server, template])
    await db_session.flush()
    db_session.add_all(
        [
            AgentToolBinding(agent_id=template.id, tool_id=tool.id),
            AgentSkillBinding(
                id=generate_ulid(),
                agent_id=template.id,
                skill_id=skill.id,
                config={"mode": "guided"},
                status="active",
            ),
            AgentMCPBinding(
                id=generate_ulid(),
                agent_id=template.id,
                mcp_server_id=server.id,
                allowed_tools=["run"],
                config_override={"region": "us"},
                status="active",
            ),
        ]
    )
    await db_session.flush()

    first = await ensure_marketplace_agent_installed(
        db_session,
        entity_id=entity_id,
        agent_id=template.id,
        owner_user_id=generate_ulid(),
    )
    second = await ensure_marketplace_agent_installed(
        db_session,
        entity_id=entity_id,
        agent_id=template.id,
    )

    assert first.id == second.id
    assert first.id != template.id
    assert first.entity_id == entity_id
    assert first.name == template.name
    assert first.avatar_url == template.avatar_url
    assert first.source == "marketplace"
    assert first.is_template is False
    assert first.is_public is False
    assert first.config["source_agent_id"] == template.id
    assert set(
        (
            await db_session.execute(
                select(AgentToolBinding.tool_id).where(
                    AgentToolBinding.agent_id == first.id
                )
            )
        ).scalars()
    ) == {tool.id}
    installed_skill_ids = set(
        (
            await db_session.execute(
                select(AgentSkillBinding.skill_id).where(
                    AgentSkillBinding.agent_id == first.id
                )
            )
        ).scalars()
    )
    assert len(installed_skill_ids) == 1
    installed_skill_id = next(iter(installed_skill_ids))
    assert installed_skill_id != skill.id
    installed_skill = await db_session.get(Skill, installed_skill_id)
    assert installed_skill is not None
    assert installed_skill.entity_id == entity_id
    assert installed_skill.config["source_skill_id"] == skill.id
    links = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.relationship == "installed_from",
        )
    )).scalars().all())
    assert {
        (link.marketplace_resource_type, link.marketplace_resource_id,
         link.local_resource_type, link.local_resource_id)
        for link in links
    } >= {
        ("agent", template.id, "agent", first.id),
        ("skill", skill.id, "skill", installed_skill.id),
    }
    assert set(
        (
            await db_session.execute(
                select(AgentMCPBinding.mcp_server_id).where(
                    AgentMCPBinding.agent_id == first.id
                )
            )
        ).scalars()
    ) == {server.id}


async def test_workspace_architect_prefers_installed_marketplace_identity(db_session):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    template = Agent(
        id=f"tmpl_{generate_ulid()[5:]}",
        entity_id=None,
        name="Atlas",
        slug=f"atlas-{entity_id}",
        system_prompt="Research the market.",
        config={},
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    db_session.add(template)
    await db_session.flush()
    installed = await ensure_marketplace_agent_installed(
        db_session,
        entity_id=entity_id,
        agent_id=template.id,
        owner_user_id=user_id,
    )

    inventory = json.loads(
        await _search_entity_agents(
            db_session,
            entity_id=entity_id,
            user_id=user_id,
        )
    )
    candidates = {candidate["id"]: candidate for candidate in inventory["agents"]}

    assert installed.id in candidates
    assert template.id not in candidates
    assert candidates[installed.id]["source"] == "marketplace"
    assert candidates[installed.id]["scope"] == "entity"
    assert candidates[installed.id]["source_agent_id"] == template.id


async def test_same_slug_marketplace_agents_keep_distinct_exact_id_installs(
    db_session,
):
    entity_id = generate_ulid()
    shared_slug = f"shared-marketplace-slug-{entity_id}"
    sources = [
        Agent(
            id=generate_ulid(),
            entity_id=None,
            name=f"Marketplace Agent {index}",
            slug=shared_slug,
            system_prompt=f"Source {index}",
            config={},
            is_template=True,
            is_public=True,
            source="marketplace",
            status="active",
        )
        for index in range(2)
    ]
    db_session.add_all(sources)
    await db_session.flush()

    installed = [
        await ensure_marketplace_agent_installed(
            db_session,
            entity_id=entity_id,
            agent_id=source.id,
        )
        for source in sources
    ]

    assert installed[0].id != installed[1].id
    assert {
        row.config["source_agent_id"] for row in installed
    } == {source.id for source in sources}


async def test_marketplace_agent_id_rejects_local_agent(db_session):
    entity_id = generate_ulid()
    local = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        name="Local Agent",
        system_prompt="Local behavior.",
        status="active",
    )
    db_session.add(local)
    await db_session.flush()

    with pytest.raises(ValueError, match="local, not a Marketplace Agent"):
        await ensure_marketplace_agent_installed(
            db_session,
            entity_id=entity_id,
            agent_id=local.id,
        )

    resolved = await ensure_marketplace_agent_installed(
        db_session,
        entity_id=entity_id,
        agent_id=local.id,
        allow_local=True,
    )
    assert resolved.id == local.id


async def test_marketplace_agent_install_rejects_duplicate_historical_copies(
    db_session,
):
    entity_id = generate_ulid()
    source = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=f"duplicate-agent-source-{entity_id}",
        system_prompt="Marketplace behavior.",
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    historical = [
        Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=None,
            name=f"Historical Install {index}",
            slug=f"historical-agent-{index}-{entity_id}",
            system_prompt="Historical behavior.",
            config={"source_agent_id": source.id},
            source="marketplace",
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
        await ensure_marketplace_agent_installed(
            db_session,
            entity_id=entity_id,
            agent_id=source.id,
        )


async def test_marketplace_agent_reuses_exact_historical_copy_with_home_workspace(
    db_session,
):
    entity_id = generate_ulid()
    source = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=f"workspace-home-source-{entity_id}",
        system_prompt="Marketplace behavior.",
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    historical = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        name="Historical Install",
        slug=f"workspace-home-install-{entity_id}",
        system_prompt="Historical behavior.",
        config={"source_agent_id": source.id},
        source="marketplace",
        status="active",
    )
    db_session.add_all([source, historical])
    await db_session.flush()

    installed = await ensure_marketplace_agent_installed(
        db_session,
        entity_id=entity_id,
        agent_id=source.id,
    )

    assert installed.id == historical.id
    link = (await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_id == source.id,
        )
    )).scalar_one()
    assert link.local_resource_id == historical.id


async def test_workspace_architect_hides_private_global_agents(db_session):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    local = Agent(
        entity_id=entity_id,
        owner_user_id=user_id,
        name="Local Agent",
        status="active",
    )
    public_template = Agent(
        entity_id=None,
        name="Public Marketplace Agent",
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    private_template = Agent(
        entity_id=None,
        name="Private Platform Agent",
        system_prompt="Private platform instructions must not be exposed.",
        is_template=True,
        is_public=False,
        status="active",
    )
    db_session.add_all([local, public_template, private_template])
    await db_session.flush()

    inventory = json.loads(
        await _search_entity_agents(
            db_session,
            entity_id=entity_id,
            user_id=user_id,
        )
    )
    candidate_ids = {candidate["id"] for candidate in inventory["agents"]}

    assert local.id in candidate_ids
    assert public_template.id in candidate_ids
    assert private_template.id not in candidate_ids


async def test_workspace_architect_filters_reusable_resources_by_caller_access(
    db_session,
):
    entity_id = generate_ulid()
    owner_user_id = generate_ulid()
    caller_user_id = generate_ulid()
    private_agent = Agent(
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        visibility="private",
        name="Private Agent",
        slug=f"private-agent-{entity_id}",
        system_prompt="Do not expose this prompt.",
        status="active",
    )
    caller_agent = Agent(
        entity_id=entity_id,
        owner_user_id=caller_user_id,
        visibility="private",
        name="Caller Agent",
        slug=f"caller-agent-{entity_id}",
        system_prompt="Visible to its owner.",
        status="active",
    )
    private_skill = Skill(
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        visibility="private",
        name="Private Skill",
        slug=f"private-skill-{entity_id}",
        system_prompt="Do not expose these instructions.",
        tools=[],
        status="active",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=caller_user_id,
        fields={
            "name": "Access-safe Draft",
            "kind": "operations",
            "operating_context": "Verify reusable resource access.",
            "primary_work": "Build an authorized workspace configuration.",
            "services": [
                {
                    "service_key": "existing",
                    "name": "Existing Agent",
                    "description": "Use an existing reusable Agent.",
                    "autonomy_level": "supervised",
                    "owner_role": "operator",
                },
                {
                    "service_key": "custom",
                    "name": "Custom Agent",
                    "description": "Build an Agent from reusable Skills.",
                    "autonomy_level": "supervised",
                    "owner_role": "operator",
                },
            ],
            "agent_mappings": [
                {
                    "service_key": "existing",
                    "strategy": "match",
                    "agent_id": private_agent.id,
                },
                {
                    "service_key": "custom",
                    "strategy": "create_custom",
                    "create_agent_draft": {
                        "agent_name": "Custom Agent",
                        "system_prompt": "Use only authorized reusable Skills.",
                        "skill_bindings": [private_skill.id],
                    },
                },
            ],
            "goals": [],
        },
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([private_agent, caller_agent, private_skill, draft])
    await db_session.flush()
    db_session.add(AgentSkillBinding(
        id=generate_ulid(),
        agent_id=caller_agent.id,
        skill_id=private_skill.id,
        status="active",
    ))
    await db_session.flush()

    inventory = json.loads(await _search_entity_agents(
        db_session,
        entity_id=entity_id,
        user_id=caller_user_id,
        draft_id=draft.id,
    ))
    candidates = {candidate["id"]: candidate for candidate in inventory["agents"]}
    assert private_agent.id not in candidates
    assert candidates[caller_agent.id]["skill_bindings"] == []

    capabilities = json.loads(await _search_capabilities(
        db_session,
        entity_id=entity_id,
        user_id=caller_user_id,
        draft_id=draft.id,
    ))
    assert private_skill.id not in {
        skill["id"] for skill in capabilities["skills"]
    }

    proposed = json.loads(await _propose_agent_mapping(
        db_session,
        entity_id=entity_id,
        user_id=caller_user_id,
        draft_id=draft.id,
        service_key="existing",
        agent_id=private_agent.id,
        rationale="Attempt to bind an inaccessible Agent.",
    ))
    assert proposed == {
        "ok": False,
        "error": "agent is not accessible",
        "got": private_agent.id,
    }

    lint = json.loads(await _lint_draft(
        db_session,
        entity_id=entity_id,
        user_id=caller_user_id,
        draft_id=draft.id,
    ))
    p0_locations = {
        issue["where"]
        for issue in lint["issues"]
        if issue["severity"] == "P0"
    }
    assert "agent_mappings.existing.agent_id" in p0_locations
    assert "agent_mappings.custom.skill_bindings" in p0_locations


async def test_workspace_architect_accepts_marketplace_template_id(db_session):
    entity_id = generate_ulid()
    template = Agent(
        id=f"tmpl_{generate_ulid()[5:]}",
        entity_id=None,
        name="Marketplace Researcher",
        slug=f"marketplace-researcher-{entity_id}",
        system_prompt="Research the market.",
        config={},
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=generate_ulid(),
        fields={
            "services": [
                {
                    "service_key": "market_research",
                    "name": "Market Research",
                    "description": "Research the market.",
                    "autonomy_level": "full",
                    "owner_role": "researcher",
                }
            ],
            "agent_mappings": [],
        },
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([template, draft])
    await db_session.flush()

    agent_id_schema = PROPOSE_AGENT_MAPPING_SCHEMA["function"]["parameters"][
        "properties"
    ]["agent_id"]
    assert "pattern" not in agent_id_schema
    result = json.loads(
        await _propose_agent_mapping(
            db_session,
            entity_id=entity_id,
            user_id=draft.user_id,
            draft_id=draft.id,
            service_key="market_research",
            agent_id=template.id,
            rationale="Use the selected Marketplace Agent.",
        )
    )

    assert result["ok"] is True
    assert result["agent_id"] == template.id
    assert draft.fields["agent_mappings"][0]["agent_id"] == template.id


async def test_blueprint_marketplace_subscription_uses_my_agents_identity(db_session):
    entity_id = generate_ulid()
    template = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Blueprint Marketplace Agent",
        slug=f"blueprint-marketplace-{entity_id}",
        system_prompt="Run the Blueprint service.",
        config={},
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    db_session.add(template)
    await db_session.flush()
    payload = _base_payload(
        **{
            "recipe.subscriptions": [
                {
                    "agent_slug": template.slug,
                    "service_key": "marketplace_service",
                }
            ]
        }
    )

    first = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=generate_ulid(),
    )
    second = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=generate_ulid(),
    )

    subscriptions = list(
        (
            await db_session.execute(
                select(AgentSubscription).where(
                    AgentSubscription.id.in_(
                        first.subscription_ids + second.subscription_ids
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(subscriptions) == 2
    assert {subscription.agent_id for subscription in subscriptions} != {template.id}
    assert len({subscription.agent_id for subscription in subscriptions}) == 1

    installed_id = subscriptions[0].agent_id
    installed = (
        await db_session.execute(
            select(Agent).where(
                Agent.id == installed_id,
                Agent.entity_id == entity_id,
            )
        )
    ).scalar_one()
    assert installed.config["source_agent_id"] == template.id
    assert installed.source == "marketplace"


async def test_blueprint_does_not_install_private_global_agent(db_session):
    entity_id = generate_ulid()
    template = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Private Global Agent",
        slug=f"private-global-agent-{entity_id}",
        system_prompt="Do not expose this Agent.",
        config={},
        is_template=True,
        is_public=False,
        source="marketplace",
        status="active",
    )
    db_session.add(template)
    await db_session.flush()
    payload = _base_payload(
        **{
            "recipe.subscriptions": [
                {
                    "agent_slug": template.slug,
                    "service_key": "private_service",
                }
            ]
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=generate_ulid(),
    )

    assert result.subscription_ids == []
    assert any(todo.kind == "missing_agent" for todo in result.todos)
    local_agents = list(
        (
            await db_session.execute(
                select(Agent).where(Agent.entity_id == entity_id)
            )
        )
        .scalars()
        .all()
    )
    assert local_agents == []


async def test_legacy_blueprint_agent_slug_resolves_accessible_home_agent(
    db_session,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    foreign_workspace_id = generate_ulid()
    foreign_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user_id,
        workspace_id=foreign_workspace_id,
        visibility="private",
        name="Workspace-home Agent",
        slug=f"workspace-private-agent-{entity_id}",
        system_prompt="Reuse this Agent when the caller can read it.",
        status="active",
    )
    db_session.add(foreign_agent)
    await db_session.flush()
    payload = _base_payload(**{
        "recipe.subscriptions": [{
            "agent_slug": foreign_agent.slug,
            "service_key": "private_service",
        }],
    })

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=user_id,
    )

    assert len(result.subscription_ids) == 1
    subscription = await db_session.get(
        AgentSubscription,
        result.subscription_ids[0],
    )
    assert subscription.agent_id == foreign_agent.id
    assert not any(todo.kind == "missing_agent" for todo in result.todos)


async def test_legacy_blueprint_agent_slug_rejects_inaccessible_home_agent(
    db_session,
):
    entity_id = generate_ulid()
    foreign_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=generate_ulid(),
        workspace_id=generate_ulid(),
        visibility="private",
        name="Private Workspace-home Agent",
        slug=f"inaccessible-workspace-agent-{entity_id}",
        system_prompt="Do not reuse this Agent for another caller.",
        status="active",
    )
    db_session.add(foreign_agent)
    await db_session.flush()
    payload = _base_payload(**{
        "recipe.subscriptions": [{
            "agent_slug": foreign_agent.slug,
            "service_key": "private_service",
        }],
    })

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=generate_ulid(),
    )

    assert result.subscription_ids == []
    assert any(todo.kind == "missing_agent" for todo in result.todos)


async def test_legacy_blueprint_agent_slug_backfills_exact_source_link(db_session):
    entity_id = generate_ulid()
    shared_slug = f"historical-marketplace-agent-{entity_id}"
    source = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=shared_slug,
        system_prompt="Marketplace behavior.",
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    historical = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=None,
        name="Historical Local Agent",
        slug=shared_slug,
        system_prompt="Historical installed behavior.",
        config={"source_agent_id": source.id},
        source="marketplace",
        status="active",
    )
    db_session.add_all([source, historical])
    await db_session.flush()
    payload = _base_payload(**{
        "recipe.subscriptions": [{
            "agent_slug": shared_slug,
            "service_key": "marketplace_service",
        }],
    })

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    subscription = await db_session.get(
        AgentSubscription,
        result.subscription_ids[0],
    )
    link = (await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_type == "agent",
            MarketplaceResourceLink.marketplace_resource_id == source.id,
            MarketplaceResourceLink.local_resource_id == historical.id,
        )
    )).scalar_one()

    assert subscription is not None
    assert subscription.agent_id == historical.id
    assert link.relationship == "installed_from"


@pytest.mark.parametrize("include_exact_id", [False, True])
async def test_blueprint_agent_identity_conflict_becomes_blocking_todo(
    db_session,
    include_exact_id,
):
    entity_id = generate_ulid()
    source = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=f"blueprint-conflict-source-{entity_id}",
        system_prompt="Marketplace behavior.",
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    historical = [
        Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            name=f"Historical Agent {index}",
            slug=f"blueprint-conflict-install-{index}-{entity_id}",
            system_prompt="Historical behavior.",
            config={"source_agent_id": source.id},
            source="marketplace",
            status="active",
        )
        for index in range(2)
    ]
    db_session.add_all([source, *historical])
    await db_session.flush()
    subscription = {
        "agent_slug": source.slug,
        "service_key": "marketplace_service",
    }
    if include_exact_id:
        subscription["marketplace_agent_id"] = source.id
    payload = _base_payload(**{"recipe.subscriptions": [subscription]})

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )

    conflicts = [
        todo for todo in result.todos
        if todo.payload.get("identity_conflict")
    ]
    assert result.subscription_ids == []
    assert len(conflicts) == 1
    assert conflicts[0].blocking is True
    assert "multiple historical local installs" in conflicts[0].detail


async def test_workspace_finalize_installs_marketplace_mapping_into_my_agents(db_session):
    entity_id = generate_ulid()
    template = Agent(
        id=f"tmpl_{generate_ulid()[5:]}",
        entity_id=None,
        name="Workspace Marketplace Agent",
        slug=f"workspace-marketplace-{entity_id}",
        system_prompt="Run the Workspace service.",
        config={},
        is_template=True,
        is_public=True,
        source="marketplace",
        status="active",
    )
    db_session.add(template)
    await db_session.flush()
    session = WorkspaceSetupSession(
        entity_id=entity_id,
        user_id=generate_ulid(),
        fields={
            "name": "Marketplace Workspace",
            "kind": "operations",
            "operating_context": "Test Marketplace installation.",
            "primary_work": "Run the Marketplace-backed service.",
            "services": [
                {
                    "service_key": "marketplace_service",
                    "name": "Marketplace Service",
                    "description": "Use the subscribed Marketplace Agent.",
                }
            ],
            "agent_mappings": [
                {
                    "service_key": "marketplace_service",
                    "agent_id": template.id,
                    "strategy": "match",
                }
            ],
            "goals": [],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    subscription = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == "marketplace_service",
            )
        )
    ).scalar_one()
    assert subscription.agent_id != template.id

    installed = (
        await db_session.execute(
            select(Agent).where(Agent.id == subscription.agent_id)
        )
    ).scalar_one()
    assert installed.entity_id == entity_id
    assert installed.config["source_agent_id"] == template.id
