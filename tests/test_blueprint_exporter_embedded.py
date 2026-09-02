"""Unit tests for the v1.1 exporter's embedded.* assembly.

Uses the conftest's ``db_session`` fixture (real PostgreSQL) so we
don't mutate the shared SQLAlchemy MetaData — earlier versions of
this file used in-memory sqlite via _build_engine and had to monkey-
patch JSONB→JSON / ARRAY→JSON, which silently broke SQLAlchemy's
internal comparator caching for downstream tests in the same pytest
process (Document.metadata_['k'].astext lost the JSONB comparator).

Each test uses a unique entity_id so rows from concurrent tests don't
collide without truncating between tests.

Covers:
  * subscribed agents split into embedded (entity-private) vs external
    (is_public=true → contract.requires.agents)
  * AgentToolBinding → tool slug list + contract.requires.tools union
  * AgentMCPBinding config_override KEYS only (values dropped) +
    secret-shaped key names filtered out
  * AgentSkillBinding → embedded.skills (entity-private) vs
    contract.requires.skills (public/external)
  * Agent-level AgentMemory respects include_starter_memory toggle and
    drops confidential/restricted classification
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.exporter import (
    ExportContext,
    ExportError,
    export_workspace,
    list_exportable_knowledge_documents,
)
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.payload import validate_payload
from packages.core.constants.blueprints import BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.services.marketplace_resource_links import (
    RELATIONSHIP_INSTALLED_FROM,
    RESOURCE_AGENT,
    RESOURCE_SKILL,
    SCOPE_ENTITY,
    record_marketplace_resource_link,
)
from packages.core.services.marketplace_skill_service import (
    ensure_marketplace_skill_installed,
)


# ── Fixtures: seed a workspace with embedded + external agents ────────


async def _seed(db: AsyncSession, entity_id: str) -> dict[str, Any]:
    """Seed:
    * workspace
    * agent A: entity-private (is_public=false) → embedded
      - bound to tool 'tool.x.post'
      - bound to MCP 'linear-mcp' with config_override {team_id, api_token}
      - bound to private skill 'reply-tone' (entity-private)
      - bound to public skill 'manor/triage' (external)
      - has 1 starter memory (active) + 1 confidential (drop)
    * agent B: public (is_public=true) → external requirement only
    """
    ws = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="X Growth",
        kind="social_media",
        operating_context="ctx",
        primary_work="work",
        operating_model={},
        settings={},
    )
    db.add(ws)
    await db.flush()

    tool_x_post = ToolDefinition(
        id=generate_ulid(),
        name=f"tool.x.post.{entity_id}",
        display_name="X Post",
    )
    tool_x_like = ToolDefinition(
        id=generate_ulid(),
        name=f"tool.x.like.{entity_id}",
        display_name="X Like",
    )
    db.add_all([tool_x_post, tool_x_like])
    await db.flush()

    mcp = MCPServer(
        id=generate_ulid(),
        server_key=f"linear-mcp-{entity_id}",
        name="Linear",
        description="Task sync",
        transport="http",
        endpoint="https://example.com",
        auth_type="api_key",
        status="active",
    )
    db.add(mcp)
    await db.flush()

    private_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Reply Tone",
        slug=f"reply-tone-{entity_id}",
        display_name="Reply Tone",
        system_prompt="Reply in founder voice.",
        tools=[tool_x_like.name],
        is_public=False,
        version="1.0.0",
    )
    public_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Triage",
        slug=f"manor/triage-{entity_id}",
        display_name="Triage",
        system_prompt="Triage incoming.",
        tools=[],
        is_public=True,
        version="1.0.0",
    )
    db.add_all([private_skill, public_skill])
    await db.flush()

    agent_a = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Calvin Reply",
        slug=f"calvin-reply-{entity_id}",
        system_prompt="Reply like Calvin.",
        config={"model": "claude-opus-4.7", "temperature": 0.5},
        category="social_replies",
        tags=["replies"],
        is_template=False,
        is_public=False,
        status="active",
        version="1.0",
    )
    agent_b = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="X Poster",
        slug=f"x-poster-v2-{entity_id}",
        system_prompt="(public marketplace agent)",
        config={},
        is_public=True,
        status="active",
        version="2.0",
    )
    db.add_all([agent_a, agent_b])
    await db.flush()

    db.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_a.id,
            workspace_id=ws.id,
            service_key="social.x.reply",
            config={},
            status="active",
        )
    )
    db.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_b.id,
            workspace_id=ws.id,
            service_key="social.x.poster",
            config={},
            status="active",
        )
    )

    db.add(AgentToolBinding(agent_id=agent_a.id, tool_id=tool_x_post.id))
    db.add(
        AgentMCPBinding(
            id=generate_ulid(),
            agent_id=agent_a.id,
            mcp_server_id=mcp.id,
            allowed_tools=["linear.create_issue"],
            config_override={"team_id": "t_123", "api_token": "sk_LEAK"},
            status="active",
        )
    )
    db.add(
        AgentSkillBinding(
            id=generate_ulid(),
            agent_id=agent_a.id,
            skill_id=private_skill.id,
            status="active",
        )
    )
    db.add(
        AgentSkillBinding(
            id=generate_ulid(),
            agent_id=agent_a.id,
            skill_id=public_skill.id,
            status="active",
        )
    )

    db.add(
        AgentMemory(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_a.id,
            memory_type="instruction",
            content="Reply within 1h.",
            importance=8,
            confidence=0.9,
            status="active",
            visibility="entity",
            classification="internal",
        )
    )
    db.add(
        AgentMemory(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_a.id,
            memory_type="fact",
            content="Confidential customer data.",
            importance=5,
            confidence=1.0,
            status="active",
            visibility="entity",
            classification="confidential",
        )
    )

    await db.commit()
    return {
        "entity_id": entity_id,
        "workspace": ws,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "private_skill_slug": private_skill.slug,
        "public_skill_slug": public_skill.slug,
        "tool_x_post_name": tool_x_post.name,
        "tool_x_like_name": tool_x_like.name,
        "mcp_slug": mcp.server_key,
    }


@pytest.fixture
def entity_id() -> str:
    """Per-test unique entity_id so DB rows from concurrent tests don't
    interfere with each other without needing truncation. ULID is 26
    chars which matches the entity_id column width."""
    return generate_ulid()


# ── Tests ─────────────────────────────────────────────────────────────


async def test_embedded_vs_external_split(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(
        db_session,
        seeded["workspace"].id,
        title="Test",
        context=ExportContext(include_starter_memory=False),
    )
    embedded_slugs = [a["slug"] for a in payload["embedded"]["agents"]]
    assert embedded_slugs == [f"calvin-reply-{entity_id}"]
    required_agents = [a["slug"] for a in payload["contract"]["requires"]["agents"]]
    assert f"x-poster-v2-{entity_id}" in required_agents
    assert f"calvin-reply-{entity_id}" not in required_agents


async def test_embedded_agent_carries_tool_bindings_and_skills(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [calvin] = payload["embedded"]["agents"]
    assert calvin["tool_bindings"] == [seeded["tool_x_post_name"]]
    assert sorted(calvin["skill_bindings"]) == sorted(
        [
            seeded["public_skill_slug"],
            seeded["private_skill_slug"],
        ]
    )
    declared = payload["contract"]["requires"]["tools"]
    assert seeded["tool_x_post_name"] in declared
    # Skill's tool (x.like) also flows through via the embedded skill.
    assert seeded["tool_x_like_name"] in declared


async def test_mcp_allowlist_drops_secret_shaped_keys(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [calvin] = payload["embedded"]["agents"]
    [mcp_binding] = calvin["mcp_bindings"]
    assert mcp_binding["server_slug"] == seeded["mcp_slug"]
    # api_token is secret-shaped → dropped; team_id is safe → kept.
    assert mcp_binding["config_override_allowlist"] == ["team_id"]
    # No raw values anywhere in the payload.
    assert "sk_LEAK" not in str(payload)


async def test_mcp_empty_allowlist_round_trips_without_becoming_inherited(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    binding = (await db_session.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id == seeded["agent_a"].id
        )
    )).scalar_one()
    binding.allowed_tools = []
    await db_session.flush()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [calvin] = payload["embedded"]["agents"]
    [mcp_binding] = calvin["mcp_bindings"]
    assert mcp_binding["allowed_tools"] == []


async def test_skills_split_into_embedded_vs_required(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    emb_slugs = [s["slug"] for s in payload["embedded"]["skills"]]
    req_slugs = [s["slug"] for s in payload["contract"]["requires"]["skills"]]
    assert emb_slugs == [seeded["private_skill_slug"]]
    assert req_slugs == [seeded["public_skill_slug"]]


async def test_marketplace_skill_binding_exports_exact_source_reference(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    public_skill = (await db_session.execute(
        select(Skill).where(
            Skill.entity_id == entity_id,
            Skill.slug == seeded["public_skill_slug"],
        )
    )).scalar_one()
    source_skill_id = generate_ulid()
    await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_SKILL,
        marketplace_resource_id=source_skill_id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=public_skill.id,
        marketplace_version=public_skill.version,
    )
    await db_session.commit()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [embedded_agent] = payload["embedded"]["agents"]
    assert embedded_agent["skill_binding_refs"] == [
        {
            "slug": public_skill.slug,
            "marketplace_source": "platform",
            "marketplace_id": source_skill_id,
        }
    ]
    assert {
        (skill.get("marketplace_source"), skill.get("marketplace_id"))
        for skill in payload["contract"]["requires"]["skills"]
    } == {("platform", source_skill_id)}


async def test_installed_marketplace_skill_exports_exact_source_identity(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Research",
        slug=f"marketplace-research-{entity_id}",
        system_prompt="Research from approved sources.",
        is_public=True,
        version="2.1.0",
        status="active",
    )
    db_session.add(source)
    await db_session.flush()
    installed = await ensure_marketplace_skill_installed(
        db_session,
        entity_id=entity_id,
        skill_id=source.id,
    )
    db_session.add(AgentSkillBinding(
        id=generate_ulid(),
        agent_id=seeded["agent_a"].id,
        skill_id=installed.id,
        status="active",
    ))
    await db_session.commit()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")

    assert source.slug not in {
        skill["slug"] for skill in payload["embedded"]["skills"]
    }
    requirement = next(
        item for item in payload["contract"]["requires"]["skills"]
        if item.get("marketplace_id") == source.id
    )
    assert requirement == {
        "slug": source.slug,
        "min_version": source.version,
        "marketplace_id": source.id,
        "marketplace_source": "platform",
    }
    embedded_agent = next(
        agent for agent in payload["embedded"]["agents"]
        if agent["slug"] == seeded["agent_a"].slug
    )
    assert {
        (ref["marketplace_source"], ref["marketplace_id"])
        for ref in embedded_agent["skill_binding_refs"]
    } >= {("platform", source.id)}


async def test_keyless_installed_marketplace_agent_exports_exact_source_identity(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    installed_agent = seeded["agent_b"]
    installed_agent.is_public = False
    installed_agent.slug = None
    source_agent_id = generate_ulid()
    await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_AGENT,
        marketplace_resource_id=source_agent_id,
        relationship=RELATIONSHIP_INSTALLED_FROM,
        scope_type=SCOPE_ENTITY,
        scope_id=entity_id,
        local_resource_type=RESOURCE_AGENT,
        local_resource_id=installed_agent.id,
        marketplace_version=installed_agent.version,
    )
    await db_session.commit()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    subscription = next(
        item for item in payload["recipe"]["subscriptions"]
        if item["service_key"] == "social.x.poster"
    )
    assert subscription["marketplace_agent_id"] == source_agent_id
    assert subscription["agent_slug"].startswith("x-poster-")
    requirement = next(
        item for item in payload["contract"]["requires"]["agents"]
        if item.get("marketplace_id") == source_agent_id
    )
    assert requirement["slug"] == subscription["agent_slug"]


async def test_starter_memory_opt_in_off_by_default(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [calvin] = payload["embedded"]["agents"]
    assert calvin["starter_memory"] == []


async def test_starter_memory_opt_in_drops_confidential(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(
        db_session,
        seeded["workspace"].id,
        title="T",
        context=ExportContext(include_starter_memory=True),
    )
    [calvin] = payload["embedded"]["agents"]
    contents = [m["content"] for m in calvin["starter_memory"]]
    assert "Reply within 1h." in contents
    assert all("Confidential" not in c for c in contents)


async def test_requires_tools_is_deduplicated_and_sorted(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    tools = payload["contract"]["requires"]["tools"]
    assert tools == sorted(set(tools))


async def test_payload_validates_against_v11_schema(
    db_session: AsyncSession,
    entity_id: str,
):
    """Round-trip: the exporter's output must satisfy validate_payload's
    rules. Catches any future divergence in section shape."""
    seeded = await _seed(db_session, entity_id)
    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    validate_payload(payload)  # should not raise
    experience = payload["recipe"]["simulation_experience"]
    assert experience["schema_version"] == "1.0"
    assert experience["artifacts"]


async def test_legacy_private_agent_and_skill_get_portable_blueprint_refs(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    agent = seeded["agent_a"]
    private_skill = (await db_session.execute(
        select(Skill).where(
            Skill.entity_id == entity_id,
            Skill.slug == seeded["private_skill_slug"],
        )
    )).scalar_one()
    agent.slug = None
    agent.config = {
        **dict(agent.config or {}),
        "business_capabilities": ["workspace.search"],
    }
    private_skill.slug = None
    await db_session.commit()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [embedded_agent] = payload["embedded"]["agents"]
    [embedded_skill] = payload["embedded"]["skills"]

    assert embedded_agent["slug"].startswith("calvin-reply-")
    assert embedded_skill["slug"].startswith("reply-tone-")
    assert embedded_agent["business_capabilities"] == ["workspace.search"]
    assert embedded_skill["slug"] in embedded_agent["skill_bindings"]
    private_subscription = next(
        subscription
        for subscription in payload["recipe"]["subscriptions"]
        if subscription["service_key"] == "social.x.reply"
    )
    assert private_subscription["agent_slug"] == embedded_agent["slug"]


async def test_export_drops_source_workspace_runtime_ids(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workspace = seeded["workspace"]
    source_user_id = generate_ulid()
    source_group_id = generate_ulid()
    workspace.operating_model = {
        "services": [{"service_key": "social.x.reply"}],
        "agent_mappings": [{
            "service_key": "social.x.reply",
            "agent_id": seeded["agent_a"].id,
        }],
        "knowledge": {
            "auto_search": True,
            "default_group_ids": [source_group_id],
            "group_purposes": {source_group_id: "Source-only group"},
        },
    }
    workspace.settings = {
        "created_by_user_id": source_user_id,
        "provisioning": {"status": "ready"},
        "access_mode": "members_only",
        "publication_setup": {
            "workspace_id": workspace.id,
            "mode": "reviewed",
        },
        "integration": {"value": "example-sensitive-value"},
    }
    await db_session.commit()

    payload = await export_workspace(db_session, workspace.id, title="T")
    operating_model = payload["recipe"]["operating_model"]
    assert "agent_mappings" not in operating_model
    assert operating_model["knowledge"] == {"auto_search": True}
    assert operating_model["settings"] == {
        "access_mode": "members_only",
        "publication_setup": {"mode": "reviewed"},
    }
    assert seeded["agent_a"].id not in str(payload)
    assert source_user_id not in str(payload)
    assert source_group_id not in str(payload)
    assert "example-sensitive-value" not in str(payload)


async def test_export_rejects_secrets_in_declared_portable_workspace_settings(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workspace = seeded["workspace"]
    workspace.settings = {
        "_blueprint": {"portable_setting_keys": ["integration_config"]},
        "integration_config": {
            "authorization": "Bearer sk-live-1234567890abcdef",
            "endpoint": (
                "https://example.invalid/callback?"
                "api_key=sk-live-1234567890abcdef"
            ),
        },
    }
    await db_session.commit()

    with pytest.raises(ExportError, match="contain credentials"):
        await export_workspace(
            db_session,
            workspace.id,
            title="Unsafe portable settings",
        )


async def test_export_rejects_credentials_in_free_form_portable_content(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workspace = seeded["workspace"]
    workspace.operating_context = (
        "Call the provider with Authorization: Bearer sk-live-1234567890abcdef"
    )
    await db_session.commit()

    with pytest.raises(ExportError, match="contain credentials"):
        await export_workspace(
            db_session,
            workspace.id,
            title="Unsafe operating context",
        )


async def test_export_includes_workspace_workflow_binding(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        name="daily-brief",
        description="Create the daily brief.",
        trigger_type="event",
        trigger_config={"event": "workspace.updated"},
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "name": "Start",
                "config": {"run_inputs": [{"key": "topic", "type": "string"}]},
                "next": ["draft"],
            },
            {
                "id": "draft",
                "type": "agent",
                "name": "Draft",
                "config": {
                    "service_key": "social.x.reply",
                    "agent_id": seeded["agent_a"].id,
                    "credential_ref": "vault:must-not-export",
                },
                "next": [],
            },
        ],
        variables={"tone": "concise"},
        category="content",
        tags=["daily"],
        is_active=True,
        status="active",
    )
    db_session.add(workflow)
    await db_session.flush()
    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=entity_id,
        workflow_id=workflow.id,
        workspace_id=seeded["workspace"].id,
        name="Daily Brief",
        trigger_type="event",
        trigger_config={
            "event": "task.completed",
            "filters": {"minimum_score": 0.8},
            "webhook_token": "must-not-export",
        },
        variables={"audience": "founders"},
        config={
            "source": "blueprint",
            "source_template_id": "source-row-id",
            "workspace_blueprint_workflow_slug": "daily-brief-v1",
            "chat_entrypoint": {"enabled": True},
        },
        enabled=False,
        status="inactive",
    )
    scheduled_job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"daily-brief-{entity_id}",
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
        name="Scheduled daily brief",
        execution_type="workflow",
        execution_target={
            "workflow_id": workflow.id,
            "binding_id": binding.id,
            "workspace_id": seeded["workspace"].id,
        },
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        enabled=False,
    )
    db_session.add_all([binding, scheduled_job])
    await db_session.commit()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")

    [exported] = payload["recipe"]["workflows"]
    assert exported["slug"] == "daily-brief-v1"
    assert exported["name"] == "Daily Brief"
    assert exported["binding_config"] == {"chat_entrypoint": {"enabled": True}}
    assert exported["trigger_config"] == {
        "event": "task.completed",
        "filters": {"minimum_score": 0.8},
    }
    assert exported["enabled"] is False
    assert exported["status"] == "inactive"
    assert exported["definition_enabled"] is True
    assert exported["definition_status"] == "active"
    assert exported["run_inputs"] == [{"key": "topic", "type": "string"}]
    assert exported["variables"] == [
        {"key": "audience", "default": "founders"},
        {"key": "tone", "default": "concise"},
    ]
    assert "agent_id" not in str(exported)
    assert "credential_ref" not in str(exported)
    [exported_job] = payload["recipe"]["scheduled_jobs"]
    assert exported_job["execution_target"] == {
        "workflow_slug": "daily-brief-v1",
    }
    assert exported_job["enabled"] is False


async def test_knowledge_pack_modes_use_canonical_membership_and_real_text(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
        name="Launch Notes",
        settings={"purpose": "Reusable launch context"},
    )
    public_doc = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="voice.md",
        mime_type="text/markdown",
        metadata_={"content_text": "# Voice\n\nBe direct."},
        classification="public",
        visibility="workspace",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    private_doc = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="customer-notes.md",
        mime_type="text/markdown",
        metadata_={"content_text": "Never export me."},
        classification="public",
        visibility="private",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    db_session.add_all([group, public_doc, private_doc])
    await db_session.flush()
    db_session.add_all([
        DocumentGroupMember(document_id=public_doc.id, group_id=group.id),
        DocumentGroupMember(document_id=private_doc.id, group_id=group.id),
    ])
    await db_session.commit()

    skeleton = await export_workspace(db_session, seeded["workspace"].id, title="T")
    [skeleton_pack] = skeleton["embedded"]["knowledge_packs"]
    assert skeleton_pack["mode"] == "skeleton"
    assert skeleton_pack["folder_structure"] == [
        {"path": "voice.md", "description": None},
    ]
    assert skeleton_pack["starter_documents"] == []

    inline = await export_workspace(
        db_session,
        seeded["workspace"].id,
        title="T",
        context=ExportContext(include_memory_files=True),
    )
    [inline_pack] = inline["embedded"]["knowledge_packs"]
    assert inline_pack["mode"] == "inline_text"
    [starter] = inline_pack["starter_documents"]
    assert starter["path"] == "voice.md"
    assert starter["body_md"] == "# Voice\n\nBe direct."
    assert starter["key"].startswith("voice-md-")
    assert public_doc.id not in starter["key"]
    assert "Never export me" not in str(inline_pack)


async def test_knowledge_pack_inline_text_can_select_exact_safe_documents(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
        name="Blueprint Starters",
        settings={"purpose": "Selected portable starter files"},
    )
    selected = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="selected.md",
        mime_type="text/markdown",
        metadata_={"content_text": "# Selected"},
        classification="public",
        visibility="workspace",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    omitted = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="omitted.md",
        mime_type="text/markdown",
        metadata_={"content_text": "# Omitted"},
        classification="public",
        visibility="workspace",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    ineligible = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="private.md",
        mime_type="text/markdown",
        metadata_={"content_text": "Never portable"},
        classification="confidential",
        visibility="private",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    db_session.add_all([group, selected, omitted, ineligible])
    await db_session.flush()
    db_session.add_all([
        DocumentGroupMember(document_id=document.id, group_id=group.id)
        for document in (selected, omitted, ineligible)
    ])
    await db_session.commit()

    candidates = await list_exportable_knowledge_documents(
        db_session,
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
    )
    assert [candidate["id"] for candidate in candidates] == [
        omitted.id,
        selected.id,
    ]
    assert all("body_md" not in candidate for candidate in candidates)

    payload = await export_workspace(
        db_session,
        seeded["workspace"].id,
        title="Selected starters",
        context=ExportContext(
            knowledge_pack_mode="inline_text",
            knowledge_document_ids=frozenset({selected.id}),
        ),
    )
    [pack] = payload["embedded"]["knowledge_packs"]
    [starter] = pack["starter_documents"]
    assert starter["path"] == "selected.md"
    assert starter["body_md"] == "# Selected"
    assert starter["key"].startswith("selected-md-")
    assert selected.id not in str(payload)
    assert omitted.id not in str(payload)

    with pytest.raises(ExportError, match="unavailable or ineligible"):
        await export_workspace(
            db_session,
            seeded["workspace"].id,
            title="Unsafe starter",
            context=ExportContext(
                knowledge_pack_mode="inline_text",
                knowledge_document_ids=frozenset({ineligible.id}),
            ),
        )

    skeleton = await export_workspace(
        db_session,
        seeded["workspace"].id,
        title="No starter bodies",
        context=ExportContext(
            knowledge_pack_mode="inline_text",
            knowledge_document_ids=frozenset(),
        ),
    )
    [skeleton_pack] = skeleton["embedded"]["knowledge_packs"]
    assert skeleton_pack["mode"] == "skeleton"
    assert skeleton_pack["starter_documents"] == []


async def test_export_preserves_shared_knowledge_document_identity(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    groups = [
        DocumentGroup(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=seeded["workspace"].id,
            name=name,
            settings={},
        )
        for name in ("Operations", "Support")
    ]
    shared = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="shared.md",
        file_size=len("# Shared"),
        mime_type="text/markdown",
        metadata_={"content_text": "# Shared"},
        classification="public",
        visibility="workspace",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    db_session.add_all([*groups, shared])
    await db_session.flush()
    db_session.add_all([
        DocumentGroupMember(document_id=shared.id, group_id=group.id)
        for group in groups
    ])
    await db_session.commit()

    payload = await export_workspace(
        db_session,
        seeded["workspace"].id,
        title="Shared Knowledge",
        context=ExportContext(
            knowledge_pack_mode="inline_text",
            knowledge_document_ids=frozenset({shared.id}),
        ),
    )
    starters = [
        pack["starter_documents"][0]
        for pack in payload["embedded"]["knowledge_packs"]
        if pack["starter_documents"]
    ]
    assert len(starters) == 2
    assert starters[0]["key"] == starters[1]["key"]
    assert starters[0]["body_md"] == starters[1]["body_md"] == "# Shared"
    assert shared.id not in str(payload)


async def test_export_rejects_oversized_knowledge_before_payload_assembly(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
        name="Oversized",
        settings={},
    )
    document = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="large.md",
        file_size=1,
        mime_type="text/markdown",
        metadata_={
            "content_text": "x" * (BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES + 1),
        },
        classification="public",
        visibility="workspace",
        pii_detected=False,
        quarantine_status="clean",
        is_trashed=False,
    )
    db_session.add_all([group, document])
    await db_session.flush()
    db_session.add(DocumentGroupMember(document_id=document.id, group_id=group.id))
    await db_session.commit()

    candidates = await list_exportable_knowledge_documents(
        db_session,
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
    )
    assert all(candidate["id"] != document.id for candidate in candidates)

    with pytest.raises(ExportError, match="byte Blueprint limit"):
        await export_workspace(
            db_session,
            seeded["workspace"].id,
            title="Too large",
            context=ExportContext(
                knowledge_pack_mode="inline_text",
                knowledge_document_ids=frozenset({document.id}),
            ),
        )


async def test_export_uses_live_workspace_framing_and_omits_derived_jobs(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workspace = seeded["workspace"]
    workspace.operating_context = "Current context"
    workspace.primary_work = "Current work"
    workspace.operating_model = {
        "context": "Stale context",
        "primary_work": "Stale work",
        "settings": {"timezone": "UTC"},
    }
    workspace.settings = {
        "timezone": "America/Los_Angeles",
        "_blueprint": {"runtime": "not portable"},
    }
    db_session.add_all([
        ScheduledJob(
            job_id=f"sr:{workspace.id}",
            entity_id=entity_id,
            workspace_id=workspace.id,
            execution_type="strategist_review",
            enabled=True,
        ),
        ScheduledJob(
            job_id=f"gm:{generate_ulid()}",
            entity_id=entity_id,
            workspace_id=workspace.id,
            execution_type="goal_measurement",
            enabled=True,
        ),
        ScheduledJob(
            job_id=f"weekly-review-{entity_id}",
            entity_id=entity_id,
            workspace_id=workspace.id,
            execution_type="strategist_review",
            schedule_kind="cron",
            cron_expr="0 9 * * 1",
            enabled=True,
        ),
    ])
    await db_session.commit()

    payload = await export_workspace(db_session, workspace.id, title="T")
    operating_model = payload["recipe"]["operating_model"]
    assert operating_model["context"] == "Current context"
    assert operating_model["primary_work"] == "Current work"
    assert operating_model["settings"] == {"timezone": "America/Los_Angeles"}
    assert [job["job_id"] for job in payload["recipe"]["scheduled_jobs"]] == [
        f"weekly-review-{entity_id}",
    ]


async def test_session_requirements_are_limited_to_workspace_references(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workspace = seeded["workspace"]
    workspace.settings = {
        "session_requirements": [
            {"provider": "x", "label": "main", "required": True},
        ],
    }
    db_session.add_all([
        IntegrationSession(
            id=generate_ulid(),
            entity_id=entity_id,
            provider="x",
            label="main",
            status="active",
            health_check={"url": "https://x.com/home"},
            metadata_json={"purpose": "Workspace publishing"},
        ),
        IntegrationSession(
            id=generate_ulid(),
            entity_id=entity_id,
            provider="x",
            label="other-workspace",
            status="active",
            health_check={},
            metadata_json={},
        ),
        IntegrationSession(
            id=generate_ulid(),
            entity_id=entity_id,
            provider="linkedin",
            label="main",
            status="active",
            health_check={},
            metadata_json={},
        ),
    ])
    await db_session.commit()

    payload = await export_workspace(db_session, workspace.id, title="T")

    assert [
        (item["provider"], item["label"])
        for item in payload["contract"]["sessions"]
    ] == [("x", "main")]


async def test_default_export_excludes_runtime_tasks_and_entity_task_policy(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    db_session.add(Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=seeded["workspace"].id,
        title="Proposal generated task", status="proposed", details={},
    ))
    await db_session.flush()

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")
    assert payload["recipe"]["task_categories"] == []
    assert payload["recipe"]["sla_policies"] == []
    assert payload["recipe"]["escalation_rules"] == []


async def test_export_emits_goal_definitions_only_in_recipe_goals(
    db_session: AsyncSession,
    entity_id: str,
):
    from packages.core.goals.service import create_goal

    seeded = await _seed(db_session, entity_id)
    await create_goal(
        db_session,
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
        title="Paid signups",
        goal_key="paid_signups",
        metric_key="signup_count",
        target_value=25,
        install_schedule=False,
    )

    payload = await export_workspace(db_session, seeded["workspace"].id, title="T")

    assert "goals" not in payload["recipe"]["operating_model"]
    assert [goal["goal_key"] for goal in payload["recipe"]["goals"]] == [
        "paid_signups"
    ]


async def test_paused_scheduled_skill_round_trips_by_portable_component_key(
    db_session: AsyncSession,
    entity_id: str,
):
    seeded = await _seed(db_session, entity_id)
    workspace = seeded["workspace"]
    workspace.status = "paused"
    source_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        visibility="workspace",
        name="Weekly Pipeline Bundle",
        slug=f"weekly-pipeline-{entity_id}",
        system_prompt="Run the packaged pipeline.",
        tools=["sandbox_exec"],
        config={
            "type": "sandbox",
            "scripts": {"run.py": "print('ok')"},
            "requirements": "httpx==0.28.1",
        },
        version="2.3.4",
        status="active",
    )
    source_job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"weekly-pipeline-{entity_id}",
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Weekly pipeline",
        execution_type="skill",
        execution_target={"skill_id": source_skill.id, "complexity": "worker"},
        schedule_kind="cron",
        cron_expr="0 9 * * 1",
        payload_message="Prepare this week's pipeline report.",
        enabled=False,
        delete_after_run=True,
    )
    db_session.add_all([source_skill, source_job])
    await db_session.commit()

    payload = await export_workspace(db_session, workspace.id, title="Portable")

    exported_job = next(
        job for job in payload["recipe"]["scheduled_jobs"]
        if job["job_id"] == source_job.job_id
    )
    assert "skill_id" not in exported_job["execution_target"]
    assert exported_job["execution_target"]["skill_component_key"] == source_skill.slug
    assert exported_job["enabled"] is False
    assert exported_job["delete_after_run"] is True
    assert [
        skill["slug"] for skill in payload["embedded"]["skills"]
        if skill["slug"] == source_skill.slug
    ] == [source_skill.slug]

    installed = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
        blueprint_id=generate_ulid(),
        user_id=generate_ulid(),
    )
    installed_job = (await db_session.execute(select(ScheduledJob).where(
        ScheduledJob.workspace_id == installed.workspace_id,
        ScheduledJob.execution_type == "skill",
    ))).scalar_one()
    installed_skill_id = installed_job.execution_target["skill_id"]
    assert installed_skill_id != source_skill.id
    installed_skill = await db_session.get(Skill, installed_skill_id)
    assert installed_skill is not None
    assert installed_skill.workspace_id == installed.workspace_id
    assert installed_skill.version == "2.3.4"
    assert installed_skill.config["scripts"] == {"run.py": "print('ok')"}
    assert installed_job.enabled is False
    assert installed_job.delete_after_run is True


@pytest.mark.parametrize(
    "execution_target",
    [
        pytest.param({}, id="generation-pending"),
        pytest.param({"skill_id": generate_ulid()}, id="missing-skill"),
    ],
)
async def test_export_rejects_scheduled_skill_without_portable_target(
    db_session: AsyncSession,
    entity_id: str,
    execution_target: dict[str, str],
):
    seeded = await _seed(db_session, entity_id)
    source_job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"pending-skill-{entity_id}",
        entity_id=entity_id,
        workspace_id=seeded["workspace"].id,
        name="Pending generated Skill",
        execution_type="skill",
        execution_target=execution_target,
        schedule_kind="cron",
        cron_expr="0 9 * * 1",
        payload_message="Generate and run the weekly report.",
        enabled=False,
    )
    db_session.add(source_job)
    await db_session.commit()

    with pytest.raises(
        ExportError,
        match="scheduled Skill.*portable target",
    ):
        await export_workspace(
            db_session,
            seeded["workspace"].id,
            title="Invalid pending Skill",
        )
