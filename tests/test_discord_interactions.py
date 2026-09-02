"""Discord signed Interactions routing and delivery deduplication."""
from __future__ import annotations

import json
import hashlib
import inspect
import time
from types import SimpleNamespace
from typing import Any

import pytest
from nacl.signing import SigningKey
from sqlalchemy import func, select

from packages.core.credentials import get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.models.user import OAuthAccount
from tests.test_document_permissions import _auth


async def _async_value(value):
    return value


def _signed_headers(signing_key: SigningKey, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = signing_key.sign(timestamp.encode() + body).signature
    return {
        "Content-Type": "application/json",
        "X-Signature-Ed25519": signature.hex(),
        "X-Signature-Timestamp": timestamp,
    }


def _configure_app(monkeypatch, signing_key: SigningKey) -> None:
    monkeypatch.setenv("DISCORD_CLIENT_ID", "discord-app-id")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "discord-client-secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "deployment-bot-token")
    monkeypatch.setenv(
        "DISCORD_PUBLIC_KEY",
        signing_key.verify_key.encode().hex(),
    )


def _interaction_payload(
    *,
    interaction_id: str = "interaction-1",
    application_id: str = "discord-app-id",
    guild_id: str = "guild-123",
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "application_id": application_id,
        "guild_id": guild_id,
        "channel_id": "channel-456",
        "token": "interaction-token",
        "type": 2,
        "data": {
            "name": "manor",
            "options": [
                {"name": "message", "type": 3, "value": "hello"},
            ],
        },
        "member": {
            "user": {"id": "discord-user", "username": "tester"},
        },
    }


async def _seed_discord_connection(client, db_session) -> tuple[dict, ChannelConfig]:
    headers = await _auth(client, "discord_interaction_owner")
    owner_response = await client.get("/api/v1/auth/me", headers=headers)
    assert owner_response.status_code == 200
    owner = owner_response.json()
    account = OAuthAccount(
        id=generate_ulid(),
        user_id=owner["id"],
        provider="discord",
        provider_user_id="discord:discord-app-id:guild:guild-123",
        profile={
            "application_id": "discord-app-id",
            "guild_id": "guild-123",
            "guild_name": "Manor QA",
        },
    )
    get_credential_service().store_oauth_account(
        account,
        {"access_token": "discord-install-access-token"},
    )
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="discord",
        provider="discord_app",
        name="Manor QA",
        credential_source_kind="oauth_account",
        credential_source_id=account.id,
        discord_application_id="discord-app-id",
        discord_guild_id="guild-123",
        config={
            "connection_kind": "oauth_account",
            "connection_id": account.id,
            "discord_application_id": "discord-app-id",
            "discord_guild_id": "guild-123",
        },
        credentials={},
        status="active",
    )
    db_session.add_all([account, config])
    await db_session.commit()
    return owner, config


@pytest.mark.asyncio
async def test_discord_interactions_requires_valid_signature_and_answers_ping(
    client,
    monkeypatch,
) -> None:
    signing_key = SigningKey.generate()
    _configure_app(monkeypatch, signing_key)
    body = json.dumps(
        {"type": 1, "application_id": "discord-app-id"},
        separators=(",", ":"),
    ).encode()

    unsigned = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
    )
    bad_signature = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
        headers={
            "X-Signature-Ed25519": "00" * 64,
            "X-Signature-Timestamp": str(int(time.time())),
        },
    )
    verified = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
        headers=_signed_headers(signing_key, body),
    )

    assert unsigned.status_code == 401
    assert bad_signature.status_code == 401
    assert verified.status_code == 200
    assert verified.json() == {"type": 1}


@pytest.mark.asyncio
async def test_discord_interactions_rejects_mismatched_application(
    client,
    monkeypatch,
) -> None:
    signing_key = SigningKey.generate()
    _configure_app(monkeypatch, signing_key)
    body = json.dumps(
        {"type": 1, "application_id": "another-app"},
        separators=(",", ":"),
    ).encode()

    response = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
        headers=_signed_headers(signing_key, body),
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_discord_unknown_guild_receives_private_connection_message(
    client,
    monkeypatch,
) -> None:
    signing_key = SigningKey.generate()
    _configure_app(monkeypatch, signing_key)
    body = json.dumps(
        _interaction_payload(guild_id="unknown-guild"),
        separators=(",", ":"),
    ).encode()

    response = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
        headers=_signed_headers(signing_key, body),
    )

    assert response.status_code == 200
    assert response.json() == {
        "type": 4,
        "data": {
            "content": "This Discord Server is not connected to Manor.",
            "flags": 64,
        },
    }


