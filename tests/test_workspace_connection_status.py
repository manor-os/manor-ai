"""Current connection guidance is independent of Workspace creation origin."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import OAuthAccount
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition, Workspace
from packages.core.services.workspace_connection_status import WorkspaceConnectionStatusFactory


async def _workspace(db, *, settings=None):
    workspace = Workspace(entity_id=generate_ulid(), name="Connection checks", settings=settings or {})
    db.add(workspace)
    await db.flush()
    return workspace


async def _agent(db, workspace, *, service="email"):
    agent = Agent(entity_id=workspace.entity_id, name="Email agent")
    db.add(agent)
    await db.flush()
    db.add(AgentSubscription(
        entity_id=workspace.entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key=service,
    ))
    await db.flush()
    return agent


async def _server(db, *, auth_type="oauth2"):
    server = MCPServer(
        server_key=f"notice_{generate_ulid().lower()}", name="Email connection",
        transport="builtin", auth_type=auth_type, status="active",
    )
    db.add(server)
    await db.flush()
    return server


async def _status(db, workspace, user_id):
    return await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user_id)


@pytest.mark.parametrize("installed", [False, True])
async def test_connection_recovery_and_expiry_without_creation_flags(db_session, installed):
    db = db_session
    user_id = generate_ulid()
    server = await _server(db)
    settings = {"_blueprint": {"live_setup_requirements": [{
        "kind": "missing_integration", "payload": {"provider": server.server_key}, "blocking": True,
    }]}} if installed else {}
    workspace = await _workspace(db, settings=settings)
    agent = await _agent(db, workspace)
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db.flush()
    original = deepcopy(workspace.settings)
    missing = await _status(db, workspace, user_id)
    assert missing["required_issue_count"] == 1
    assert missing["requirements"][0]["service_keys"] == ["email"]

    account = OAuthAccount(
        user_id=user_id, provider=server.server_key, provider_user_id="email@example.test",
        access_token="fixture-token-not-used-for-provider-calls",
    )
    db.add(account)
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 0
    account.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1
    account.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 0
    assert workspace.settings == original


async def test_other_users_accounts_and_other_workspaces_do_not_satisfy_requirements(db_session):
    db = db_session
    server = await _server(db)
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    other_user = generate_ulid()
    db.add(OAuthAccount(user_id=other_user, provider=server.server_key, provider_user_id="other", access_token="fixture"))
    other_workspace = await _workspace(db)
    other_agent = await _agent(db, other_workspace)
    unrelated = await _server(db)
    db.add(AgentMCPBinding(agent_id=other_agent.id, mcp_server_id=unrelated.id))
    await db.flush()
    result = await _status(db, workspace, generate_ulid())
    assert result["required_issue_count"] == 1
    assert [item["provider"] for item in result["requirements"]] == [server.server_key]
    assert (await _status(db, workspace, other_user))["required_issue_count"] == 0


async def test_optional_blueprint_connection_stays_optional_when_bound(db_session):
    db = db_session
    server = await _server(db)
    workspace = await _workspace(db, settings={"_blueprint": {"live_setup_requirements": [{
        "kind": "missing_integration", "payload": {"provider": server.server_key}, "blocking": False,
    }]}})
    agent = await _agent(db, workspace)
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db.flush()
    result = await _status(db, workspace, generate_ulid())
    assert result["required_issue_count"] == 0
    assert result["requirements"][0]["ready"] is False
    assert result["requirements"][0]["required"] is False


async def test_two_hundred_tool_and_skill_dependencies_are_deduplicated(db_session):
    db = db_session
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    server = await _server(db)
    names = [f"mcp__{server.server_key}__action_{index}" for index in range(200)]
    for name in names:
        tool = ToolDefinition(id=generate_ulid(), name=name)
        db.add_all([tool, AgentToolBinding(agent_id=agent.id, tool_id=tool.id)])
    skill = Skill(id=generate_ulid(), entity_id=workspace.entity_id, name="Email skill", system_prompt="fixture", tools=names)
    db.add_all([skill, AgentSkillBinding(agent_id=agent.id, skill_id=skill.id)])
    await db.flush()
    result = await _status(db, workspace, generate_ulid())
    assert result["required_issue_count"] == 1
    assert len(result["requirements"]) == 1


async def test_blueprint_mcp_configuration_failure_uses_runtime_gate(db_session):
    db = db_session
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    server = await _server(db, auth_type="none")
    workspace.settings = {"_blueprint": {"live_setup_requirements": [{
        "kind": "mcp_configuration", "blocking": True,
        "payload": {"server_slug": server.server_key, "installed_agent_id": agent.id},
    }]}}
    result = await _status(db, workspace, generate_ulid())
    assert result["required_issue_count"] == 1
    assert result["requirements"][-1]["setup_kind"] == "mcp_binding"
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db.flush()
    assert (await _status(db, workspace, generate_ulid()))["required_issue_count"] == 0


async def test_status_endpoint_requires_workspace_read_access(monkeypatch):
    from fastapi import HTTPException
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from apps.api.routers import workspaces

    denied = AsyncMock(side_effect=HTTPException(404, "Workspace not found"))
    factory = AsyncMock()
    monkeypatch.setattr(workspaces, "_require_workspace_read", denied)
    monkeypatch.setattr(WorkspaceConnectionStatusFactory, "create", factory)
    with pytest.raises(HTTPException):
        await workspaces.get_workspace_connection_status("other", SimpleNamespace(id="user"), object())
    factory.assert_not_awaited()


async def test_disabled_mcp_is_not_ready_even_with_an_account(db_session):
    db = db_session
    server = await _server(db)
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    user_id = generate_ulid()
    db.add_all([
        AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id),
        OAuthAccount(user_id=user_id, provider=server.server_key, provider_user_id="mine", access_token="fixture"),
    ])
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 0
    server.status = "inactive"
    await db.flush()
    result = await _status(db, workspace, user_id)
    assert result["required_issue_count"] == 1
    assert "disabled" in result["requirements"][0]["reason"]


async def test_exact_catalog_spelling_and_declarative_setup_check(db_session):
    db = db_session
    server = await _server(db, auth_type="none")
    server.server_key = server.server_key.replace("_", "-")
    await db.flush()
    workspace = await _workspace(db, settings={"blocking_setup": {"checks": [{
        "kind": "integration_provider", "provider": server.server_key.replace("-", "_"),
    }]}})
    assert (await _status(db, workspace, generate_ulid()))["required_issue_count"] == 0


@pytest.mark.parametrize("installed", [False, True])
async def test_channel_connection_regression_is_visible_in_both_paths(db_session, installed):
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    db = db_session
    declaration = {"channel_type": "email", "role": "primary_external", "required": True}
    settings = {"_blueprint": {"live_setup_requirements": [{
        "kind": "channel", "payload": declaration, "blocking": True,
    }]}} if installed else {}
    workspace = await _workspace(db, settings=settings)
    if not installed:
        workspace.operating_model = {"channel_config": {"primary_external_channel": declaration}}
    user_id = generate_ulid()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1
    account = ChannelConfig(entity_id=workspace.entity_id, owner_user_id=user_id, channel_type="email", provider="email", name="My email", status="active")
    db.add(account)
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1
    db.add(Channel(entity_id=workspace.entity_id, workspace_id=workspace.id, type="email", status="active", config={
        "channel_config_id": account.id, "role": "primary_external",
    }))
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 0
    account.status = "inactive"
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1


async def test_browser_session_regression_is_visible(db_session):
    from packages.core.models.integration_session import IntegrationSession

    db = db_session
    workspace = await _workspace(db, settings={"_blueprint": {"live_setup_requirements": [{
        "kind": "browser_session", "payload": {"provider": "notice_browser", "label": "sales"}, "blocking": True,
    }]}})
    user_id = generate_ulid()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1
    session = IntegrationSession(entity_id=workspace.entity_id, provider="notice_browser", label="sales", status="active")
    db.add(session)
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 0
    session.status = "expired"
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1


async def test_saved_health_failure_is_reported_without_provider_calls(db_session):
    db = db_session
    server = await _server(db)
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    user_id = generate_ulid()
    account = OAuthAccount(
        user_id=user_id, provider=server.server_key, provider_user_id="mine", access_token="fixture",
        profile={"last_health_check": {"ok": False, "detail": "private upstream response"}},
    )
    db.add_all([account, AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id)])
    await db.flush()
    result = await _status(db, workspace, user_id)
    assert result["required_issue_count"] == 1
    assert "latest connection health check failed" in result["requirements"][0]["reason"]
    assert "private upstream response" not in str(result)
    account.profile = {"last_health_check": {"ok": True}}
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 0


async def test_connection_status_http_projection_and_tenant_boundary(client, db_session):
    from test_workspaces import _register

    _, headers = await _register(client, "noticeowner")
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Notice HTTP"})
    assert created.status_code == 201
    workspace = await db_session.get(Workspace, created.json()["id"])
    agent = await _agent(db_session, workspace)
    server = await _server(db_session)
    db_session.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db_session.commit()
    response = await client.get(f"/api/v1/workspaces/{workspace.id}/connection-status", headers=headers)
    assert response.status_code == 200
    assert response.json()["required_issue_count"] == 1
    assert response.json()["requirements"][0]["kind"] == "integration"
    assert response.json()["requirements"][0]["service_keys"] == ["email"]
    _, other_headers = await _register(client, "noticeoutsider")
    denied = await client.get(f"/api/v1/workspaces/{workspace.id}/connection-status", headers=other_headers)
    assert denied.status_code == 404
