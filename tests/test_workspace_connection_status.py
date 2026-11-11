"""Current connection guidance is independent of Workspace creation origin."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

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


async def test_removed_workspace_integration_overrides_flags_and_shared_agent_bindings(db_session):
    db = db_session
    server = await _server(db)
    workspace = await _workspace(db, settings={"flagged_integrations": [{
        "provider": server.server_key, "required": True, "source": "agent_design",
        "linked_service_keys": ["tutorial_production"],
    }]})
    sibling = Workspace(entity_id=workspace.entity_id, name="Shared agent sibling", settings={})
    db.add(sibling)
    await db.flush()
    agent = await _agent(db, workspace, service="tutorial_production")
    db.add_all([
        AgentSubscription(entity_id=workspace.entity_id, workspace_id=sibling.id, agent_id=agent.id, service_key="other"),
        AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id),
    ])
    await db.flush()
    user_id = generate_ulid()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1
    original = deepcopy(workspace.settings)
    workspace.operating_model = {"integration_scope": {"disabled_providers": [server.server_key]}}
    await db.flush()
    assert (await _status(db, workspace, user_id))["requirements"] == []
    assert (await _status(db, sibling, user_id))["required_issue_count"] == 1
    assert workspace.settings == original
    workspace.operating_model = {"integration_scope": {"disabled_providers": []}}
    await db.flush()
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1


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


@pytest.mark.parametrize("installed", [False, True])
async def test_pending_connection_is_asked_once_in_workspace_chat(db_session, installed):
    """Created and installed Workspaces ask to connect a missing integration
    with the Reconnect card, connecting recommended, and never ask twice."""
    from packages.core.models.task import Message
    from packages.core.services.workspace_connection_status import ask_to_connect_pending_integrations

    db = db_session
    user_id = generate_ulid()
    server = await _server(db)
    settings = {"_blueprint": {"live_setup_requirements": [{
        "kind": "missing_integration", "payload": {"provider": server.server_key}, "blocking": True,
    }]}} if installed else {"flagged_integrations": [{
        "provider": server.server_key, "required": True, "source": "agent_design",
        "linked_service_keys": ["email"], "blocks_creation": False,
    }]}
    workspace = await _workspace(db, settings=settings)
    # Operation-written services key themselves with ``key``.
    workspace.operating_model = {"services": [{"key": "email", "name": "客户咨询回复"}]}
    agent = await _agent(db, workspace)
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db.flush()

    asked = await ask_to_connect_pending_integrations(
        db, workspace=workspace, user_id=user_id, publish_event=False,
    )
    assert asked == [server.server_key]
    assert await ask_to_connect_pending_integrations(
        db, workspace=workspace, user_id=user_id, publish_event=False,
    ) == []
    await db.flush()
    cards = [
        row.pending_action for row in (await db.execute(select(Message))).scalars().all()
        if (row.pending_action or {}).get("provider_key") == server.server_key
    ]
    assert len(cards) == 1
    card = cards[0]
    assert card["kind"] == "needs_login"
    assert card["options"][0] == "sign_in"
    assert card["recommendation"]["option"] == "sign_in"
    assert card["login_url"] == f"/integrations?provider={server.server_key}"
    assert card["option_details"]["sign_in"]["label_code"] == "integration.connect"
    # Viewer-language title and description name the waiting service, never its key.
    assert card["title_code"] == "integration.connect"
    assert card["description_code"] == "connect_waiting"
    assert card["description_params"]["services"] == "客户咨询回复"
    assert "email" not in card["description_params"]["services"]

    # The App lists this requirement once: its connections row, not the queue too.
    from types import SimpleNamespace
    from packages.core.services.workspace_app_sections import _queue_items
    from packages.core.workspace_chat.attention import list_workspace_attention

    viewer = SimpleNamespace(id=user_id, entity_id=workspace.entity_id, preferences={})
    attention = await list_workspace_attention(db, user=viewer, workspace_id=workspace.id, unread_only=False)
    assert [item["connection_provider"] for item in attention["items"]] == [server.server_key]
    assert await _queue_items(db, workspace, user=viewer) == []


async def test_continuing_after_a_connect_card_hands_the_waiting_work_back_to_the_ai(db_session, monkeypatch):
    """"Continue the waiting work" becomes the person's next message to the AI,
    but only once the account really is usable."""
    from types import SimpleNamespace

    from apps.api.routers import workspace_chat
    from packages.core.models.task import Message

    db = db_session
    user_id = generate_ulid()
    server = await _server(db)
    workspace = await _workspace(db)
    user = SimpleNamespace(id=user_id, entity_id=workspace.entity_id, locale="zh")
    card = {"kind": "needs_login", "connection_request": True, "provider_key": server.server_key,
            "title_params": {"provider": "Gmail"}}
    scheduled: list[dict] = []
    monkeypatch.setattr(workspace_chat, "_schedule_workspace_chat_processing", AsyncMock(side_effect=lambda **kwargs: scheduled.append(kwargs)))

    async def follow_ups():
        return [row for row in (await db.execute(select(Message))).scalars().all()
                if row.author_kind == "user" and "Gmail" in (row.content or "")]

    await workspace_chat._continue_after_connection(db, user=user, workspace_id=workspace.id, pending_action=card)
    assert scheduled == [] and await follow_ups() == []

    db.add(OAuthAccount(user_id=user_id, provider=server.server_key, provider_user_id="owner@example.test",
                        access_token="fixture-token-not-used-for-provider-calls"))
    await db.flush()
    await workspace_chat._continue_after_connection(db, user=user, workspace_id=workspace.id, pending_action=card)
    [message] = await follow_ups()
    assert message.content == "Gmail 已连接，请继续之前在等它的工作。"
    assert (message.meta or {}).get("workspace_chat_audience") == "ai"
    assert [call["message_id"] for call in scheduled] == [message.id]


async def test_failed_workflow_step_names_the_integration_its_agent_waited_for(db_session):
    """A run's retry can offer "connect X, then continue" only for the step whose
    Agent needs X, and only while X is not connected."""
    from types import SimpleNamespace

    from packages.core.services.workspace_connection_status import workflow_step_connection_need

    db = db_session
    user_id = generate_ulid()
    server = await _server(db)
    workspace = await _workspace(db)
    agent = await _agent(db, workspace, service="publishing")
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db.flush()
    from packages.core.services.workflow_run_trace import build_execution_snapshot

    workflow = SimpleNamespace(id="wf-1", version=1, variables={}, steps=[
        {"id": "draft", "type": "agent", "config": {"service_key": "writing"}},
        {"id": "publish", "type": "agent", "config": {"service_key": "publishing"}},
        {"id": "notify", "type": "agent", "config": {"agent_id": agent.id}},
    ])
    # The step config lives in the run's frozen execution contract, not in
    # its display snapshot.
    run = SimpleNamespace(
        workspace_id=workspace.id, workflow_id="wf-1",
        definition_snapshot={"nodes": [{"id": "publish", "type": "agent"}]},
        execution_snapshot=build_execution_snapshot(workflow),
    )

    assert await workflow_step_connection_need(db, run=run, step_id="draft", user_id=user_id) is None
    need = await workflow_step_connection_need(db, run=run, step_id="publish", user_id=user_id)
    assert need and need["provider"] == server.server_key
    assert (await workflow_step_connection_need(db, run=run, step_id="notify", user_id=user_id))["provider"] == server.server_key
    # Staging 2026-11-05: a step that said what else was wrong still waited for this connection.
    for said, waits in (("The file was not found; please upload it.", False),
                        ("Reading mail is outside this agent's scope.", False),
                        ("I have no access to the account.", True),
                        (f"Connect {server.server_key} first.", True)):
        need = await workflow_step_connection_need(db, run=run, step_id="publish", user_id=user_id, evidence=said)
        assert bool(need) is waits, said
    # Without evidence from the caller, the result the run stored for the step speaks;
    # a step that stopped for a person needs a connection only when its ask names one.
    for stored, waits in (({"status": "failed", "error": "The file was not found."}, False),
                          ({"status": "failed", "error": "I have no access to the account."}, True),
                          ({"status": "completed", "needs_person": {
                              "missing": "I have no access to real product photos.",
                              "ask": "Add product photos to the knowledge."}}, False),
                          ({"status": "completed", "needs_person": {
                              "missing": "Nothing can be published yet.",
                              "ask": f"Connect {server.server_key}."}}, True)):
        run.step_results = {"publish": stored}
        need = await workflow_step_connection_need(db, run=run, step_id="publish", user_id=user_id)
        assert bool(need) is waits, stored
    run.step_results = {}

    db.add(OAuthAccount(user_id=user_id, provider=server.server_key, provider_user_id="owner@example.test",
                        access_token="fixture-token-not-used-for-provider-calls"))
    await db.flush()
    assert await workflow_step_connection_need(db, run=run, step_id="publish", user_id=user_id) is None


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


async def test_internal_mcp_binding_is_not_connection_guidance(db_session):
    db = db_session
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    server = (await db.execute(
        select(MCPServer).where(MCPServer.server_key == "chrome_knowledge_local")
    )).scalar_one_or_none()
    if server is None:
        server = MCPServer(
            server_key="chrome_knowledge_local",
            name="Chrome Knowledge Upload Prep",
            transport="builtin",
            auth_type="none",
            status="active",
        )
        db.add(server)
        await db.flush()
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    await db.flush()

    assert (await _status(db, workspace, generate_ulid()))["requirements"] == []


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


async def test_browser_sessions_are_not_asked_for_and_integrations_are_offered_instead(db_session):
    """Captured browser sessions are not a connection people can add. A site
    with an integration is offered as that integration (connected in place);
    a site without one publishes through Chrome and is not listed."""
    db = db_session
    workspace = await _workspace(db, settings={"_blueprint": {"live_setup_requirements": [
        {"kind": "browser_session", "payload": {"provider": provider, "label": "main"}, "blocking": True}
        for provider in ("medium", "reddit", "instagram")
    ]}})
    result = await _status(db, workspace, generate_ulid())
    assert [item["kind"] for item in result["requirements"]] == ["integration"]
    assert result["requirements"][0]["provider"] == "facebook"
    assert result["requirements"][0]["required"] is False
    assert result["required_issue_count"] == 0


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


@pytest.mark.parametrize(("linked", "platform", "asked_again"), [
    ("instagram_engagement", "instagram", False),
    ("", "instagram", False),
    ("", "xiaohongshu", True),
])
async def test_a_platform_the_workspace_already_connects_through_an_integration_is_not_asked_again(
    db_session, linked, platform, asked_again,
):
    """Local eval 2026-11-03: Meta was asked for, and App's Needs you still read "instagram (no built-in channel)"."""
    workspace = await _workspace(db_session)
    workspace.operating_model = {"channel_config": {"secondary_external_channels": [
        {"channel_type": "other", "provider": platform, "required": True, "linked_service_key": linked},
    ]}}
    workspace.settings = {"flagged_integrations": [
        {"provider": "facebook", "required": True, "linked_service_keys": ["instagram_engagement"]},
    ]}
    await db_session.flush()

    result = await _status(db_session, workspace, generate_ulid())

    labels = [row["label"] for row in result["requirements"] if row["kind"] == "channel"]
    assert bool(labels) is asked_again, labels


