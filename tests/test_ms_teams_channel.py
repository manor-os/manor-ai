from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select


def _channel_config() -> SimpleNamespace:
    return SimpleNamespace(
        id="teams-config",
        entity_id="entity-1",
        config={
            "teams_client_state": "client-state-1",
            "teams_subscription_id": "subscription-1",
        },
    )


def test_teams_adapter_uses_fixed_notification_endpoint_and_validates_client_state():
    from packages.core.services.channels.ms_teams_adapter import (
        TeamsChannelAdapter,
    )

    adapter = TeamsChannelAdapter()
    assert adapter.webhook_path(_channel_config()) == "/api/v1/channels/ms_teams/notifications"

    notification = {
        "subscriptionId": "subscription-1",
        "clientState": "client-state-1",
        "resource": "users/user-1/chats/chat-1/messages/message-1",
        "resourceData": {
            "id": "message-1",
            "from": {"user": {"id": "sender-1", "displayName": "Sender"}},
            "body": {"contentType": "text", "content": "Hello Manor"},
        },
    }

    parsed = adapter.parse_notification(_channel_config(), notification)
    assert parsed is not None
    assert parsed.source_id == "sender-1"
    assert parsed.reply_to == "chat-1"
    assert parsed.content == "Hello Manor"
    assert parsed.external_message_id == "message-1"

    notification["clientState"] = "wrong-state"
    assert adapter.parse_notification(_channel_config(), notification) is None


def test_teams_adapter_ignores_messages_sent_by_the_connected_user():
    from packages.core.services.channels.ms_teams_adapter import TeamsChannelAdapter

    config = _channel_config()
    config.config["teams_user_id"] = "self-user"
    notification = {
        "subscriptionId": "subscription-1",
        "clientState": "client-state-1",
        "resource": "users/self-user/chats/chat-1/messages/message-1",
        "resourceData": {
            "id": "message-1",
            "from": {"user": {"id": "self-user", "displayName": "Me"}},
            "body": {"contentType": "html", "content": "<p>Echo</p>"},
        },
    }

    assert TeamsChannelAdapter().parse_notification(config, notification) is None


def test_teams_adapter_rejects_notifications_before_subscription_registration():
    from packages.core.services.channels.ms_teams_adapter import TeamsChannelAdapter

    config = SimpleNamespace(id="teams-config", entity_id="entity-1", config={})
    notification = {
        "subscriptionId": "attacker-subscription",
        "clientState": "attacker-state",
        "resource": "users/user-1/chats/chat-1/messages/message-1",
        "resourceData": {
            "id": "message-1",
            "from": {"user": {"id": "sender-1"}},
            "body": {"contentType": "text", "content": "Forged"},
        },
    }

    assert TeamsChannelAdapter().parse_notification(config, notification) is None


def test_teams_subscription_renewal_is_scheduled():
    from packages.core.celery_app import celery_app

    assert celery_app.conf.beat_schedule[
        "teams-subscription-renewal"
    ]["task"] == "channel.ms_teams_subscription_tick"


@pytest.mark.asyncio
async def test_teams_registration_clears_pending_after_success(monkeypatch):
    from apps.api.routers import integrations

    config = SimpleNamespace(
        id="teams-config",
        channel_type="ms_teams",
        config={"teams_registration_pending": True},
    )

    class DB:
        commits = 0

        async def commit(self):
            self.commits += 1

    class Adapter:
        async def register_webhook(self, _config):
            return {"registered": True, "subscription_id": "sub-1"}

    async def configs(*_args, **_kwargs):
        return [config]

    monkeypatch.setattr(integrations, "_channel_configs_for_integration", configs)
    monkeypatch.setattr(
        "packages.core.services.channels.get_adapter", lambda _type: Adapter(),
    )

    failures = await integrations._register_integration_channel_webhooks(
        DB(), entity_id="entity-1", integration_id="integration-1",
    )

    assert failures == []
    assert config.config["teams_registration_pending"] is False


@pytest.mark.asyncio
async def test_teams_registration_recreates_missing_remote_subscription(monkeypatch):
    from packages.core.services.channels.ms_teams_adapter import TeamsChannelAdapter

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.example"),
    )
    adapter = TeamsChannelAdapter()
    config = _channel_config()
    config.config["teams_user_id"] = "teams-user"
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
    assert config.config["teams_subscription_id"] == "subscription-recreated"


