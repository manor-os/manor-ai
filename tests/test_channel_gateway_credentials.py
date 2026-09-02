"""Inbound channel gateway credential-source guards."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_whatsapp_dispatch_stops_after_source_connection_is_revoked(monkeypatch):
    from packages.core.services import channel_gateway
    from packages.core.services import channel_credentials

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    config = SimpleNamespace(
        entity_id="entity-id",
        channel_type="whatsapp",
        status="active",
    )

    async def load_config(_db, _config_id):
        return config

    async def source_available(_db, _config):
        return False

    monkeypatch.setattr(channel_gateway, "async_session", lambda: Session())
    monkeypatch.setattr(channel_gateway, "load_channel_config", load_config)
    monkeypatch.setattr(
        channel_credentials,
        "channel_credential_source_is_available",
        source_available,
    )

    result = await channel_gateway.dispatch_inbound(
        entity_id="entity-id",
        channel_config_id="channel-config-id",
        channel_type="whatsapp",
        sender_id="15550001111",
        sender_name="Sender",
        chat_id="15550001111",
        content="Hello",
    )

    assert result == {
        "status": "skipped",
        "reason": "channel credential source unavailable",
    }


@pytest.mark.asyncio
async def test_twilio_voice_dispatch_rejects_binding_scope_changed_mid_call(monkeypatch):
    from packages.core.services import channel_credentials, channel_gateway

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    config = SimpleNamespace(
        entity_id="entity-id",
        channel_type="twilio_voice",
        status="active",
        owner_user_id="owner-id",
        workspace_id="workspace-original",
    )
    binding = SimpleNamespace(
        status="active",
        user_id="owner-id",
        workspace_id="workspace-original",
    )
    rebound_subscription = SimpleNamespace(
        id="subscription-new",
        agent_id="agent-new",
        workspace_id="workspace-new",
    )

    async def load_config(_db, _config_id):
        return config

    async def source_available(_db, _config):
        return True

    async def workspace_routable(_db, _workspace_id, *, entity_id):
        return entity_id == "entity-id"

    async def load_binding(_db, _config, _binding_id):
        return binding

    async def resolve_rebound(_db, *, binding, contact):
        assert contact is None
        return rebound_subscription

    async def unexpected_contact(*_args, **_kwargs):
        raise AssertionError("scope mismatch must fail before creating a contact")

    monkeypatch.setattr(channel_gateway, "async_session", lambda: Session())
    monkeypatch.setattr(channel_gateway, "load_channel_config", load_config)
    monkeypatch.setattr(
        channel_gateway,
        "load_channel_binding_by_id_for_config",
        load_binding,
    )
    monkeypatch.setattr(
        channel_gateway,
        "channel_workspace_is_routable",
        workspace_routable,
    )
    monkeypatch.setattr(channel_gateway, "resolve_subscription", resolve_rebound)
    monkeypatch.setattr(channel_gateway, "upsert_channel_contact", unexpected_contact)
    monkeypatch.setattr(
        channel_credentials,
        "channel_credential_source_is_available",
        source_available,
    )

    result = await channel_gateway.dispatch_inbound(
        entity_id="entity-id",
        channel_config_id="channel-config-id",
        channel_type="twilio_voice",
        channel_binding_id="binding-id",
        channel_agent_subscription_id="subscription-original",
        channel_agent_id="agent-original",
        channel_workspace_id="workspace-original",
        sender_id="15550001111",
        sender_name="Caller",
        chat_id="15550001111",
        content="Hello",
    )

    assert result == {
        "status": "unbound",
        "channel_config_id": "channel-config-id",
        "reason": "binding_scope_changed",
    }
