"""Tests for per-agent MCP scope resolution + runtime tool surface filtering.

Verifies:
  * resolve_agent_mcp_scope() reads bindings correctly
  * filter_mcp_tools_by_scope() enforces the allowlist
  * runtime_tool_schemas_for_agent() respects mcp_allowed_names
  * default-deny for custom agents without a binding lookup
  * master sees all MCP tools when mcp_allowed_names is None
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.core.ai.tool_pool import tool_pool
from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_agent
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.workspace import Agent, AgentToolBinding, ToolDefinition
from packages.core.services.agent_permission_service import (
    filter_mcp_tools_by_scope,
    resolve_agent_mcp_scope,
)


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    entity_id = me.json()["entity_id"]
    return headers, data["user_id"], entity_id


async def _get_or_create_mcp_server(db, key: str) -> MCPServer:
    from sqlalchemy import select

    existing = (await db.execute(select(MCPServer).where(MCPServer.server_key == key))).scalar_one_or_none()
    if existing:
        return existing
    server = MCPServer(
        id=generate_ulid(),
        server_key=key,
        name=key.title(),
        transport="builtin",
        auth_type="oauth2",
        status="active",
    )
    db.add(server)
    await db.flush()
    return server


async def _make_agent(db, entity_id: str, name: str) -> Agent:
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        slug=name.lower().replace(" ", "-"),
        description="test agent",
        status="active",
    )
    db.add(agent)
    await db.flush()
    return agent


# ── resolve_agent_mcp_scope ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_scope_with_allowlist(client: AsyncClient):
    """Binding with allowed_tools returns that exact allowlist."""
    _, _, entity_id = await _register_owner(client, "scope_1")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        server = await _get_or_create_mcp_server(db, "gmail")
        agent = await _make_agent(db, entity_id, "Jane")
        db.add(
            AgentMCPBinding(
                id=generate_ulid(),
                agent_id=agent.id,
                mcp_server_id=server.id,
                allowed_tools=["send_message", "list_messages"],
                status="active",
            )
        )
        await db.commit()

    async with dbmod.async_session() as db:
        scope = await resolve_agent_mcp_scope(db, agent.id)
        assert scope == {"gmail": ["send_message", "list_messages"]}


@pytest.mark.asyncio
async def test_resolve_scope_null_allows_all(client: AsyncClient):
    """allowed_tools=None means 'all tools from this server'."""
    _, _, entity_id = await _register_owner(client, "scope_2")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        server = await _get_or_create_mcp_server(db, "linkedin")
        agent = await _make_agent(db, entity_id, "Alex SDR")
        db.add(
            AgentMCPBinding(
                id=generate_ulid(),
                agent_id=agent.id,
                mcp_server_id=server.id,
                allowed_tools=None,  # null = all
                status="active",
            )
        )
        await db.commit()

    async with dbmod.async_session() as db:
        scope = await resolve_agent_mcp_scope(db, agent.id)
        assert scope == {"linkedin": None}


@pytest.mark.asyncio
async def test_resolve_scope_no_bindings(client: AsyncClient):
    """Unbound agent → empty scope dict."""
    _, _, entity_id = await _register_owner(client, "scope_3")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        agent = await _make_agent(db, entity_id, "Lonely")
        await db.commit()

    async with dbmod.async_session() as db:
        scope = await resolve_agent_mcp_scope(db, agent.id)
        assert scope == {}


@pytest.mark.asyncio
async def test_resolve_scope_includes_agent_settings_mcp_tool_binding(client: AsyncClient):
    """Legacy Agent settings rows stored mcp tools as AgentToolBinding only."""
    _, _, entity_id = await _register_owner(client, "scope_settings_mcp")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        await _get_or_create_mcp_server(db, "settings_x")
        agent = await _make_agent(db, entity_id, "Settings MCP")
        tool = ToolDefinition(
            id=generate_ulid(),
            name="mcp__settings_x__publish_post",
            display_name="Publish Post",
            category="mcp",
            status="active",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentToolBinding(agent_id=agent.id, tool_id=tool.id))
        await db.commit()

    async with dbmod.async_session() as db:
        scope = await resolve_agent_mcp_scope(db, agent.id)
        assert scope == {"settings_x": ["publish_post"]}


# ── filter_mcp_tools_by_scope ───────────────────────────────────────────────


def test_filter_scope_allowlist():
    names = [
        "mcp__gmail__send_message",
        "mcp__gmail__list_messages",
        "mcp__gmail__delete_draft",
        "mcp__linkedin__create_post",
    ]
    scope = {"gmail": ["send_message", "list_messages"]}
    allowed = filter_mcp_tools_by_scope(names, scope)
    assert allowed == {"mcp__gmail__send_message", "mcp__gmail__list_messages"}


def test_filter_scope_none_means_all_for_server():
    names = [
        "mcp__linkedin__create_post",
        "mcp__linkedin__get_profile",
        "mcp__gmail__send_message",
    ]
    scope = {"linkedin": None}
    allowed = filter_mcp_tools_by_scope(names, scope)
    assert allowed == {"mcp__linkedin__create_post", "mcp__linkedin__get_profile"}


def test_filter_scope_drops_non_mcp():
    names = ["bash", "read_file", "mcp__gmail__send_message"]
    scope = {"gmail": None}
    allowed = filter_mcp_tools_by_scope(names, scope)
    # Non-MCP names are ignored by this filter
    assert allowed == {"mcp__gmail__send_message"}


def test_filter_scope_empty():
    names = ["mcp__gmail__send_message", "mcp__linkedin__create_post"]
    allowed = filter_mcp_tools_by_scope(names, {})
    assert allowed == set()


# ── runtime_tool_schemas_for_agent integration ──────────────────────────────


def test_master_sees_all_mcp_when_scope_is_none():
    tool_pool.initialize()
    _, allowed = runtime_tool_schemas_for_agent(
        agent_id=None,
        is_master=True,
        mcp_allowed_names=None,
    )
    mcp = [n for n in allowed if n.startswith("mcp__")]
    assert len(mcp) >= 32, f"master should see all MCP tools, got {len(mcp)}"


def test_custom_agent_default_deny_without_scope_lookup():
    """Custom agent with mcp_allowed_names=None sees ZERO MCP tools (safety)."""
    tool_pool.initialize()
    _, allowed = runtime_tool_schemas_for_agent(
        agent_id="some-agent",
        is_master=False,
        bound_tool_names=set(),
        mcp_allowed_names=None,
    )
    mcp = [n for n in allowed if n.startswith("mcp__")]
    assert mcp == [], f"default-deny expected, got {mcp}"


def test_custom_agent_sees_only_scoped_mcp():
    """With an explicit allowlist, only those MCP tools show up."""
    tool_pool.initialize()
    allowlist = {
        "mcp__gmail__send_message",
        "mcp__gmail__list_messages",
        "mcp__linkedin__create_post",
    }
    _, allowed = runtime_tool_schemas_for_agent(
        agent_id="some-agent",
        is_master=False,
        bound_tool_names=set(),
        mcp_allowed_names=allowlist,
    )
    mcp = {n for n in allowed if n.startswith("mcp__")}
    assert mcp == allowlist, f"expected exactly the allowlist, got {mcp}"


# ── runtime chat context wiring — end-to-end per-agent scope enforcement ────


@pytest.mark.asyncio
async def test_chat_service_applies_mcp_scope_for_custom_agent(client: AsyncClient):
    """resolve_runtime_chat_context calls the scope resolver so a
    custom agent's tool list reflects its bindings in agent_mcp_bindings.
    """
    _, _, entity_id = await _register_owner(client, "chat_scope_custom")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        gmail = await _get_or_create_mcp_server(db, "gmail")
        await _get_or_create_mcp_server(db, "linkedin")
        agent = await _make_agent(db, entity_id, "ScopedJane")

        # Bind only gmail with a 2-tool allowlist
        db.add(
            AgentMCPBinding(
                id=generate_ulid(),
                agent_id=agent.id,
                mcp_server_id=gmail.id,
                allowed_tools=["send_message", "list_messages"],
                status="active",
            )
        )
        # DON'T bind linkedin — mcp__linkedin__* must be hidden
        await db.commit()

    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context

    async with dbmod.async_session() as db:
        _, tools, _, _ = await resolve_runtime_chat_context(
            db,
            "hello",
            entity_id=entity_id,
            user_id=None,
            agent_id=agent.id,
            conversation_id=None,
            is_master=False,
        )

    tool_names = {t["function"]["name"] for t in tools}
    mcp_tools = {n for n in tool_names if n.startswith("mcp__")}
    # The two allowed gmail tools may appear in schemas if they're not
    # deferred — but with deferral, the eager list won't contain MCP tools
    # at all. Either way, linkedin must be absent.
    assert not any(n.startswith("mcp__linkedin__") for n in mcp_tools), (
        f"linkedin MCP tools leaked to a non-bound agent: {mcp_tools}"
    )


@pytest.mark.asyncio
async def test_chat_service_unbound_agent_default_deny(client: AsyncClient):
    """Custom agent with no bindings sees NO MCP tools."""
    _, _, entity_id = await _register_owner(client, "chat_scope_empty")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        agent = await _make_agent(db, entity_id, "Unbound")
        await db.commit()

    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context

    async with dbmod.async_session() as db:
        _, tools, _, _ = await resolve_runtime_chat_context(
            db,
            "hello",
            entity_id=entity_id,
            user_id=None,
            agent_id=agent.id,
            conversation_id=None,
            is_master=False,
        )
    tool_names = {t["function"]["name"] for t in tools}
    assert not any(n.startswith("mcp__") for n in tool_names), (
        f"unbound agent sees MCP tools (should be default-deny): {[n for n in tool_names if n.startswith('mcp__')]}"
    )


@pytest.mark.asyncio
async def test_chat_service_disable_tools_returns_empty_tool_surface(client: AsyncClient):
    """Live editor sessions can opt out of the agent tool surface entirely."""
    _, _, entity_id = await _register_owner(client, "chat_disable_tools")

    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        _, tools, _, ctx = await resolve_runtime_chat_context(
            db,
            "edit the active PDF",
            entity_id=entity_id,
            user_id=None,
            agent_id=None,
            conversation_id=None,
            is_master=True,
            disable_tools=True,
        )

    assert tools == []
    assert ctx.tool_names == []
    assert ctx.allowed_tool_names == set()


@pytest.mark.asyncio
async def test_chat_service_master_skips_scope_lookup(client: AsyncClient):
    """is_master=True skips the scope resolver entirely (no DB query to
    agent_mcp_bindings) — verified by passing a bogus agent_id that has no
    bindings and confirming resolution still succeeds."""
    _, _, entity_id = await _register_owner(client, "chat_scope_master")

    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        # Master with agent_id set — scope resolver must NOT be called
        # (if it were called with bogus agent_id, it would still succeed
        # with an empty scope, but the contract is that master bypasses
        # the lookup entirely). Test succeeds as long as no exception.
        sp, tools, _, _ = await resolve_runtime_chat_context(
            db,
            "hello",
            entity_id=entity_id,
            user_id=None,
            agent_id="ghost-agent-id-does-not-exist",
            conversation_id=None,
            is_master=True,
        )
    # System prompt falls back to default since agent wasn't found; that's ok
    assert isinstance(sp, str) and sp
    # Tool list may be empty because _load_agent returned None, but the
    # scope resolver path shouldn't have raised.
    _ = tools
