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
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.payload import validate_payload
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
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
