"""Outlook Mail Graph webhook and channel adapter contract tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog


async def _auth(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _outlook_config(*, entity_id: str = "outlook-entity") -> SimpleNamespace:
    return SimpleNamespace(
        id="outlook-config",
        entity_id=entity_id,
        owner_user_id="outlook-owner",
        channel_type="outlook",
        config={
            "outlook_subscription_id": "subscription-1",
            "outlook_client_state": "client-state-1",
            "outlook_user_id": "self-user",
        },
    )


def _notification(
    *,
    message_id: str = "message-1",
    client_state: str = "client-state-1",
    sender: str = "sender@example.com",
    conversation_id: str = "conversation-1",
    body: str = "Hello Manor",
) -> dict:
    return {
        "subscriptionId": "subscription-1",
        "clientState": client_state,
        "changeType": "created",
        "resource": f"users/self-user/mailFolders('inbox')/messages/{message_id}",
        "resourceData": {
            "id": message_id,
            "conversationId": conversation_id,
            "from": {"emailAddress": {"address": sender, "name": "Sender"}},
            "subject": "Question",
            "body": {"contentType": "html", "content": f"<p>{body}</p>"},
        },
    }


def test_outlook_adapter_parses_graph_message_and_preserves_thread_target():
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    parsed = OutlookChannelAdapter().parse_notification(
        _outlook_config(), _notification()
    )

    assert parsed is not None
    assert parsed.source_id == "sender@example.com"
    assert parsed.sender_name == "Sender"
    assert parsed.reply_to == "message-1"
    assert parsed.content == "Hello Manor"
    assert parsed.external_message_id == "message-1"
    assert parsed.raw["conversationId"] == "conversation-1"


def test_outlook_adapter_rejects_wrong_state_and_connected_user_echo():
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    adapter = OutlookChannelAdapter()
    assert adapter.parse_notification(
        _outlook_config(), _notification(client_state="wrong")
    ) is None
    assert adapter.parse_notification(
        _outlook_config(), _notification(sender="self-user")
    ) is None


@pytest.mark.asyncio
async def test_outlook_adapter_hydrates_graph_resource_without_body(monkeypatch):
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    adapter = OutlookChannelAdapter()
    notification = _notification()
    notification["resourceData"] = {"id": "message-1"}

    async def fake_request(_cc, _credentials, method, path, *, body=None):
        assert method == "GET"
        assert path.endswith("message-1")
        return _notification()["resourceData"]

    async def fake_credentials(_cc, *, reason):
        assert reason == "channel.outlook.hydrate_notification"
        return {"access_token": "graph-token"}

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(adapter, "credentials", fake_credentials)
    hydrated = await adapter.hydrate_notification(_outlook_config(), notification)

    assert hydrated["resourceData"]["body"]["content"] == "<p>Hello Manor</p>"


@pytest.mark.asyncio
async def test_outlook_adapter_registers_and_unregisters_graph_subscription(monkeypatch):
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )
    adapter = OutlookChannelAdapter()
    config = _outlook_config()
    config.config.pop("outlook_subscription_id")
    requests: list[dict] = []

    async def fake_credentials(_cc, *, reason):
        return {"access_token": "graph-token"}

    async def fake_request(_cc, _credentials, method, path, *, body=None):
        requests.append({"method": method, "path": path, "body": body})
        if method == "POST":
            return {"id": "subscription-new", "expirationDateTime": "2026-08-28T00:00:00Z"}
        return {}

    monkeypatch.setattr(adapter, "credentials", fake_credentials)
    monkeypatch.setattr(adapter, "_request", fake_request)

    registered = await adapter.register_webhook(config)
    assert registered["registered"] is True
    assert config.config["outlook_subscription_id"] == "subscription-new"
    assert "config_id=outlook-config" in requests[0]["body"]["notificationUrl"]
    assert requests[0]["body"]["resource"].endswith("mailFolders('inbox')/messages")

    unregistered = await adapter.unregister_webhook(config)
    assert unregistered["unregistered"] is True
    assert config.config["outlook_subscription_id"] is None
    assert requests[-1]["method"] == "DELETE"


@pytest.mark.asyncio
async def test_outlook_registration_recreates_missing_remote_subscription(monkeypatch):
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )
    adapter = OutlookChannelAdapter()
    config = _outlook_config()
    requests: list[dict] = []

    async def fake_credentials(_cc, *, reason):
        return {"access_token": "graph-token"}

    async def fake_request(_cc, _credentials, method, path, *, body=None):
        requests.append({"method": method, "path": path, "body": body})
        if method == "PATCH":
            raise RuntimeError("Microsoft Graph error 404: subscription not found")
        return {"id": "subscription-recreated"}

    monkeypatch.setattr(adapter, "credentials", fake_credentials)
    monkeypatch.setattr(adapter, "_request", fake_request)

    result = await adapter.register_webhook(config)

    assert result["subscription_id"] == "subscription-recreated"
    assert [request["method"] for request in requests] == ["PATCH", "POST"]
    assert config.config["outlook_subscription_id"] == "subscription-recreated"


def test_outlook_subscription_renewal_is_scheduled():
    from packages.core.celery_app import celery_app

    assert celery_app.conf.beat_schedule[
        "outlook-subscription-renewal"
    ]["task"] == "channel.outlook_subscription_tick"


@pytest.mark.asyncio
async def test_outlook_registration_failure_is_reported_and_keeps_pending(monkeypatch):
    from apps.api.routers import integrations

    config = SimpleNamespace(
        id="outlook-config",
        channel_type="outlook",
        config={"outlook_registration_pending": False},
    )

    class DB:
        async def commit(self):
            return None

    class Adapter:
        async def register_webhook(self, _config):
            raise RuntimeError("Graph unavailable")

    async def configs(*_args, **_kwargs):
        return [config]

    monkeypatch.setattr(integrations, "_channel_configs_for_integration", configs)
    monkeypatch.setattr(
        "packages.core.services.channels.get_adapter", lambda _type: Adapter(),
    )

    failures = await integrations._register_integration_channel_webhooks(
        DB(), entity_id="entity-1", integration_id="integration-1",
    )

    assert failures == ["outlook-config"]
    assert config.config["outlook_registration_pending"] is True


@pytest.mark.asyncio
async def test_outlook_integration_materializes_source_linked_channel_config(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "outlook_bridge")
    response = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "outlook",
            "credentials": {"access_token": "graph-token"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == body["entity_id"],
                ChannelConfig.channel_type == "outlook",
                ChannelConfig.credential_source_id == body["id"],
            )
        )
    ).scalar_one()
    assert config.provider == "microsoft_graph"
    assert config.credentials == {}
    assert config.credential_source_kind == "integration"


@pytest.mark.asyncio
async def test_outlook_webhook_validation_dedup_and_threaded_dispatch(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import outlook as outlook_router

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="outlook-webhook-entity",
        owner_user_id="outlook-owner",
        channel_type="outlook",
        provider="microsoft_graph",
        config={
            "outlook_subscription_id": "subscription-1",
            "outlook_client_state": "client-state-1",
            "outlook_user_id": "self-user",
        },
        credentials={"access_token": "graph-token"},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(outlook_router, "async_session", db_module.async_session)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        outlook_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )

    validation = await client.post(
        f"/api/v1/channels/outlook/notifications?config_id={config.id}&validationToken=validate-me"
    )
    assert validation.status_code == 200
    assert validation.text == "validate-me"

    payload = {"value": [_notification()]}
    first = await client.post(
        f"/api/v1/channels/outlook/notifications?config_id={config.id}",
        json=payload,
    )
    duplicate = await client.post(
        f"/api/v1/channels/outlook/notifications?config_id={config.id}",
        json=payload,
    )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert len(dispatched) == 1
    assert dispatched[0]["chat_id"] == "message-1"
    assert dispatched[0]["channel_type"] == "outlook"
    assert dispatched[0]["inbound_message_log_id"]
    rows = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config.id,
                MessageLog.direction == "inbound",
                MessageLog.external_id == "message-1",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_outlook_webhook_ignores_invalid_client_state(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import outlook as outlook_router
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="outlook-invalid-state",
        owner_user_id="outlook-owner",
        channel_type="outlook",
        provider="microsoft_graph",
        config={
            "outlook_subscription_id": "subscription-1",
            "outlook_client_state": "client-state-1",
        },
        credentials={"access_token": "graph-token"},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(outlook_router, "async_session", db_module.async_session)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        outlook_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )

    async def unexpected_hydration(*_args, **_kwargs):
        raise AssertionError("invalid clientState must be rejected before hydration")

    monkeypatch.setattr(OutlookChannelAdapter, "_request", unexpected_hydration)

    invalid = _notification(client_state="attacker-state")
    invalid["resourceData"] = {"id": "message-1"}
    response = await client.post(
        f"/api/v1/channels/outlook/notifications?config_id={config.id}",
        json={"value": [invalid]},
    )

    assert response.status_code == 202
    assert dispatched == []


@pytest.mark.asyncio
async def test_outlook_webhook_claims_received_receipt_before_dispatch(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    """A duplicate arriving before the first enqueue completes must not be
    dispatched twice. The claim has to happen before the broker call; this
    test disables the legacy post-enqueue marker to exercise that boundary.
    """
    import packages.core.database as db_module
    from apps.api.routers.channels import outlook as outlook_router

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="outlook-claim-entity",
        owner_user_id="outlook-owner",
        channel_type="outlook",
        provider="microsoft_graph",
        config={
            "outlook_subscription_id": "subscription-1",
            "outlook_client_state": "client-state-1",
            "outlook_user_id": "self-user",
        },
        credentials={"access_token": "graph-token"},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(outlook_router, "async_session", db_module.async_session)

    async def skip_mark_queued(*_args, **_kwargs):
        return None

    monkeypatch.setattr(outlook_router, "_mark_queued", skip_mark_queued)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        outlook_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )

    payload = {"value": [_notification(message_id="message-claim")]}
    path = f"/api/v1/channels/outlook/notifications?config_id={config.id}"
    first = await client.post(path, json=payload)
    duplicate = await client.post(path, json=payload)

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_outlook_webhook_concurrent_duplicate_message_is_published_once(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from apps.api.routers.channels import outlook as outlook_router
    from packages.core.services.channel_service import handle_inbound_message as original_handle_inbound_message

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="outlook-concurrent-entity",
        owner_user_id="outlook-owner",
        channel_type="outlook",
        provider="microsoft_graph",
        config={
            "outlook_subscription_id": "subscription-1",
            "outlook_client_state": "client-state-1",
            "outlook_user_id": "self-user",
        },
        credentials={"access_token": "graph-token"},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(outlook_router, "async_session", db_module.async_session)

    async def skip_mark_queued(*_args, **_kwargs):
        return None

    monkeypatch.setattr(outlook_router, "_mark_queued", skip_mark_queued)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        outlook_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )

    entered = 0
    both_entered = asyncio.Event()

    async def racing_handle(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=5)
        return await original_handle_inbound_message(*args, **kwargs)

    monkeypatch.setattr(outlook_router, "handle_inbound_message", racing_handle)
    path = f"/api/v1/channels/outlook/notifications?config_id={config.id}"
    payload = {"value": [_notification(message_id="message-concurrent")]}

    responses = await asyncio.gather(client.post(path, json=payload), client.post(path, json=payload))

    statuses = [response.status_code for response in responses]
    assert statuses.count(202) >= 1
    assert set(statuses) <= {202, 503}
    assert len(dispatched) == 1