async def test_other_platform_channel_is_optional_and_satisfied_by_a_dedicated_integration(db_session):
    """The architect declared X as channel_type='other' (no X channel adapter at the time).

    That used to surface as a required "Other — Attach an active other channel
    account" row nothing could satisfy. It is a platform declaration: optional,
    labelled by the platform, and satisfied by a dedicated integration or the
    Chrome extension.
    """
    db = db_session
    workspace = await _workspace(db)
    user_id = generate_ulid()
    block = {"channel_type": "other", "provider": "twitter_x", "purpose": "Publish X posts", "role": "primary_external"}
    workspace.operating_model = {"channel_config": {"primary_external_channel": block}}
    await db.flush()

    result = await _status(db, workspace, user_id)
    assert result["required_issue_count"] == 0
    [item] = [row for row in result["requirements"] if row["kind"] == "channel"]
    assert item["required"] is False
    assert item["ready"] is False
    assert item["label"] == "twitter_x (no built-in channel)"
    assert item["setup_kind"] == "platform_access"
    assert "no channel account to attach" in item["reason"]
    assert "Chrome extension" in item["reason"]

    # A connected dedicated integration for the platform satisfies it.
    db.add(OAuthAccount(user_id=user_id, provider="twitter_x", provider_user_id="me", access_token="fixture"))
    await db.flush()
    [item] = [row for row in (await _status(db, workspace, user_id))["requirements"] if row["kind"] == "channel"]
    assert item["ready"] is True
    assert "twitter_x integration is connected" in item["reason"]

    # Someone else's account does not; and an explicitly required declaration counts.
    workspace.operating_model = {"channel_config": {"primary_external_channel": {**block, "required": True}}}
    await db.flush()
    other_user = generate_ulid()
    result = await _status(db, workspace, other_user)
    assert result["required_issue_count"] == 1
    [item] = [row for row in result["requirements"] if row["kind"] == "channel"]
    assert item["required"] is True and item["ready"] is False


