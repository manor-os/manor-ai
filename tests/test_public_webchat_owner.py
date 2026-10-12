"""Public voice keeps the explicit webchat creator as its billing actor."""

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select

from apps.api.chat_voice import CallStart, resolve_call_scope
from apps.api.routers.public_chat import _public_audio_scope
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup


async def _register(client, name):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": name,
            "email": f"{name}@test.com",
            "password": "pass123",
            "entity_name": f"{name} Corp",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return data, {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.parametrize("entry", ["workspace_api", "setup"])
@pytest.mark.parametrize("visitor", ["anonymous", "other_entity"])
async def test_created_webchat_admits_speech_and_call_as_creator(
    client, db_session, entry, visitor, monkeypatch, tmp_path,
):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    owner, headers = await _register(client, "publicvoiceowner")
    visitor_data, visitor_headers = await _register(client, "publicvoicevisitor")
    agent_response = await client.post(
        "/api/v1/agents", headers=headers, json={"name": "Public Support"},
    )
    assert agent_response.status_code == 201, agent_response.text
    agent_id = agent_response.json()["id"]
    channel = {
        "channel_type": "webchat",
        "provider": "webchat",
        "linked_service_key": "support",
        "login_required": visitor == "other_entity",
        # Model/client configuration cannot select a different billing actor.
        "owner_user_id": visitor_data["user_id"],
    }
    if entry == "setup":
        workspace_id = await finalize_setup(
            WorkspaceSetupSession(
                entity_id=owner["entity_id"],
                user_id=owner["user_id"],
                fields={
                    "name": "Public Voice Workspace",
                    "kind": "support_desk",
                    "operating_context": "Customer support",
                    "primary_work": "Answer customer questions",
                    "services": [{"service_key": "support", "title": "Support"}],
                    "agent_mappings": [{"service_key": "support", "agent_id": agent_id}],
                    "goals": [],
                    "channel_config": {"channels": [channel]},
                },
                messages=[],
                ready=True,
            ),
            db_session,
        )
        await db_session.commit()
    else:
        created = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "Public Voice Workspace"},
        )
        assert created.status_code == 201, created.text
        workspace_id = created.json()["id"]
        subscription = AgentSubscription(
            entity_id=owner["entity_id"], workspace_id=workspace_id,
            agent_id=agent_id, service_key="support", status="active",
        )
        db_session.add(subscription)
        await db_session.commit()
        attached = await client.post(
            f"/api/v1/workspaces/{workspace_id}/channels",
            headers=headers,
            json={
                "channel_type": "webchat", "agent_subscription_id": subscription.id,
                "config": channel,
            },
        )
        assert attached.status_code == 201, attached.text

    config = (await db_session.scalars(select(ChannelConfig).where(
        ChannelConfig.workspace_id == workspace_id,
        ChannelConfig.channel_type == "webchat",
    ))).one()
    binding = (await db_session.scalars(select(Channel).where(
        Channel.workspace_id == workspace_id, Channel.type == "webchat",
    ))).one()
    assert config.owner_user_id == binding.user_id == owner["user_id"]
    assert config.owner_user_id != visitor_data["user_id"]
    token = config.config["public_token"]
    access_headers = visitor_headers if visitor == "other_entity" else {}
    session = await client.post(
        f"/api/v1/public/chat/{token}/session", headers=access_headers, json={},
    )
    assert session.status_code == 200, session.text
    session_id = session.json()["session_id"]
    request = Request({
        "type": "http",
        "headers": [(key.lower().encode(), value.encode()) for key, value in access_headers.items()],
    })
    speech_scope = await _public_audio_scope(db_session, token, request, session_id)
    call_scope = await resolve_call_scope(
        db_session, request, CallStart(type="start", public_token=token, session_id=session_id),
        create=True,
    )
    assert speech_scope == call_scope
    assert speech_scope.user_id == owner["user_id"]
    assert speech_scope.entity_id == owner["entity_id"]
    assert speech_scope.workspace_id == workspace_id
    assert speech_scope.agent_id == agent_id

    # Legacy unknown ownership never falls back to the visitor or Workspace creator.
    config.owner_user_id = None
    binding.user_id = None
    await db_session.commit()
    assert (await db_session.get(Workspace, workspace_id)).settings["created_by_user_id"] == owner["user_id"]
    for is_call in (False, True):
        with pytest.raises(HTTPException) as error:
            if is_call:
                await resolve_call_scope(
                    db_session, request,
                    CallStart(type="start", public_token=token, session_id=session_id),
                    create=True,
                )
            else:
                await _public_audio_scope(db_session, token, request, session_id)
        assert error.value.status_code == 503
        assert error.value.detail == "Chat voice is not configured"


async def test_background_channel_repair_does_not_guess_a_billing_owner(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.services.workspace_operation_service import repair_workspace_operation_runtime

    workspace = Workspace(
        entity_id=generate_ulid(), name="Legacy channel declaration", status="active",
        settings={"created_by_user_id": generate_ulid()},
        operating_model={"channel_config": {"channels": [{"channel_type": "webchat"}]}},
    )
    db_session.add(workspace)
    await db_session.flush()
    result = await repair_workspace_operation_runtime(
        db_session, workspace.id, workspace.entity_id, user_id=None,
    )
    assert result["channels"]["created_configs"] == 1
    config = (await db_session.scalars(select(ChannelConfig).where(
        ChannelConfig.workspace_id == workspace.id,
    ))).one()
    binding = (await db_session.scalars(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ))).one()
    assert config.owner_user_id is None
    assert binding.user_id is None
