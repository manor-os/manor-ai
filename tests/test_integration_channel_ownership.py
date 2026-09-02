"""Owner-only ChannelConfig visibility and agent binding."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

import packages.core.database as db_module
from packages.core.models.channel import ChannelConfig
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client, headers: dict) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_channel_bindings_are_visible_and_bindable_only_by_the_connection_owner(client):
    from packages.core.models.user import OAuthAccount, UserMembership
    from packages.core.services.integration_service import (
        list_channel_bindings,
        upsert_channel_binding,
    )

    owner_headers = await _auth(client, "channel_binding_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(
        owner["entity_id"], "channel_binding_member", role="member",
    )

    async with db_module.async_session() as db:
        db.add(UserMembership(
            user_id=member["id"],
            entity_id=owner["entity_id"],
            role="member",
            status="active",
        ))
        source_id = "source_slack_owner"
        source = OAuthAccount(
            id=source_id,
            user_id=owner["id"],
            provider="slack",
            provider_user_id="channel-binding-owner",
            profile={"team_id": "owner-team", "team_name": "Owner Slack"},
        )
        config = ChannelConfig(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            credential_source_kind="oauth_account",
            credential_source_id=source_id,
            channel_type="slack",
            provider="slack_app",
            config={"name": "Owner Slack"},
            credentials={},
            status="active",
        )
        db.add_all([source, config])
        await db.commit()

        owner_rows = await list_channel_bindings(
            db, owner["entity_id"], owner["id"],
        )
        member_rows = await list_channel_bindings(
            db, owner["entity_id"], member["id"],
        )
        assert [row["channel_config_id"] for row in owner_rows] == [config.id]
        assert member_rows == []

        owner_binding = await upsert_channel_binding(
            db,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            channel_config_id=config.id,
            agent_id=None,
        )
        assert owner_binding.user_id == owner["id"]

        with pytest.raises(ValueError, match="Channel config not found"):
            await upsert_channel_binding(
                db,
                entity_id=owner["entity_id"],
                user_id=member["id"],
                channel_config_id=config.id,
                agent_id=None,
            )


@pytest.mark.asyncio
async def test_discord_channel_binding_records_connection_owner(client):
    from packages.core.services.integration_service import upsert_channel_binding

    owner_headers = await _auth(client, "discord_channel_binding_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        config = ChannelConfig(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            credential_source_kind="oauth_account",
            credential_source_id="source_discord_owner",
            channel_type="discord",
            provider="discord_app",
            config={
                "name": "Owner Discord",
                "discord_application_id": "discord-app-id",
                "discord_guild_id": "guild-owner",
            },
            credentials={},
            status="active",
        )
        db.add(config)
        await db.commit()

        binding = await upsert_channel_binding(
            db,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            channel_config_id=config.id,
            agent_id="manor-master",
        )

        assert binding.user_id == owner["id"]

        binding = await upsert_channel_binding(
            db,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            channel_config_id=config.id,
            agent_id=None,
        )

        assert binding.user_id == owner["id"]


@pytest.mark.asyncio
async def test_whatsapp_binding_requires_one_exact_active_subscription(client):
    from packages.core.models.document import Channel
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.integration_service import (
        list_channel_bindings,
        upsert_channel_binding,
    )

    owner_headers = await _auth(client, "whatsapp_exact_binding_owner")
    owner = await _me(client, owner_headers)
    other_admin = await _create_entity_user(
        owner["entity_id"],
        "whatsapp_exact_binding_other_admin",
        role="owner",
    )

    async with db_module.async_session() as db:
        workspace = Workspace(
            entity_id=owner["entity_id"],
            name="WhatsApp Support",
            status="active",
        )
        agent = Agent(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            name="WhatsApp Support Agent",
            status="active",
        )
        db.add_all([workspace, agent])
        await db.flush()
        subscription = AgentSubscription(
            entity_id=owner["entity_id"],
            agent_id=agent.id,
            workspace_id=workspace.id,
            name="WhatsApp Support deployment",
            status="active",
        )
        config = ChannelConfig(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            credential_source_kind="integration",
            credential_source_id="whatsapp-integration-owner",
            channel_type="whatsapp",
            provider="whatsapp_cloud",
            whatsapp_phone_number_id="phone-exact-binding",
            config={"name": "WhatsApp Support"},
            credentials={},
            status="active",
        )
        db.add_all([subscription, config])
        await db.commit()

        binding = await upsert_channel_binding(
            db,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            channel_config_id=config.id,
            agent_id=None,
            agent_subscription_id=subscription.id,
            user_role=owner["role"],
        )

        assert binding.agent_subscription_id == subscription.id
        assert binding.agent_id == subscription.agent_id
        assert binding.workspace_id == subscription.workspace_id
        assert binding.user_id == config.owner_user_id

        projected = await list_channel_bindings(
            db,
            owner["entity_id"],
            owner["id"],
        )
        assert projected[0]["bound_agent_subscription_id"] == subscription.id
        assert projected[0]["bound_workspace_id"] == workspace.id
        assert projected[0]["workspace_name"] == workspace.name

        with pytest.raises(ValueError, match="agent_subscription_id"):
            await upsert_channel_binding(
                db,
                entity_id=owner["entity_id"],
                user_id=owner["id"],
                channel_config_id=config.id,
                agent_id=None,
                agent_subscription_id=None,
                user_role=owner["role"],
            )

        with pytest.raises(ValueError, match="agent_subscription_id"):
            await upsert_channel_binding(
                db,
                entity_id=owner["entity_id"],
                user_id=owner["id"],
                channel_config_id=config.id,
                agent_id=agent.id,
                agent_subscription_id=None,
                user_role=owner["role"],
            )

        with pytest.raises(ValueError, match="Channel config not found"):
            await upsert_channel_binding(
                db,
                entity_id=owner["entity_id"],
                user_id=other_admin["id"],
                channel_config_id=config.id,
                agent_id=None,
                agent_subscription_id=subscription.id,
                user_role="owner",
            )

        duplicate = Channel(
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            workspace_id=workspace.id,
            type="whatsapp",
            name="Duplicate WhatsApp route",
            config={"channel_config_id": config.id},
            agent_id=agent.id,
            agent_subscription_id=subscription.id,
            status="active",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


@pytest.mark.asyncio
async def test_non_whatsapp_binding_list_keeps_legacy_duplicate_behavior(client):
    from packages.core.models.document import Channel
    from packages.core.services.integration_service import list_channel_bindings

    owner_headers = await _auth(client, "legacy_duplicate_binding_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        config = ChannelConfig(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            channel_type="slack",
            provider="slack_app",
            config={"name": "Legacy duplicate Slack"},
            credentials={},
            status="active",
        )
        db.add(config)
        await db.flush()
        first = Channel(
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            type="slack",
            name="Legacy duplicate Slack one",
            config={"channel_config_id": config.id},
            agent_id="manor-master",
            status="active",
        )
        second = Channel(
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            type="slack",
            name="Legacy duplicate Slack two",
            config={"channel_config_id": config.id},
            agent_id="manor-master",
            status="active",
        )
        db.add_all([first, second])
        await db.commit()

        projected = await list_channel_bindings(
            db,
            owner["entity_id"],
            owner["id"],
        )

        assert projected[0]["bound_channel_id"] in {first.id, second.id}


@pytest.mark.asyncio
async def test_workspace_manager_cannot_attach_another_users_private_slack(client):
    from packages.core.models.user import UserMembership

    owner_headers = await _auth(client, "workspace_slack_connection_owner")
    owner = await _me(client, owner_headers)
    other_admin = await _create_entity_user(
        owner["entity_id"],
        "workspace_slack_other_admin",
        role="owner",
    )
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private Slack Workspace"},
    )
    assert workspace.status_code == 201, workspace.text

    async with db_module.async_session() as db:
        db.add(UserMembership(
            user_id=other_admin["id"],
            entity_id=owner["entity_id"],
            role="owner",
            status="active",
        ))
        config = ChannelConfig(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            credential_source_kind="oauth_account",
            credential_source_id="source_private_slack",
            channel_type="slack",
            provider="slack_app",
            config={"name": "Owner private Slack"},
            credentials={},
            status="active",
        )
        db.add(config)
        await db.commit()

    available = await client.get(
        f"/api/v1/workspaces/{workspace.json()['id']}/channels/available",
        headers=other_admin["headers"],
    )
    attached = await client.post(
        f"/api/v1/workspaces/{workspace.json()['id']}/channels",
        headers=other_admin["headers"],
        json={
            "channel_config_id": config.id,
            "channel_type": "slack",
            "agent_id": "manor-master",
        },
    )

    assert available.status_code == 200
    assert config.id not in {row["id"] for row in available.json()}
    assert attached.status_code == 404


@pytest.mark.asyncio
async def test_private_slack_installation_can_only_have_one_active_binding(client):
    owner_headers = await _auth(client, "single_slack_binding_owner")
    owner = await _me(client, owner_headers)
    first_workspace = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "First Slack Workspace"},
    )
    second_workspace = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Second Slack Workspace"},
    )
    assert first_workspace.status_code == 201
    assert second_workspace.status_code == 201

    async with db_module.async_session() as db:
        config = ChannelConfig(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            credential_source_kind="oauth_account",
            credential_source_id="source_single_slack",
            channel_type="slack",
            provider="slack_app",
            config={"name": "Single Slack installation"},
            credentials={},
            status="active",
        )
        db.add(config)
        await db.commit()

    payload = {
        "channel_config_id": config.id,
        "channel_type": "slack",
        "agent_id": "manor-master",
    }
    first = await client.post(
        f"/api/v1/workspaces/{first_workspace.json()['id']}/channels",
        headers=owner_headers,
        json=payload,
    )
    second = await client.post(
        f"/api/v1/workspaces/{second_workspace.json()['id']}/channels",
        headers=owner_headers,
        json=payload,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert second.json()["detail"] == (
        "Slack installation already has an active Agent binding"
    )