@pytest.mark.asyncio
async def test_discord_command_routes_private_connection_and_deduplicates(
    client,
    db_session,
    monkeypatch,
) -> None:
    signing_key = SigningKey.generate()
    _configure_app(monkeypatch, signing_key)
    owner, config = await _seed_discord_connection(client, db_session)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(
        dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    body = json.dumps(
        _interaction_payload(),
        separators=(",", ":"),
    ).encode()
    headers = _signed_headers(signing_key, body)

    first = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
        headers=headers,
    )
    duplicate = await client.post(
        "/api/v1/channels/discord/interactions",
        content=body,
        headers=headers,
    )

    assert first.status_code == 200
    assert first.json() == {"type": 5}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"type": 5}
    assert len(queued) == 1
    inbound_log_id = queued[0].pop("inbound_message_log_id")
    assert queued == [{
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "discord",
        "sender_id": "discord-user",
        "sender_name": "tester",
        "chat_id": "channel-456",
        "content": "hello",
        "reply_context": {
            "application_id": "discord-app-id",
            "interaction_token": "interaction-token",
        },
    }]
    log_count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
            MessageLog.channel_type == "discord",
            MessageLog.external_id == "interaction-1",
        )
    )
    assert log_count == 1
    inbound_log = await db_session.get(MessageLog, inbound_log_id)
    assert inbound_log is not None
    assert inbound_log.status == "received"


@pytest.mark.asyncio
async def test_discord_adapter_edits_original_interaction_without_bot_auth(
    monkeypatch,
) -> None:
    from packages.core.services.channels import discord_adapter as module
    from packages.core.services.channels.discord_adapter import DiscordChannelAdapter

    captured: dict[str, Any] = {}

    class _Response:
        is_success = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "discord-original-message"}

    class _Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, **kwargs):
            captured.update(method="PATCH", url=url, **kwargs)
            return _Response()

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=_Client))
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        channel_type="discord",
        provider="discord_app",
        credential_source_kind="oauth_account",
        credential_source_id=generate_ulid(),
        config={},
        credentials={},
        status="active",
    )

    result = await DiscordChannelAdapter().send_text(
        config,
        "channel-456",
        "Agent reply",
        reply_context={
            "application_id": "discord-app-id",
            "interaction_token": "interaction-token",
        },
    )

    assert captured["method"] == "PATCH"
    assert captured["url"] == (
        "https://discord.com/api/v10/webhooks/discord-app-id/"
        "interaction-token/messages/@original"
    )
    assert captured["json"] == {"content": "Agent reply"}
    assert "headers" not in captured
    assert result == {
        "channel_id": "channel-456",
        "message_id": "discord-original-message",
        "status": "sent",
    }


@pytest.mark.asyncio
async def test_discord_adapter_classifies_http_rejection_as_determinate(
    monkeypatch,
) -> None:
    from packages.core.services.channels import discord_adapter as module
    from packages.core.services.channels.base import (
        ChannelTextSendError,
        ChannelTextSendFailureDisposition,
    )
    from packages.core.services.channels.discord_adapter import DiscordChannelAdapter

    class _Response:
        is_success = False
        status_code = 404
        text = "Unknown Channel"

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, _url, **_kwargs):
            return _Response()

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=_Client))
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        channel_type="discord",
        provider="discord_app",
        config={},
        credentials={},
        status="active",
    )

    with pytest.raises(ChannelTextSendError) as exc_info:
        await DiscordChannelAdapter().send_text(
            config,
            "missing-channel",
            "Agent reply",
            reply_context={
                "application_id": "discord-app-id",
                "interaction_token": "interaction-token",
            },
        )

    assert (
        exc_info.value.disposition
        is ChannelTextSendFailureDisposition.DETERMINATE
    )

    assert (
        ChannelTextSendError.from_http_status(
            "Discord API error 503",
            status_code=503,
        ).disposition
        is ChannelTextSendFailureDisposition.AMBIGUOUS
    )


