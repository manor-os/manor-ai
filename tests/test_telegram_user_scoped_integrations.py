"""Regression coverage for user-scoped Telegram bot connections."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Integration
from tests.test_document_permissions import _auth


async def _me(client, headers: dict) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_telegram_bridge_keeps_credentials_only_on_user_integration(
    client,
    db_session,
    monkeypatch,
):
    from packages.core.services.channels.telegram_adapter import TelegramAdapter

    async def fake_get_me(self):
        return {"id": 123456789, "is_bot": True, "username": "manor_test_bot"}

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    headers = await _auth(client, "telegram_source_owner")

    response = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "telegram", "credentials": {"bot_token": "123456789:test-token"}},
    )

    assert response.status_code == 201, response.text
    integration_id = response.json()["id"]
    integration = await db_session.get(Integration, integration_id)
    channel_config = (await db_session.execute(
        select(ChannelConfig).where(
            ChannelConfig.credential_source_kind == "integration",
            ChannelConfig.credential_source_id == integration_id,
        )
    )).scalar_one()

    assert channel_config.credentials == {}
    assert channel_config.credential_ref is None
    assert channel_config.telegram_bot_id == "123456789"
    credentials = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="test", id="telegram_source_owner"),
        reason="assert_telegram_source_credentials",
    )
    assert credentials["bot_token"] == "123456789:test-token"
    assert credentials["secret_token"]


@pytest.mark.asyncio
async def test_second_user_cannot_take_over_the_same_telegram_bot(
    client,
    monkeypatch,
):
    from packages.core.services.channels.telegram_adapter import TelegramAdapter

    async def fake_get_me(self):
        return {"id": 99887766, "is_bot": True, "username": "exclusive_bot"}

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    first_headers = await _auth(client, "telegram_first_owner")
    second_headers = await _auth(client, "telegram_second_owner")
    payload = {"provider": "telegram", "credentials": {"bot_token": "99887766:shared-token"}}

    first = await client.post("/api/v1/integrations", headers=first_headers, json=payload)
    second = await client.post("/api/v1/integrations", headers=second_headers, json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert "already connected" in second.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rejected_telegram_bot_change_keeps_the_current_webhook(
    client,
    monkeypatch,
):
    from packages.core.services.channels.telegram_adapter import (
        TelegramAdapter,
        TelegramChannelAdapter,
    )

    async def fake_get_me(self):
        bot_id = {"101010:owner-token": 101010, "202020:other-token": 202020}[self.bot_token]
        return {"id": bot_id, "is_bot": True}

    unregistered: list[str] = []

    async def fake_unregister(self, channel_config, **_kwargs):
        unregistered.append(channel_config.id)
        return {"unregistered": True}

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    monkeypatch.setattr(TelegramChannelAdapter, "unregister_webhook", fake_unregister)
    owner_headers = await _auth(client, "telegram_change_owner")
    other_headers = await _auth(client, "telegram_change_other_owner")

    owner = await client.post(
        "/api/v1/integrations",
        headers=owner_headers,
        json={"provider": "telegram", "credentials": {"bot_token": "101010:owner-token"}},
    )
    other = await client.post(
        "/api/v1/integrations",
        headers=other_headers,
        json={"provider": "telegram", "credentials": {"bot_token": "202020:other-token"}},
    )

    assert owner.status_code == 201, owner.text
    assert other.status_code == 201, other.text

    rejected = await client.put(
        f"/api/v1/integrations/{owner.json()['id']}",
        headers=owner_headers,
        json={"credentials": {"bot_token": "202020:other-token"}},
    )

    assert rejected.status_code == 409
    assert unregistered == []


@pytest.mark.asyncio
async def test_telegram_webhook_rejects_request_without_its_secret_header(
    client,
    db_session,
):
    headers = await _auth(client, "telegram_webhook_owner")
    user = await _me(client, headers)
    integration = Integration(
        id=generate_ulid(),
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        created_by_user_id=user["id"],
        provider="telegram",
        credentials={},
    )
    get_credential_service().store_integration(
        integration,
        {"bot_token": "445566:inbound-token", "secret_token": "telegram-webhook-secret"},
    )
    channel_config = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        channel_type="telegram",
        provider="telegram_bot",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        credentials={},
        status="active",
    )
    db_session.add_all([integration, channel_config])
    await db_session.commit()
    token_hash = hashlib.sha256(b"445566:inbound-token").hexdigest()

    response = await client.post(
        f"/api/v1/channels/telegram/webhook/{token_hash}?config_id={channel_config.id}",
        json={"update_id": 1},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Telegram webhook secret"


@pytest.mark.asyncio
async def test_legacy_telegram_webhook_without_secret_remains_available_after_upgrade(
    client,
    db_session,
    monkeypatch,
):
    from apps.api.routers.channels import telegram as telegram_router
    from packages.core.services.channels.telegram_adapter import TelegramAdapter

    headers = await _auth(client, "telegram_legacy_webhook_owner")
    user = await _me(client, headers)
    channel_config = ChannelConfig(
        entity_id=user["entity_id"],
        channel_type="telegram",
        provider="telegram_bot",
        credentials={"bot_token": "332211:legacy-token"},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.commit()

    async def fake_handle_update(self, _update):
        return {
            "sender_id": "legacy-sender",
            "sender_name": "Legacy Sender",
            "chat_id": "legacy-chat",
            "content": "hello",
            "message_type": "text",
            "msg_id": "legacy-message",
        }

    async def fake_handle_inbound_message(*_args, **_kwargs):
        return None

    monkeypatch.setattr(TelegramAdapter, "handle_update", fake_handle_update)
    monkeypatch.setattr(telegram_router, "handle_inbound_message", fake_handle_inbound_message)
    monkeypatch.setattr(telegram_router.dispatch_inbound_task, "delay", lambda **_kwargs: None)
    token_hash = hashlib.sha256(b"332211:legacy-token").hexdigest()

    response = await client.post(
        f"/api/v1/channels/telegram/webhook/{token_hash}?config_id={channel_config.id}",
        json={"update_id": 1},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_hardened_telegram_webhook_without_secret_is_rejected(
    client,
    db_session,
):
    headers = await _auth(client, "telegram_hardened_webhook_owner")
    user = await _me(client, headers)
    channel_config = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        channel_type="telegram",
        provider="telegram_bot",
        telegram_bot_id="667788",
        credentials={"bot_token": "667788:hardened-token"},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.commit()
    token_hash = hashlib.sha256(b"667788:hardened-token").hexdigest()

    response = await client.post(
        f"/api/v1/channels/telegram/webhook/{token_hash}?config_id={channel_config.id}",
        json={"update_id": 1},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Telegram webhook secret"


@pytest.mark.asyncio
async def test_updating_telegram_credentials_unregisters_the_old_webhook(
    client,
    monkeypatch,
):
    from packages.core.services.channels.telegram_adapter import (
        TelegramAdapter,
        TelegramChannelAdapter,
    )

    async def fake_get_me(self):
        return {"id": 772299, "is_bot": True}

    unregistered: list[str] = []

    async def fake_unregister(self, channel_config, **_kwargs):
        unregistered.append(channel_config.id)
        return {"unregistered": True}

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    monkeypatch.setattr(TelegramChannelAdapter, "unregister_webhook", fake_unregister)
    headers = await _auth(client, "telegram_rotate_owner")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "telegram", "credentials": {"bot_token": "772299:old-token"}},
    )
    assert create.status_code == 201, create.text

    update = await client.put(
        f"/api/v1/integrations/{create.json()['id']}",
        headers=headers,
        json={"credentials": {"bot_token": "772299:new-token"}},
    )

    assert update.status_code == 200, update.text
    assert len(unregistered) == 1


@pytest.mark.asyncio
async def test_telegram_update_does_not_unregister_before_source_persistence(
    client,
    monkeypatch,
):
    from apps.api.routers import integrations as integrations_router
    from packages.core.credentials import CredentialError
    from packages.core.services.channels.telegram_adapter import (
        TelegramAdapter,
        TelegramChannelAdapter,
    )

    async def fake_get_me(self):
        return {"id": 884422, "is_bot": True}

    unregistered: list[str] = []

    async def fake_unregister(self, channel_config):
        unregistered.append(channel_config.id)
        return {"unregistered": True}

    async def fail_source_persistence(*args, **kwargs):
        raise CredentialError("credential backend unavailable")

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    monkeypatch.setattr(TelegramChannelAdapter, "unregister_webhook", fake_unregister)
    headers = await _auth(client, "telegram_update_persistence_owner")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "telegram", "credentials": {"bot_token": "884422:old-token"}},
    )
    assert create.status_code == 201, create.text

    monkeypatch.setattr(integrations_router, "update_integration", fail_source_persistence)
    update = await client.put(
        f"/api/v1/integrations/{create.json()['id']}",
        headers=headers,
        json={"credentials": {"bot_token": "884422:new-token"}},
    )

    assert update.status_code >= 400
    assert unregistered == []


@pytest.mark.asyncio
async def test_telegram_update_reports_stale_credential_conflict(
    client,
    monkeypatch,
):
    from apps.api.routers import integrations as integrations_router
    from packages.core.services.channels.telegram_adapter import TelegramAdapter
    from packages.core.services.integration_service import (
        IntegrationCredentialConflictError,
    )

    async def fake_get_me(self):
        return {"id": 884433, "is_bot": True}

    expected_snapshots: list[dict | None] = []

    async def conflict_update(*args, **kwargs):
        expected_snapshots.append(kwargs.get("expected_credentials"))
        raise IntegrationCredentialConflictError("stale")

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    headers = await _auth(client, "telegram_update_conflict_owner")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "telegram", "credentials": {"bot_token": "884433:old-token"}},
    )
    assert create.status_code == 201, create.text

    monkeypatch.setattr(integrations_router, "update_integration", conflict_update)
    update = await client.put(
        f"/api/v1/integrations/{create.json()['id']}",
        headers=headers,
        json={"credentials": {"bot_token": "884433:new-token"}},
    )

    assert update.status_code == 409
    assert "reload and retry" in update.json()["detail"]
    assert expected_snapshots[0]["bot_token"] == "884433:old-token"


@pytest.mark.asyncio
async def test_deleting_telegram_integration_unregisters_its_webhook(
    client,
    monkeypatch,
):
    from packages.core.services.channels.telegram_adapter import (
        TelegramAdapter,
        TelegramChannelAdapter,
    )

    async def fake_get_me(self):
        return {"id": 555777, "is_bot": True}

    unregistered: list[str] = []

    async def fake_unregister(self, channel_config):
        unregistered.append(channel_config.id)
        return {"unregistered": True}

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    monkeypatch.setattr(TelegramChannelAdapter, "unregister_webhook", fake_unregister)
    headers = await _auth(client, "telegram_delete_owner")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "telegram", "credentials": {"bot_token": "555777:delete-token"}},
    )
    assert create.status_code == 201, create.text

    deleted = await client.delete(f"/api/v1/integrations/{create.json()['id']}", headers=headers)

    assert deleted.status_code == 204, deleted.text
    assert len(unregistered) == 1


@pytest.mark.asyncio
async def test_telegram_adapter_leases_the_user_integration_for_outbound_messages(
    client,
    db_session,
    monkeypatch,
):
    from packages.core.services.channels.telegram_adapter import (
        TelegramAdapter,
        TelegramChannelAdapter,
    )

    headers = await _auth(client, "telegram_outbound_source_owner")
    user = await _me(client, headers)
    integration = Integration(
        id=generate_ulid(),
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        created_by_user_id=user["id"],
        provider="telegram",
        credentials={},
    )
    get_credential_service().store_integration(
        integration,
        {"bot_token": "778899:outbound-token", "secret_token": "outbound-secret"},
    )
    channel_config = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        channel_type="telegram",
        provider="telegram_bot",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        credentials={},
        status="active",
    )
    db_session.add_all([integration, channel_config])
    await db_session.commit()

    sent: list[tuple[str, str, str]] = []

    async def fake_send_message(self, chat_id, text, **_kwargs):
        sent.append((self.bot_token, str(chat_id), text))
        return {"status": "sent"}

    monkeypatch.setattr(TelegramAdapter, "send_message", fake_send_message)
    result = await TelegramChannelAdapter().send_text(channel_config, "42", "Hello")

    assert result == {"status": "sent"}
    assert sent == [("778899:outbound-token", "42", "Hello")]


@pytest.mark.asyncio
async def test_telegram_http_200_api_rejection_is_determinate(monkeypatch):
    from packages.core.services.channels import telegram_adapter
    from packages.core.services.channels.base import (
        ChannelTextSendError,
        ChannelTextSendFailureDisposition,
    )
    from packages.core.services.channels.telegram_adapter import TelegramAdapter

    class Response:
        status_code = 200
        text = "bot was blocked by the user"

        def json(self):
            return {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            }

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        telegram_adapter,
        "httpx",
        type("Httpx", (), {"AsyncClient": Client}),
    )

    with pytest.raises(ChannelTextSendError) as exc_info:
        await TelegramAdapter("test-token").send_message("42", "hello")

    assert exc_info.value.disposition is ChannelTextSendFailureDisposition.DETERMINATE


@pytest.mark.asyncio
async def test_telegram_health_wiring_uses_the_source_linked_channel_config(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.config as config_module
    from packages.core.services.integration_health import _wiring_ctx_for_integration

    headers = await _auth(client, "telegram_health_source_owner")
    user = await _me(client, headers)
    integration = Integration(
        id=generate_ulid(),
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        created_by_user_id=user["id"],
        provider="telegram",
        credentials={},
    )
    get_credential_service().store_integration(
        integration,
        {"bot_token": "112233:health-token", "secret_token": "health-secret"},
    )
    channel_config = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        channel_type="telegram",
        provider="telegram_bot",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        credentials={},
        status="active",
    )
    db_session.add_all([integration, channel_config])
    await db_session.commit()
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.test"),
    )

    wiring = await _wiring_ctx_for_integration(db_session, integration)

    expected_hash = hashlib.sha256(b"112233:health-token").hexdigest()
    assert wiring == {
        "expected_url": (
            f"https://manor.test/api/v1/channels/telegram/webhook/{expected_hash}"
            f"?config_id={channel_config.id}"
        ),
        "channel_config_id": channel_config.id,
    }


@pytest.mark.asyncio
async def test_manual_telegram_webhook_registration_repairs_legacy_source_security(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.services.channels.telegram_adapter as telegram_module
    from packages.core.services.channels.telegram_adapter import TelegramAdapter

    headers = await _auth(client, "telegram_legacy_repair_owner")
    user = await _me(client, headers)
    integration = Integration(
        id=generate_ulid(),
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        created_by_user_id=user["id"],
        provider="telegram",
        credentials={},
    )
    get_credential_service().store_integration(
        integration,
        {"bot_token": "221144:legacy-token"},
    )
    channel_config = ChannelConfig(
        entity_id=user["entity_id"],
        owner_user_id=user["id"],
        channel_type="telegram",
        provider="telegram_bot",
        credential_source_kind="integration",
        credential_source_id=integration.id,
        credentials={},
        status="active",
    )
    db_session.add_all([integration, channel_config])
    await db_session.commit()

    async def fake_get_me(self):
        return {"id": 221144, "is_bot": True}

    registered: list[str] = []

    async def fake_set_webhook(self, _url, secret_token=None):
        registered.append(secret_token)
        return True

    monkeypatch.setattr(TelegramAdapter, "get_me", fake_get_me)
    monkeypatch.setattr(TelegramAdapter, "set_webhook", fake_set_webhook)
    monkeypatch.setattr(
        telegram_module,
        "_get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.test"),
    )

    response = await client.post(
        f"/api/v1/integrations/wiring/entity-accounts/{integration.id}/register",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    refreshed_integration = await db_session.get(Integration, integration.id)
    refreshed_config = await db_session.get(ChannelConfig, channel_config.id)
    await db_session.refresh(refreshed_integration)
    await db_session.refresh(refreshed_config)
    credentials = get_credential_service().lease_integration(
        refreshed_integration,
        requester=Requester(kind="test", id="telegram_legacy_repair_owner"),
        reason="assert_legacy_telegram_repair",
    )
    assert credentials["secret_token"]
    assert registered == [credentials["secret_token"]]
    assert refreshed_config.telegram_bot_id == "221144"
