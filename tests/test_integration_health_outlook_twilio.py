"""Focused health contracts for the Outlook and Twilio integrations."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import packages.core.services.integration_health as health_mod
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Integration
from packages.core.models.workspace import AgentSubscription


class _Response:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self.text = "" if body is None else "{}"
        self._body = body or {}

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_outlook_health_checks_graph_me_with_bearer(monkeypatch):
    calls: list[dict] = []

    async def fake_http_get(url, *, headers=None, timeout=10):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _Response(200, {"id": "outlook-user"})

    monkeypatch.setattr(health_mod, "_http_get", fake_http_get)

    result = await health_mod.test_outlook({"access_token": "graph-token"})

    assert result["ok"] is True
    assert calls == [{
        "url": "https://graph.microsoft.com/v1.0/me",
        "headers": {"Authorization": "Bearer graph-token"},
        "timeout": 10,
    }]


@pytest.mark.asyncio
async def test_twilio_health_uses_one_authenticated_request(monkeypatch):
    calls: list[dict] = []

    async def unexpected_unauthenticated_probe(*_args, **_kwargs):
        raise AssertionError("Twilio health must not probe without authentication")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, *, auth=None):
            calls.append({"url": url, "auth": auth})
            return _Response(200, {"sid": "AC-test"})

    monkeypatch.setattr(health_mod, "_http_get", unexpected_unauthenticated_probe)
    monkeypatch.setattr(health_mod.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = await health_mod.test_twilio({
        "account_sid": "AC-test",
        "auth_token": "twilio-token",
    })

    assert result["ok"] is True
    assert calls == [{
        "url": "https://api.twilio.com/2010-04-01/Accounts/AC-test.json",
        "auth": ("AC-test", "twilio-token"),
    }]


@pytest.mark.asyncio
async def test_twilio_health_keeps_credentials_ok_when_voice_wiring_is_incomplete(
    monkeypatch,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url, *, auth=None):
            return _Response(200, {"sid": "AC-test"})

    monkeypatch.setattr(health_mod.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = await health_mod.test_twilio(
        {"account_sid": "AC-test", "auth_token": "twilio-token"},
        wiring_ctx={
            "public_base_url": "http://localhost:8000",
            "channel_config_id": None,
            "agent_bound": False,
            "realtime_configured": False,
            "realtime_error": (
                "OpenRouter can answer the bound Agent but does not provide "
                "this Realtime WebSocket transport"
            ),
            "expected_url": "",
        },
    )

    assert result["ok"] is True
    assert result["wiring"]["ok"] is False
    assert "PUBLIC_BASE_URL" in result["wiring"]["detail"]
    assert "Voice channel" in result["wiring"]["detail"]
    assert "Agent" in result["wiring"]["detail"]
    assert "OpenRouter can answer the bound Agent" in result["wiring"]["detail"]
    assert "STT" not in result["wiring"]["detail"]
    assert "TTS" not in result["wiring"]["detail"]


@pytest.mark.asyncio
async def test_twilio_wiring_context_uses_voice_channel_agent_and_runtime_routes(
    db_session,
    monkeypatch,
):
    integration = Integration(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        provider="twilio",
        status="active",
        config={},
        credentials={},
    )
    voice_config = ChannelConfig(
        id=generate_ulid(),
        entity_id=integration.entity_id,
        owner_user_id=integration.owner_user_id,
        credential_source_kind="integration",
        credential_source_id=integration.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
        config={},
        credentials={},
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=integration.entity_id,
        agent_id="manor-master",
        workspace_id=generate_ulid(),
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=integration.entity_id,
        user_id=integration.owner_user_id,
        type="twilio_voice",
        workspace_id=subscription.workspace_id,
        agent_subscription_id=subscription.id,
        status="active",
        config={"channel_config_id": voice_config.id},
    )
    db_session.add_all([integration, voice_config, subscription, binding])
    await db_session.commit()

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )
    async def resolve_realtime(_entity_id, *, user_id):
        assert user_id == integration.owner_user_id
        return SimpleNamespace(
            api_key="realtime-test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        )

    monkeypatch.setattr(
        "packages.core.services.voice.realtime.resolve_realtime_route",
        resolve_realtime,
    )

    context = await health_mod._wiring_ctx_for_integration(db_session, integration)

    assert context == {
        "public_base_url": "https://manor.example",
        "channel_config_id": voice_config.id,
        "agent_bound": True,
        "binding_state": "ready",
        "realtime_configured": True,
        "expected_url": (
            "https://manor.example/api/v1/channels/twilio/voice"
            f"?config_id={voice_config.id}"
        ),
    }

    async def unavailable_realtime(_entity_id, *, user_id):
        assert user_id == integration.owner_user_id
        raise RuntimeError(
            "OpenRouter can answer the bound Agent but does not provide "
            "this Realtime WebSocket transport"
        )

    monkeypatch.setattr(
        "packages.core.services.voice.realtime.resolve_realtime_route",
        unavailable_realtime,
    )

    unavailable = await health_mod._wiring_ctx_for_integration(
        db_session,
        integration,
    )

    assert unavailable["realtime_configured"] is False
    assert unavailable["realtime_error"] == (
        "OpenRouter can answer the bound Agent but does not provide "
        "this Realtime WebSocket transport"
    )


@pytest.mark.asyncio
async def test_twilio_wiring_context_recognizes_agent_bound_via_upsert(
    db_session,
    monkeypatch,
):
    from packages.core.services.integration_service import upsert_channel_binding

    integration = Integration(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        provider="twilio",
        status="active",
        config={},
        credentials={},
    )
    voice_config = ChannelConfig(
        id=generate_ulid(),
        entity_id=integration.entity_id,
        owner_user_id=integration.owner_user_id,
        credential_source_kind="integration",
        credential_source_id=integration.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
        config={},
        credentials={},
    )
    db_session.add_all([integration, voice_config])
    await db_session.commit()

    binding = await upsert_channel_binding(
        db_session,
        entity_id=integration.entity_id,
        user_id=integration.owner_user_id,
        channel_config_id=voice_config.id,
        agent_id="manor-master",
    )

    assert binding.user_id == integration.owner_user_id
    binding.user_id = None
    await db_session.commit()

    rebound = await upsert_channel_binding(
        db_session,
        entity_id=integration.entity_id,
        user_id=integration.owner_user_id,
        channel_config_id=voice_config.id,
        agent_id="manor-master",
    )

    assert rebound.user_id == integration.owner_user_id

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )

    async def resolve_realtime(_entity_id, *, user_id):
        assert user_id == integration.owner_user_id
        return SimpleNamespace(
            api_key="realtime-test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        )

    monkeypatch.setattr(
        "packages.core.services.voice.realtime.resolve_realtime_route",
        resolve_realtime,
    )

    context = await health_mod._wiring_ctx_for_integration(db_session, integration)

    assert context["agent_bound"] is True
    assert context["binding_state"] == "ready"


@pytest.mark.asyncio
async def test_twilio_wiring_context_reports_shared_binding_as_ambiguous(
    db_session,
    monkeypatch,
):
    integration = Integration(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        owner_user_id=generate_ulid(),
        provider="twilio",
        status="active",
        config={},
        credentials={},
    )
    voice_config = ChannelConfig(
        id=generate_ulid(),
        entity_id=integration.entity_id,
        owner_user_id=integration.owner_user_id,
        credential_source_kind="integration",
        credential_source_id=integration.id,
        channel_type="twilio_voice",
        provider="twilio",
        status="active",
        config={},
        credentials={},
    )
    subscriptions = [
        AgentSubscription(
            id=generate_ulid(),
            entity_id=integration.entity_id,
            agent_id=generate_ulid(),
            workspace_id=generate_ulid(),
            status="active",
        )
        for _ in range(2)
    ]
    bindings = [
        Channel(
            id=generate_ulid(),
            entity_id=integration.entity_id,
            user_id=integration.owner_user_id,
            workspace_id=sub.workspace_id,
            type="twilio_voice",
            agent_subscription_id=sub.id,
            status="active",
            config={"channel_config_id": voice_config.id},
        )
        for sub in subscriptions
    ]
    db_session.add_all([integration, voice_config, *subscriptions, *bindings])
    await db_session.commit()
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )
    async def resolve_realtime(_entity_id, *, user_id):
        assert user_id == integration.owner_user_id
        return SimpleNamespace(
            api_key="realtime-test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        )

    monkeypatch.setattr(
        "packages.core.services.voice.realtime.resolve_realtime_route",
        resolve_realtime,
    )

    context = await health_mod._wiring_ctx_for_integration(db_session, integration)

    assert context["agent_bound"] is False
    assert context["binding_state"] == "ambiguous"
    assert context["realtime_configured"] is True
    assert "stt_configured" not in context
    assert "tts_configured" not in context