@pytest.mark.asyncio
async def test_discord_adapter_uses_nonce_for_gateway_reply_retries(monkeypatch) -> None:
    from packages.core.services.channels import discord_adapter as module
    from packages.core.services.channels.discord_adapter import DiscordChannelAdapter

    captured: dict[str, Any] = {}

    class _Response:
        is_success = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "discord-message-1"}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return _Response()

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=_Client))
    monkeypatch.setattr(module, "_deployment_bot_token", lambda: _async_value("deployment-bot-token"))
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        channel_type="discord",
        provider="discord_app",
        config={},
        credentials={},
        status="active",
    )

    result = await DiscordChannelAdapter().send_text(
        config,
        "channel-456",
        "Agent reply",
        idempotency_key="inbound-receipt-1",
    )

    assert captured["json"] == {
        "content": "Agent reply",
        "nonce": "inbound-receipt-1",
        "enforce_nonce": True,
    }
    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_discord_adapter_hashes_overlong_nonce_without_losing_idempotency(
    monkeypatch,
) -> None:
    from packages.core.services.channels import discord_adapter as module
    from packages.core.services.channels.discord_adapter import DiscordChannelAdapter

    captured: dict[str, Any] = {}

    class _Response:
        is_success = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "discord-message-2"}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return _Response()

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=_Client))
    monkeypatch.setattr(module, "_deployment_bot_token", lambda: _async_value("deployment-bot-token"))
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        channel_type="discord",
        provider="discord_app",
        config={},
        credentials={},
        status="active",
    )
    idempotency_key = "01M0XYGN95WTTSCKSGW69Q0P7Z"

    result = await DiscordChannelAdapter().send_text(
        config,
        "channel-456",
        "Agent reply",
        idempotency_key=idempotency_key,
    )

    assert captured["json"] == {
        "content": "Agent reply",
        "nonce": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:25],
        "enforce_nonce": True,
    }
    assert len(captured["json"]["nonce"]) == 25
    assert result["status"] == "sent"


def test_discord_worker_gateway_and_delivery_accept_reply_context() -> None:
    from packages.core.services.channel_gateway import dispatch_inbound
    from packages.core.services.channel_outbound_delivery import send_channel_text_reply
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    assert "reply_context" in inspect.signature(dispatch_inbound_task.run).parameters
    assert "reply_context" in inspect.signature(dispatch_inbound).parameters
    assert "reply_context" in inspect.signature(send_channel_text_reply).parameters
    assert "reply_idempotency_key" in inspect.signature(dispatch_inbound).parameters
    assert "idempotency_key" in inspect.signature(send_channel_text_reply).parameters


@pytest.mark.asyncio
async def test_channel_delivery_passes_discord_reply_context_to_adapter(
    monkeypatch,
) -> None:
    from packages.core.services import channel_outbound_delivery as module

    captured: dict[str, Any] = {}

    class _Adapter:
        async def send_text(self, _cc, to, text, **kwargs):
            captured.update(to=to, text=text, kwargs=kwargs)
            return {"status": "sent", "message_id": "original-message"}

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def _load_config(_db, _cc_id):
        return SimpleNamespace(
            entity_id="entity-id",
            channel_type="discord",
            status="active",
            workspace_id=None,
        )

    async def _available(*_args, **_kwargs):
        return True

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setitem(module.ADAPTERS, "discord", _Adapter())
    monkeypatch.setattr(module, "async_session", lambda: _SessionContext())
    monkeypatch.setattr(module, "load_channel_config", _load_config)
    monkeypatch.setattr(module, "channel_credential_source_is_available", _available)
    monkeypatch.setattr(module, "channel_workspace_is_routable", _available)
    monkeypatch.setattr(module, "mark_last_channel_outbound_sent", _noop)

    reply_context = {
        "application_id": "discord-app-id",
        "interaction_token": "interaction-token",
    }
    sent = await module.send_channel_text_reply(
        cc_id="config-id",
        channel_type="discord",
        chat_id="channel-456",
        text="Agent reply",
        reply_context=reply_context,
    )

    assert sent is True
    assert captured == {
        "to": "channel-456",
        "text": "Agent reply",
        "kwargs": {"reply_context": reply_context},
    }
