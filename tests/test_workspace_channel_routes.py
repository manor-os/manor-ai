"""Connecting an integration routes the Workspace channels that declared it."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as dbmod
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.user import OAuthAccount
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceStaff
from packages.core.services.workspace_connection_status import WorkspaceConnectionStatusFactory


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    resp = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com",
        "password": "pass123", "entity_name": f"{username} Corp",
    })
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    return headers, data["user_id"], me.json()["entity_id"]


async def _workspace(db, *, entity_id, owner_id, channel, settings=None) -> tuple[str, str]:
    workspace = Workspace(
        entity_id=entity_id, name="Customer care", settings=settings or {},
        operating_model={"channel_config": {
            "primary_external_channel": channel,
            "internal_channel": {"channel_type": "internal_chat", "linked_service_key": "weekly_summary"},
        }},
    )
    db.add(workspace)
    await db.flush()
    if owner_id:
        db.add(WorkspaceStaff(workspace_id=workspace.id, user_id=owner_id, role="owner", status="active"))
    agent = Agent(entity_id=entity_id, name="Customer Care Specialist")
    db.add(agent)
    await db.flush()
    summary = AgentSubscription(
        entity_id=entity_id, workspace_id=workspace.id, agent_id=agent.id, service_key="weekly_summary",
    )
    db.add(summary)
    await db.flush()
    care = AgentSubscription(
        entity_id=entity_id, workspace_id=workspace.id, agent_id=agent.id, service_key="customer_email",
    )
    db.add(care)
    await db.flush()
    return workspace.id, care.id


async def _channel_requirement(workspace_id: str, user_id: str) -> dict:
    async with dbmod.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        status = await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user_id)
    return next(item for item in status["requirements"] if item["kind"] == "channel")


async def _connect(client, monkeypatch, headers, user_id, provider, identity, profile):
    from packages.core.services import oauth_flow

    start = await client.get(f"/api/v1/integrations/oauth/{provider}/start", headers=headers)
    assert start.status_code == 200, start.text
    state = start.json()["state"]

    async def _complete_authorization(**_kwargs):
        await oauth_flow._pop_pending(state, server_key=provider)
        return user_id, oauth_flow.TokenSet(
            access_token=f"{provider}-access", refresh_token=f"{provider}-refresh",
            expires_at=None, provider_user_id=identity, raw={},
        )

    async def _resolve_oauth_identity(*_args, **_kwargs):
        return identity, profile

    monkeypatch.setattr(oauth_flow, "complete_authorization", _complete_authorization)
    monkeypatch.setattr(oauth_flow, "resolve_oauth_identity", _resolve_oauth_identity)
    callback = await client.get(
        f"/api/v1/integrations/oauth/{provider}/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    async with dbmod.async_session() as db:
        return (await db.execute(select(OAuthAccount.id).where(
            OAuthAccount.user_id == user_id, OAuthAccount.provider == provider,
        ))).scalar_one()


async def _bindings(workspace_id: str) -> list[Channel]:
    async with dbmod.async_session() as db:
        return list((await db.execute(select(Channel).where(
            Channel.workspace_id == workspace_id, Channel.type != "internal_chat",
        ))).scalars().all())


GMAIL_CHANNEL = {
    "channel_type": "email", "provider": "gmail", "required": True,
    "purpose": "Receive customer inquiries in Gmail.", "linked_service_key": "customer_email",
}


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.asyncio
async def test_connecting_gmail_routes_the_declared_email_channel(client: AsyncClient, monkeypatch, installed):
    headers, user_id, entity_id = await _register_owner(client, f"gmail_channel_route_{int(installed)}")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")
    requirement_key = "channel:0:email"
    settings = {"_blueprint": {"live_setup_requirements": [{
        "kind": "channel", "blocking": True,
        "payload": {**GMAIL_CHANNEL, "role": "primary_external", "blueprint_requirement_key": requirement_key},
    }]}} if installed else {}
    async with dbmod.async_session() as db:
        workspace_id, care_id = await _workspace(
            db, entity_id=entity_id, owner_id=user_id, channel=GMAIL_CHANNEL, settings=settings,
        )
        # Another member's Workspace is never routed to this user's mailbox.
        foreign_id, _ = await _workspace(db, entity_id=entity_id, owner_id=None, channel=GMAIL_CHANNEL)
        await db.commit()

    missing = await _channel_requirement(workspace_id, user_id)
    assert missing["ready"] is False
    assert missing["reason"] == "Attach an active email channel account to this Workspace."

    await _connect(client, monkeypatch, headers, user_id, "gmail", "google-sub-1", {"email": "shop@example.com"})

    ready = await _channel_requirement(workspace_id, user_id)
    assert ready["ready"] is True, ready
    [binding] = await _bindings(workspace_id)
    assert binding.agent_subscription_id == care_id
    if installed:
        assert binding.config["blueprint_requirement_key"] == requirement_key
    async with dbmod.async_session() as db:
        route = await db.get(ChannelConfig, binding.config["channel_config_id"])
    assert (route.workspace_id, route.channel_type, route.provider) == (workspace_id, "email", "gmail")
    assert route.config["role"] == "primary_external"
    assert await _bindings(foreign_id) == []

    # Reconnecting the same mailbox does not add a second route.
    await _connect(client, monkeypatch, headers, user_id, "gmail", "google-sub-1", {"email": "shop@example.com"})
    assert len(await _bindings(workspace_id)) == 1


@pytest.mark.asyncio
async def test_connecting_slack_attaches_the_connected_slack_account(client: AsyncClient, monkeypatch):
    headers, user_id, entity_id = await _register_owner(client, "slack_channel_route")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("APP_URL", "http://localhost:3010")
    async with dbmod.async_session() as db:
        workspace_id, care_id = await _workspace(db, entity_id=entity_id, owner_id=user_id, channel={
            "channel_type": "slack", "provider": "slack", "required": True,
            "purpose": "Answer the team's questions in Slack.", "linked_service_key": "customer_email",
        })
        await db.commit()
    assert (await _channel_requirement(workspace_id, user_id))["ready"] is False

    account_id = await _connect(
        client, monkeypatch, headers, user_id, "slack", "slack:A-ROUTE:team:T-ROUTE",
        {"app_id": "A-ROUTE", "team_id": "T-ROUTE", "team_name": "Route Team"},
    )

    assert (await _channel_requirement(workspace_id, user_id))["ready"] is True
    [binding] = await _bindings(workspace_id)
    async with dbmod.async_session() as db:
        account = await db.get(ChannelConfig, binding.config["channel_config_id"])
        workspace_routes = (await db.execute(select(ChannelConfig.id).where(
            ChannelConfig.workspace_id == workspace_id, ChannelConfig.channel_type == "slack",
        ))).scalars().all()
    # Slack inbound reaches only the user's installation account, so that
    # account is attached instead of a credentialless Workspace route.
    assert account.credential_source_id == account_id
    assert account.config["slack_team_id"] == "T-ROUTE"
    assert workspace_routes == []
    assert (binding.user_id, binding.agent_subscription_id) == (user_id, care_id)