async def test_inferred_platform_integration_falls_back_to_the_browser_extension(db_session, monkeypatch):
    """Chrome is a general capability: with the extension connected, a platform
    integration agents are merely bound to is no longer a hard requirement.
    A Blueprint-declared blocking requirement still is."""
    from packages.core.services import workspace_connection_status as module
    from packages.core.services.integration_resolution import IntegrationProviderReadiness

    db = db_session
    server = await _server(db)
    workspace = await _workspace(db)
    agent = await _agent(db, workspace)
    db.add(AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id))
    declared = await _workspace(db, settings={"_blueprint": {"live_setup_requirements": [{
        "kind": "missing_integration", "payload": {"provider": server.server_key}, "blocking": True,
    }]}})
    declared_agent = await _agent(db, declared)
    db.add(AgentMCPBinding(agent_id=declared_agent.id, mcp_server_id=server.id))
    await db.flush()
    user_id = generate_ulid()

    # Extension not connected: the inferred requirement is a real issue.
    assert (await _status(db, workspace, user_id))["required_issue_count"] == 1

    real = module.integration_provider_readiness

    async def with_extension(db_, *, entity_id, provider_keys, user_id=None):
        if list(provider_keys) == ["chrome"]:
            return {"chrome": IntegrationProviderReadiness(provider="chrome", ready=True, reason="Connected.", scope="entity")}
        return await real(db_, entity_id=entity_id, provider_keys=provider_keys, user_id=user_id)

    monkeypatch.setattr(module, "integration_provider_readiness", with_extension)

    result = await _status(db, workspace, user_id)
    assert result["required_issue_count"] == 0
    [item] = result["requirements"]
    assert item["required"] is False and item["ready"] is False
    assert item["covered_by"] == "chrome"
    assert "Chrome extension is connected" in item["reason"]

    # Declared-blocking stays required: the Blueprint author asked for it.
    result = await _status(db, declared, user_id)
    assert result["required_issue_count"] == 1
    assert all("covered_by" not in item for item in result["requirements"])


def test_no_builtin_blueprint_asks_for_a_browser_session():
    """Browser sessions cannot be added; Blueprints declare integrations or Chrome."""
    import json
    from pathlib import Path

    configs = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs"
    offenders = {
        path.name: sessions
        for path in sorted(configs.rglob("*.json"))
        if (sessions := (json.loads(path.read_text()).get("contract") or {}).get("sessions"))
    }
    assert not offenders, offenders
