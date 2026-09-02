"""Credential leases for source-linked communication channels."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.credentials import get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from tests.test_document_permissions import _auth


@pytest.fixture(autouse=True)
def _whatsapp_deployment_config(monkeypatch):
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", "meta-app-secret")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "whatsapp-verify-token")


async def _current_user(client, headers: dict) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _source_linked_config(channel_type: str) -> ChannelConfig:
    return ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        channel_type=channel_type,
        provider=f"{channel_type}_test",
        credential_source_kind="integration",
        credential_source_id=generate_ulid(),
        config={},
        credentials={},
        status="active",
    )


def _lease_only_from_source(monkeypatch, credentials: dict) -> None:
    from packages.core.services.channels.base import ChannelAdapter

    async def source_credentials(self, cc, *, reason: str) -> dict:
        assert cc.credentials == {}
        return credentials

    monkeypatch.setattr(ChannelAdapter, "credentials", source_credentials)


@pytest.mark.asyncio
async def test_email_adapter_uses_source_credentials_for_smtp(monkeypatch):
    from packages.core.services.channels.email_adapter import EmailChannelAdapter
    from packages.core.services import smtp_transport

    _lease_only_from_source(monkeypatch, {
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "username": "bot@example.test",
        "password": "smtp-secret",
        "from_address": "bot@example.test",
    })
    captured: dict = {}

    async def fake_send_message_async(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(smtp_transport, "send_message_async", fake_send_message_async)

    result = await EmailChannelAdapter().send_text(
        _source_linked_config("email"), "person@example.test", "hello",
    )

    assert result["status"] == "sent"
    assert captured["host"] == "smtp.example.test"
    assert captured["username"] == "bot@example.test"
    assert captured["password"] == "smtp-secret"


@pytest.mark.asyncio
async def test_whatsapp_adapter_uses_source_credentials_for_outbound(monkeypatch):
    from packages.core.services.channels.whatsapp_adapter import (
        WhatsAppAdapter,
        WhatsAppChannelAdapter,
    )

    _lease_only_from_source(monkeypatch, {
        "phone_number_id": "phone-source",
        "access_token": "whatsapp-source-token",
    })
    captured: dict[str, str] = {}

    async def fake_send_text(self, to: str, text: str) -> dict:
        captured.update(phone_number_id=self.phone_number_id, access_token=self.access_token)
        return {"status": "sent", "to": to, "text": text}

    monkeypatch.setattr(WhatsAppAdapter, "send_text", fake_send_text)

    result = await WhatsAppChannelAdapter().send_text(
        _source_linked_config("whatsapp"), "15550001111", "hello",
    )

    assert result["status"] == "sent"
    assert captured == {
        "phone_number_id": "phone-source",
        "access_token": "whatsapp-source-token",
    }


@pytest.mark.asyncio
async def test_whatsapp_adapter_resolves_nango_credentials_at_runtime(monkeypatch):
    from packages.core.services.channels import whatsapp_adapter as whatsapp_module
    from packages.core.services.channels.whatsapp_adapter import WhatsAppChannelAdapter

    calls: dict[str, object] = {}

    async def fake_lease(cc, *, reason: str, resolve_nango: bool = False) -> dict:
        calls.update(reason=reason, resolve_nango=resolve_nango)
        return {
            "via": "nango",
            "connection_id": "teamsafe-whatsapp-connection",
            "phone_number_id": "phone-from-nango",
            "access_token": "token-from-nango",
        }

    monkeypatch.setattr(whatsapp_module, "lease_channel_config_credentials", fake_lease)
    adapter = await WhatsAppChannelAdapter()._build(
        _source_linked_config("whatsapp"),
        reason="test.whatsapp.nango",
    )

    assert adapter.phone_number_id == "phone-from-nango"
    assert adapter.access_token == "token-from-nango"
    assert calls["resolve_nango"] is True


def test_whatsapp_business_config_uses_one_deployment_source(monkeypatch):
    from packages.core.services import whatsapp_business_config as module

    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv(
        "NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET",
        "meta-app-secret",
    )
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "webhook-verify-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "legacy-app-secret")
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "legacy-api-token")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "legacy-phone-id")
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example/"),
    )

    config = module.load_whatsapp_business_config()

    assert config.app_id == "meta-app-id"
    assert config.app_secret == "meta-app-secret"
    assert config.verify_token == "webhook-verify-token"
    assert config.embedded_signup_config_id == "embedded-config-id"
    assert config.callback_url == (
        "https://manor.example/api/v1/channels/whatsapp/webhook"
    )
    assert "legacy" not in repr(config)


@pytest.mark.asyncio
async def test_nango_whatsapp_connection_fields_and_deployment_config(monkeypatch):
    from packages.core.services import channel_credentials as module

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "connection_id": "entity--user--whatsapp",
                "credentials": {
                    "access_token": "token-from-nango",
                    "phone_number_id": "phone-from-nango",
                },
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        "packages.core.ai.mcp.nango.get_nango_secret",
        lambda *_args, **_kwargs: _async_value("nango-secret"),
    )
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", "env-app-secret")
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "env-verify-token")

    resolved = await module.resolve_nango_integration_credentials(
        None,
        SimpleNamespace(
            entity_id="entity-1",
            provider="whatsapp",
            config={"nango": {"provider_config_key": "whatsapp"}},
        ),
        {
            "via": "nango",
            "connection_id": "entity--user--whatsapp",
            "provider_config_key": "whatsapp",
        },
    )

    assert resolved["access_token"] == "token-from-nango"
    assert resolved["phone_number_id"] == "phone-from-nango"
    assert resolved["app_secret"] == "env-app-secret"
    assert resolved["verify_token"] == "env-verify-token"


@pytest.mark.asyncio
async def test_nango_whatsapp_resolves_synced_phone_number_id(monkeypatch):
    from packages.core.services import channel_credentials as module

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"credentials": {"access_token": "token-from-nango"}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        "packages.core.ai.mcp.nango.get_nango_secret",
        lambda *_args, **_kwargs: _async_value("nango-secret"),
    )

    resolved = await module.resolve_nango_integration_credentials(
        None,
        SimpleNamespace(
            entity_id="entity-1",
            provider="whatsapp",
            config={
                "nango": {"provider_config_key": "whatsapp"},
                "whatsapp": {"phone_number_id": "phone-from-sync"},
            },
        ),
        {
            "via": "nango",
            "connection_id": "entity--user--whatsapp",
            "provider_config_key": "whatsapp",
        },
    )

    assert resolved["access_token"] == "token-from-nango"
    assert resolved["phone_number_id"] == "phone-from-sync"


@pytest.mark.asyncio
async def test_nango_whatsapp_missing_runtime_fields_is_rejected_clearly(monkeypatch):
    from packages.core.services.channels.whatsapp_adapter import WhatsAppChannelAdapter
    from packages.core.services.channels import whatsapp_adapter as module

    async def fake_lease(*_args, **_kwargs):
        return {
            "via": "nango",
            "connection_id": "entity--user--whatsapp",
            "provider_config_key": "whatsapp",
            "phone_number_id": "phone-from-nango",
            "access_token": "token",
        }

    monkeypatch.setattr(module, "lease_channel_config_credentials", fake_lease)
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CLIENT_ID", "meta-app-id")
    monkeypatch.delenv("NANGO_PROVIDER_WHATSAPP_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("NANGO_PROVIDER_WHATSAPP_CONFIG_ID", "embedded-config-id")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "env-verify-token")

    with pytest.raises(RuntimeError, match="app_secret"):
        await WhatsAppChannelAdapter()._build(
            _source_linked_config("whatsapp"),
            reason="channel.whatsapp.verify_inbound",
        )


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_wechat_adapter_uses_source_credentials_for_outbound(monkeypatch):
    from packages.core.services.channels.wechat_adapter import (
        WeChatAdapter,
        WeChatChannelAdapter,
    )

    _lease_only_from_source(monkeypatch, {
        "app_id": "wechat-source-app",
        "app_secret": "wechat-source-secret",
        "token": "wechat-webhook-token",
    })
    captured: dict[str, str] = {}

    async def fake_send_text(self, to: str, text: str) -> bool:
        captured.update(app_id=self.app_id, app_secret=self.app_secret, token=self.token)
        return True

    monkeypatch.setattr(WeChatAdapter, "send_text", fake_send_text)

    result = await WeChatChannelAdapter().send_text(
        _source_linked_config("wechat"), "openid-source", "hello",
    )

    assert result == {"status": "sent"}
    assert captured == {
        "app_id": "wechat-source-app",
        "app_secret": "wechat-source-secret",
        "token": "wechat-webhook-token",
    }


@pytest.mark.asyncio
async def test_wechat_adapter_uses_source_credentials_for_signature_verification(monkeypatch):
    from packages.core.services.channels.wechat_adapter import (
        WeChatAdapter,
        WeChatChannelAdapter,
    )

    _lease_only_from_source(monkeypatch, {"token": "wechat-webhook-token"})
    query = {"timestamp": "123", "nonce": "abc"}
    query["signature"] = WeChatAdapter._sign("wechat-webhook-token", "123", "abc")

    assert await WeChatChannelAdapter().verify_inbound(
        _source_linked_config("wechat"), headers={}, query=query, body=b"",
    )


@pytest.mark.asyncio
async def test_wechat_personal_adapter_uses_source_credentials_for_outbound(monkeypatch):
    from packages.core.services.channels import wechat_personal_adapter as module
    from packages.core.services.channels.wechat_personal_adapter import WeChatPersonalChannelAdapter

    _lease_only_from_source(monkeypatch, {
        "runner_url": "https://runner.example.test",
        "session_id": "session-source",
        "bearer_token": "runner-source-token",
        "default_target": "source-target",
    })
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        is_success = True

        def json(self):
            return {"msg_id": "source-message"}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=FakeClient))

    result = await WeChatPersonalChannelAdapter().send_text(
        _source_linked_config("wechat_personal"), "", "hello",
    )

    assert result["msg_id"] == "source-message"
    assert captured["url"] == "https://runner.example.test/sessions/session-source/messages"
    assert captured["headers"]["Authorization"] == "Bearer runner-source-token"
    assert captured["json"]["target"] == "source-target"


@pytest.mark.asyncio
async def test_discord_adapter_uses_deployment_bot_for_outbound(monkeypatch):
    from packages.core.services.channels import discord_adapter as module
    from packages.core.services.channels.base import ChannelAdapter
    from packages.core.services.channels.discord_adapter import DiscordChannelAdapter

    captured: dict = {}

    async def _unexpected_source_credentials(*_args, **_kwargs):
        raise AssertionError("Discord must not lease a per-connection Bot Token")

    monkeypatch.setattr(ChannelAdapter, "credentials", _unexpected_source_credentials)

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_resolve_app(_db):
        return SimpleNamespace(bot_token="deployment-discord-bot-token")

    monkeypatch.setattr(module, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(module, "resolve_discord_app_config", fake_resolve_app)

    class FakeResponse:
        is_success = True
        status_code = 200
        text = ""

        def json(self):
            return {"id": "discord-message"}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            captured.update(method="POST", url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=FakeClient))

    result = await DiscordChannelAdapter().send_text(
        _source_linked_config("discord"), "discord-channel", "hello",
    )

    assert result["message_id"] == "discord-message"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/channels/discord-channel/messages")
    assert captured["headers"]["Authorization"] == "Bot deployment-discord-bot-token"


@pytest.mark.asyncio
async def test_facebook_adapter_uses_source_credentials_for_signature_verification(monkeypatch):
    from packages.core.services.channels.facebook_adapter import FacebookChannelAdapter

    _lease_only_from_source(monkeypatch, {"app_secret": "facebook-source-secret"})

    assert not await FacebookChannelAdapter().verify_inbound(
        _source_linked_config("facebook"),
        headers={"X-Hub-Signature-256": "sha256=not-a-valid-signature"},
        query={},
        body=b"{}",
    )


async def _integration_channel_config(
    db_session,
    *,
    entity_id: str,
    integration_id: str,
    channel_type: str,
) -> ChannelConfig:
    return (await db_session.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.channel_type == channel_type,
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_id,
        )
    )).scalar_one()


@pytest.mark.asyncio
async def test_provider_webhook_handshakes_lease_source_credentials(
    client,
    db_session,
    monkeypatch,
):
    """Provider callbacks must work with an empty source-linked bridge row."""
    import packages.core.database as db_module
    from apps.api.routers.channels import facebook, wechat, whatsapp
    from apps.api.routers.integrations import _sync_channel_config_if_needed
    from packages.core.services.channels.wechat_adapter import WeChatAdapter

    monkeypatch.setattr(wechat, "async_session", db_module.async_session)
    monkeypatch.setattr(whatsapp, "async_session", db_module.async_session)
    monkeypatch.setattr(facebook, "async_session", db_module.async_session)

    headers = await _auth(client, "source_webhook_handshakes")
    current_user = await _current_user(client, headers)
    integrations = [
        ("wechat_official", "wechat", {
            "app_id": "wechat-app",
            "app_secret": "wechat-secret",
            "token": "wechat-verify-token",
        }),
        ("whatsapp", "whatsapp", {
            "phone_number_id": "whatsapp-phone",
            "access_token": "whatsapp-access-token",
            "verify_token": "whatsapp-verify-token",
            "app_secret": "whatsapp-app-secret",
        }),
        ("facebook", "facebook", {
            "page_id": "facebook-page",
            "access_token": "facebook-access-token",
            "verify_token": "facebook-verify-token",
            "app_secret": "facebook-app-secret",
        }),
    ]
    configs: dict[str, ChannelConfig] = {}
    for provider, channel_type, credentials in integrations:
        if provider == "whatsapp":
            integration = Integration(
                id=generate_ulid(),
                entity_id=current_user["entity_id"],
                owner_user_id=current_user["id"],
                created_by_user_id=current_user["id"],
                provider=provider,
                credentials={},
                status="active",
            )
            get_credential_service().store_integration(integration, credentials)
            db_session.add(integration)
            await db_session.flush()
            await _sync_channel_config_if_needed(
                db_session,
                entity_id=current_user["entity_id"],
                owner_user_id=current_user["id"],
                provider=provider,
                integration_id=integration.id,
                whatsapp_phone_number_id=credentials["phone_number_id"],
            )
            await db_session.commit()
            integration_id = integration.id
        else:
            response = await client.post(
                "/api/v1/integrations",
                headers=headers,
                json={"provider": provider, "credentials": credentials},
            )
            assert response.status_code == 201, response.text
            integration_id = response.json()["id"]
        config = await _integration_channel_config(
            db_session,
            entity_id=current_user["entity_id"],
            integration_id=integration_id,
            channel_type=channel_type,
        )
        assert config.credentials == {}
        configs[channel_type] = config

    wechat_signature = WeChatAdapter._sign("wechat-verify-token", "123", "nonce")
    wechat_response = await client.get(
        "/api/v1/channels/wechat/callback",
        params={
            "config_id": configs["wechat"].id,
            "signature": wechat_signature,
            "timestamp": "123",
            "nonce": "nonce",
            "echostr": "wechat-challenge",
        },
    )
    assert wechat_response.status_code == 200
    assert wechat_response.text == "wechat-challenge"

    whatsapp_response = await client.get(
        "/api/v1/channels/whatsapp/webhook",
        params={
            "config_id": configs["whatsapp"].id,
            "hub.mode": "subscribe",
            "hub.verify_token": "whatsapp-verify-token",
            "hub.challenge": "whatsapp-challenge",
        },
    )
    assert whatsapp_response.status_code == 200
    assert whatsapp_response.text == "whatsapp-challenge"

    facebook_response = await client.get(
        "/api/v1/channels/facebook/webhook",
        params={
            "config_id": configs["facebook"].id,
            "hub.mode": "subscribe",
            "hub.verify_token": "facebook-verify-token",
            "hub.challenge": "facebook-challenge",
        },
    )
    assert facebook_response.status_code == 200
    assert facebook_response.text == "facebook-challenge"


@pytest.mark.asyncio
async def test_legacy_channel_service_leases_source_credentials_for_twilio(monkeypatch):
    import packages.core.services.channel_service as channel_service

    source_config = _source_linked_config("twilio_sms")
    captured: dict[str, str] = {}

    async def source_credentials(config, *, reason: str) -> dict:
        assert config is source_config
        assert reason == "channel_service.twilio_sms.send"
        return {
            "account_sid": "AC-service-source",
            "auth_token": "service-source-token",
            "phone_number": "+14155550104",
        }

    class FakeMessages:
        def create(self, *, body: str, from_: str, to: str):
            captured.update(body=body, from_number=from_, to=to)
            return SimpleNamespace(
                sid="SM-service-source",
                price=None,
                price_unit=None,
            )

    class FakeTwilioClient:
        def __init__(self, account_sid: str, auth_token: str):
            captured.update(account_sid=account_sid, auth_token=auth_token)
            self.messages = FakeMessages()

    monkeypatch.setattr(
        channel_service,
        "lease_channel_config_credentials",
        source_credentials,
        raising=False,
    )
    monkeypatch.setattr(channel_service, "TwilioClient", FakeTwilioClient)

    result = await channel_service._send_sms(
        source_config,
        to="+14155550199",
        content="source service message",
    )

    assert result["external_id"] == "SM-service-source"
    assert captured == {
        "account_sid": "AC-service-source",
        "auth_token": "service-source-token",
        "body": "source service message",
        "from_number": "+14155550104",
        "to": "+14155550199",
    }


@pytest.mark.asyncio
async def test_legacy_channel_service_does_not_fallback_to_global_twilio_credentials(
    monkeypatch,
):
    import packages.core.services.channel_service as channel_service

    config = _source_linked_config("twilio_sms")
    config.config = {"phone_number": "+14155550104"}

    async def missing_source_credentials(config, *, reason: str) -> dict:
        assert config is not None
        assert reason == "channel_service.twilio_sms.send"
        return {}

    class UnexpectedTwilioClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("global Twilio credentials must not be used")

    monkeypatch.setattr(
        channel_service,
        "lease_channel_config_credentials",
        missing_source_credentials,
        raising=False,
    )
    monkeypatch.setattr(channel_service, "TwilioClient", UnexpectedTwilioClient)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-global")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "global-token")

    result = await channel_service._send_sms(
        config,
        to="+14155550199",
        content="must not use global credentials",
    )

    assert result == {"error": "Twilio credentials not configured"}


@pytest.mark.asyncio
async def test_legacy_channel_service_voice_does_not_fallback_to_global_twilio_credentials(
    monkeypatch,
):
    import packages.core.services.channel_service as channel_service

    config = _source_linked_config("twilio_voice")
    config.config = {"phone_number": "+14155550104"}

    async def missing_source_credentials(config, *, reason: str) -> dict:
        assert config is not None
        assert reason == "channel_service.twilio_voice.send"
        return {}

    class UnexpectedTwilioClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("global Twilio credentials must not be used")

    monkeypatch.setattr(
        channel_service,
        "lease_channel_config_credentials",
        missing_source_credentials,
        raising=False,
    )
    monkeypatch.setattr(channel_service, "TwilioClient", UnexpectedTwilioClient)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-global")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "global-token")

    result = await channel_service._send_voice_call(
        config,
        to="+14155550199",
        twiml_url="https://manor.example.test/twiml.xml",
    )

    assert result == {"error": "Twilio credentials not configured"}


@pytest.mark.asyncio
async def test_legacy_channel_service_uses_source_credentials_for_whatsapp(monkeypatch):
    import packages.core.services.channel_service as channel_service

    source_config = _source_linked_config("whatsapp")
    captured: dict = {}

    async def source_credentials(
        config, *, reason: str, resolve_nango: bool = False,
    ) -> dict:
        assert config is source_config
        assert reason == "channel_service.whatsapp.send"
        assert resolve_nango is True
        return {
            "phone_number_id": "whatsapp-source-phone",
            "access_token": "whatsapp-source-token",
        }

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"messages": [{"id": "wamid-source-message"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        channel_service,
        "lease_channel_config_credentials",
        source_credentials,
    )
    monkeypatch.setattr(channel_service, "httpx", SimpleNamespace(AsyncClient=FakeClient))

    result = await channel_service._send_whatsapp(
        source_config,
        to="15550001111",
        content="source WhatsApp message",
    )

    assert result["external_id"] == "wamid-source-message"
    assert captured["url"].endswith("/whatsapp-source-phone/messages")
    assert captured["headers"]["Authorization"] == "Bearer whatsapp-source-token"


@pytest.mark.asyncio
async def test_task_external_notification_leases_source_credentials(monkeypatch):
    import packages.core.services.task_external_notifications as notifications

    source_config = _source_linked_config("slack")
    source_config.provider = "slack_app"
    captured: dict[str, str] = {}

    async def source_credentials(config, *, reason: str) -> dict:
        assert config is source_config
        assert reason == "task_event.external_notification"
        return {
            "bot_token": "slack-source-token",
            "default_channel": "source-alerts",
        }

    async def fake_send_slack(token: str, target: str, text: str) -> bool:
        captured.update(token=token, target=target, text=text)
        return True

    monkeypatch.setattr(
        notifications,
        "lease_channel_config_credentials",
        source_credentials,
        raising=False,
    )
    monkeypatch.setattr(notifications, "_send_slack_bot_message", fake_send_slack)

    assert await notifications._send_channel_message(
        source_config,
        "source notification",
        {},
    )
    assert captured == {
        "token": "slack-source-token",
        "target": "source-alerts",
        "text": "source notification",
    }


@pytest.mark.asyncio
async def test_telegram_link_uses_source_credentials_for_bot_username(
    db_session,
    monkeypatch,
):
    from packages.core.services import notification_channel_linking as linking

    source_config = _source_linked_config("telegram")
    source_config.config = {}
    db_session.add(source_config)
    await db_session.commit()

    async def source_credentials(db, config, *, reason: str) -> dict:
        assert db is db_session
        assert config.id == source_config.id
        assert reason == "notification_channel_linking.telegram_bot_username"
        return {"bot_username": "SourceManorBot"}

    monkeypatch.setattr(
        linking,
        "lease_channel_credentials",
        source_credentials,
        raising=False,
    )

    result = await linking.start_link(
        db_session,
        user_id=source_config.owner_user_id,
        entity_id=source_config.entity_id,
        channel_type="telegram",
    )

    assert result.bot_username == "SourceManorBot"
    assert result.deep_link and "SourceManorBot" in result.deep_link


@pytest.mark.asyncio
async def test_integration_update_rejects_provider_changes(client, db_session):
    from packages.core.models.document import Integration

    headers = await _auth(client, "integration_provider_immutable")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "email",
            "credentials": {
                "smtp_host": "smtp.example.test",
                "username": "bot@example.test",
                "password": "email-source-secret",
            },
        },
    )
    assert create.status_code == 201, create.text
    integration_id = create.json()["id"]

    response = await client.put(
        f"/api/v1/integrations/{integration_id}",
        headers=headers,
        json={"provider": "discord"},
    )

    assert response.status_code == 422
    integration = await db_session.get(Integration, integration_id)
    assert integration.provider == "email"
    bridge = await _integration_channel_config(
        db_session,
        entity_id=create.json()["entity_id"],
        integration_id=integration_id,
        channel_type="email",
    )
    assert bridge.status == "active"


@pytest.mark.asyncio
async def test_source_linked_slack_channel_leases_its_owner_oauth_token(
    client,
    db_session,
):
    """A Slack ChannelConfig keeps a source reference, never a token copy."""
    from packages.core.services.channel_credentials import lease_channel_credentials

    owner_headers = await _auth(client, "slack_channel_source_owner")
    owner = await _current_user(client, owner_headers)
    account = OAuthAccount(
        id=generate_ulid(),
        user_id=owner["id"],
        provider="slack",
        provider_user_id="U-SOURCE-OWNER",
        profile={"team_name": "QA Slack"},
    )
    get_credential_service().store_oauth_account(
        account,
        {"access_token": "xoxb-owner-token"},
    )
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        credential_source_kind="oauth_account",
        credential_source_id=account.id,
        credentials={},
        status="active",
    )
    db_session.add_all([account, config])
    await db_session.flush()

    credentials = await lease_channel_credentials(
        db_session,
        config,
        reason="test.slack.send",
    )

    assert credentials == {"access_token": "xoxb-owner-token"}


@pytest.mark.asyncio
async def test_source_linked_channel_rejects_an_oauth_account_owned_by_someone_else(
    client,
    db_session,
):
    """A ChannelConfig cannot point at a token owned by a different user."""
    from packages.core.services.channel_credentials import lease_channel_credentials

    owner_headers = await _auth(client, "slack_channel_config_owner")
    other_headers = await _auth(client, "slack_channel_token_owner")
    owner = await _current_user(client, owner_headers)
    other = await _current_user(client, other_headers)
    assert owner["entity_id"] != other["entity_id"]

    account = OAuthAccount(
        id=generate_ulid(),
        user_id=other["id"],
        provider="slack",
        provider_user_id="U-OTHER-OWNER",
        profile={},
    )
    get_credential_service().store_oauth_account(
        account,
        {"access_token": "xoxb-other-token"},
    )
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        credential_source_kind="oauth_account",
        credential_source_id=account.id,
        credentials={},
        status="active",
    )
    db_session.add_all([account, config])
    await db_session.flush()

    with pytest.raises(ValueError, match="unavailable"):
        await lease_channel_credentials(
            db_session,
            config,
            reason="test.slack.send",
        )


@pytest.mark.asyncio
async def test_legacy_slack_config_id_callback_is_disabled(
    client,
    db_session,
    monkeypatch,
):
    """Slack events must use the fixed installation-routed endpoint."""
    owner_headers = await _auth(client, "slack_challenge_owner")
    owner = await _current_user(client, owner_headers)
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        credentials={"bot_token": "legacy-test-token"},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    body = json.dumps({"type": "url_verification", "challenge": "challenge"}).encode()
    path = f"/api/v1/channels/slack/callback?config_id={config.id}"
    rejected = await client.post(path, content=body)
    assert rejected.status_code == 410
    assert rejected.json()["detail"] == (
        "Slack events must use /api/v1/channels/slack/events"
    )


@pytest.mark.asyncio
async def test_slack_callback_rejects_a_channel_with_an_unavailable_oauth_source(
    client,
    db_session,
    monkeypatch,
):
    """A signed Slack event cannot revive an unlinked or deleted OAuth source."""
    owner_headers = await _auth(client, "slack_unavailable_source_owner")
    owner = await _current_user(client, owner_headers)
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        credential_source_kind="oauth_account",
        credential_source_id=generate_ulid(),
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    body = json.dumps({"type": "url_verification", "challenge": "challenge"}).encode()
    response = await client.post(
        f"/api/v1/channels/slack/callback?config_id={config.id}",
        content=body,
    )
    assert response.status_code == 410