@pytest.mark.asyncio
async def test_teams_registration_failure_is_reported_and_keeps_pending(monkeypatch):
    from apps.api.routers import integrations

    config = SimpleNamespace(
        id="teams-config",
        channel_type="ms_teams",
        config={"teams_registration_pending": True},
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

    assert failures == ["teams-config"]
    assert config.config["teams_registration_pending"] is True


def test_teams_registration_task_retries_after_graph_failure(monkeypatch):
    from celery.exceptions import Retry
    from packages.core.tasks import channel_tasks

    def fail_run(coro):
        coro.close()
        raise RuntimeError("Graph unavailable")

    monkeypatch.setattr(channel_tasks, "_run_async", fail_run)
    retry_calls: list[dict] = []

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    monkeypatch.setattr(
        channel_tasks.register_integration_webhooks_task,
        "retry",
        retry,
    )

    with pytest.raises(Retry):
        channel_tasks.register_integration_webhooks_task.run(
            entity_id="entity-1", integration_id="integration-1",
        )

    assert retry_calls and retry_calls[0]["countdown"] == 30


def test_teams_pending_config_is_processed_by_periodic_tick(monkeypatch):
    from packages.core.tasks import channel_tasks

    config = SimpleNamespace(
        id="teams-config",
        channel_type="ms_teams",
        status="active",
        config={"teams_registration_pending": True},
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [config.id]

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return Result()

        async def get(self, _model, _id):
            return config

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class Adapter:
        async def register_webhook(self, cc):
            cc.config["teams_registration_pending"] = False
            return {"registered": True}

    async def run(coro):
        return await coro

    monkeypatch.setattr("packages.core.database.async_session", lambda: DB())
    monkeypatch.setattr(
        "packages.core.services.channels.ms_teams_adapter.TeamsChannelAdapter",
        Adapter,
    )
    monkeypatch.setattr(channel_tasks, "_run_async", lambda coro: asyncio.run(coro))

    result = channel_tasks.ms_teams_subscription_tick.run()

    assert result["checked"] == 1
    assert result["renewed"] == 1


@pytest.mark.asyncio
async def test_teams_subscription_is_unregistered_before_bridge_deletion(monkeypatch):
    from apps.api.routers import integrations

    config = SimpleNamespace(
        id="teams-config",
        channel_type="ms_teams",
        config={"teams_subscription_id": "subscription-1"},
    )
    calls: list[str] = []

    class DB:
        pass

    class Adapter:
        async def unregister_webhook(self, cc):
            calls.append(cc.id)
            return {"unregistered": True}

    async def configs(*_args, **_kwargs):
        return [config]

    monkeypatch.setattr(integrations, "_channel_configs_for_integration", configs)
    monkeypatch.setattr(
        "packages.core.services.channels.get_adapter", lambda _type: Adapter(),
    )

    await integrations._unregister_teams_webhooks(
        DB(), entity_id="entity-1", integration_id="integration-1",
    )

    assert calls == ["teams-config"]


@pytest.mark.asyncio
async def test_teams_receipt_claim_is_single_use(db_session, monkeypatch):
    """A repeated Graph message ID must have one dispatch claim."""
    import packages.core.database as db_module
    from apps.api.routers.channels import ms_teams as teams_router
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import MessageLog

    monkeypatch.setattr(teams_router, "async_session", db_module.async_session)
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="teams-claim-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="ms_teams",
        external_id="teams-message-claim-once",
        status="received",
        content="claim me",
    )
    db_session.add(receipt)
    await db_session.commit()

    first, first_claimed = await teams_router._claim_received(
        receipt.channel_config_id,
        receipt.external_id,
    )
    second, second_claimed = await teams_router._claim_received(
        receipt.channel_config_id,
        receipt.external_id,
    )

    assert first is not None and first_claimed is True
    assert second is not None and second_claimed is False
    assert second.status == "queued"


@pytest.mark.asyncio
async def test_teams_webhook_deduplicates_before_dispatch(
    client,
    db_session,
    monkeypatch,
):
    """A replayed Graph notification must not enqueue a second agent turn."""
    import packages.core.database as db_module
    from apps.api.routers.channels import ms_teams as teams_router
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig, MessageLog

    config = ChannelConfig(
        id=generate_ulid(),
        entity_id="teams-webhook-entity",
        owner_user_id="teams-owner",
        channel_type="ms_teams",
        provider="microsoft_graph",
        config={
            "teams_subscription_id": "subscription-1",
            "teams_client_state": "client-state-1",
            "teams_user_id": "self-user",
        },
        credentials={"access_token": "graph-token"},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(teams_router, "async_session", db_module.async_session)
    dispatched: list[dict] = []
    monkeypatch.setattr(
        teams_router.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )

    payload = {
        "value": [{
            "subscriptionId": "subscription-1",
            "clientState": "client-state-1",
            "changeType": "created",
            "resource": "users/self-user/chats/chat-1/messages/message-1",
            "resourceData": {
                "id": "message-1",
                "from": {"user": {"id": "sender-1", "displayName": "Sender"}},
                "body": {"contentType": "text", "content": "Hello Manor"},
            },
        }],
    }
    path = "/api/v1/channels/ms_teams/notifications"
    first = await client.post(path, json=payload)
    duplicate = await client.post(path, json=payload)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert len(dispatched) == 1
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
