"""Discord Gateway mention routing for user-owned Guild installations."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from packages.core.models.channel import ChannelContact, MessageLog
from packages.core.models.base import generate_ulid
from packages.core.models.document import Channel
from packages.core.models.workspace import AgentSubscription, Workspace
from tests.test_discord_interactions import _seed_discord_connection


ROOT = Path(__file__).resolve().parents[1]


def _message_create_payload(
    *,
    message_id: str = "discord-message-1",
    guild_id: str = "guild-123",
    content: str = "<@discord-bot-user> hello",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "guild_id": guild_id,
        "channel_id": "channel-456",
        "type": 0,
        "content": content,
        "author": {
            "id": "discord-user",
            "username": "tester",
            "global_name": "Discord Tester",
            "bot": False,
        },
        "mentions": [
            {
                "id": "discord-bot-user",
                "username": "Manor staging",
                "bot": True,
            },
        ],
    }


def test_discord_gateway_only_accepts_human_mentions_in_a_guild() -> None:
    from packages.core.services.channels.discord_gateway import (
        normalize_discord_message,
    )

    mention = normalize_discord_message(
        _message_create_payload(),
        bot_user_id="discord-bot-user",
    )
    plain = normalize_discord_message(
        {**_message_create_payload(content="hello"), "mentions": []},
        bot_user_id="discord-bot-user",
    )
    direct_message = normalize_discord_message(
        {**_message_create_payload(), "guild_id": None},
        bot_user_id="discord-bot-user",
    )
    bot_message = normalize_discord_message(
        {
            **_message_create_payload(),
            "author": {
                "id": "another-bot",
                "username": "another-bot",
                "bot": True,
            },
        },
        bot_user_id="discord-bot-user",
    )

    assert mention is not None
    assert mention.message_id == "discord-message-1"
    assert mention.guild_id == "guild-123"
    assert mention.channel_id == "channel-456"
    assert mention.sender_id == "discord-user"
    assert mention.sender_name == "Discord Tester"
    assert mention.content == "hello"
    assert plain is None
    assert direct_message is None
    assert bot_message is None


def test_discord_gateway_requests_only_non_privileged_intents() -> None:
    from packages.core.services.channels.discord_gateway import build_gateway_intents

    intents = build_gateway_intents()

    assert intents.guilds is True
    assert intents.guild_messages is True
    assert intents.message_content is False
    assert intents.members is False
    assert intents.presences is False


def test_discord_gateway_client_keeps_deployment_application_id() -> None:
    from packages.core.services.channels.discord_gateway import (
        ManorDiscordGatewayClient,
    )

    client = ManorDiscordGatewayClient(application_id="discord-app-id")

    assert client.deployment_application_id == "discord-app-id"


@pytest.mark.asyncio
async def test_discord_gateway_config_does_not_resolve_oauth_secret(monkeypatch) -> None:
    from packages.core.services import discord_app_config

    monkeypatch.setenv("DISCORD_CLIENT_ID", "discord-app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "deployment-bot-token")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "00" * 32)

    async def fail_if_oauth_secret_is_requested(*_args, **_kwargs):
        pytest.fail("Gateway must not lease OAuth client secret")

    monkeypatch.setattr(
        discord_app_config,
        "resolve_oauth_config",
        fail_if_oauth_secret_is_requested,
    )

    config = await discord_app_config.resolve_discord_gateway_config(object())

    assert config is not None
    assert config.application_id == "discord-app-id"
    assert config.bot_token == "deployment-bot-token"


@pytest.mark.asyncio
async def test_discord_gateway_marks_ready_after_resume(
    tmp_path,
    monkeypatch,
) -> None:
    from packages.core.services.channels import discord_gateway

    ready_path = tmp_path / "discord-ready"
    monkeypatch.setattr(discord_gateway, "_READY_PATH", ready_path)
    client = discord_gateway.ManorDiscordGatewayClient(
        application_id="discord-app-id",
    )

    await client.on_resumed()

    assert ready_path.is_file()


@pytest.mark.asyncio
async def test_discord_gateway_rejects_mismatched_token_application(
    tmp_path,
    monkeypatch,
) -> None:
    from packages.core.services.channels import discord_gateway

    ready_path = tmp_path / "discord-ready"
    monkeypatch.setattr(discord_gateway, "_READY_PATH", ready_path)
    client = discord_gateway.ManorDiscordGatewayClient(
        application_id="discord-app-id",
    )
    client._connection.application_id = 999
    client._connection.user = SimpleNamespace(id=123)
    with pytest.raises(RuntimeError, match="Discord Gateway application mismatch"):
        await client.setup_hook()

    assert not ready_path.exists()




@pytest.mark.asyncio
async def test_discord_gateway_routes_private_connection_and_deduplicates(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services.channels.discord_gateway import (
        ingest_discord_message,
    )
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    owner, config = await _seed_discord_connection(client, db_session)
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    payload = _message_create_payload()

    first = await ingest_discord_message(
        payload,
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    )
    duplicate = await ingest_discord_message(
        payload,
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    )

    assert first == "queued"
    assert duplicate == "duplicate"
    assert len(queued) == 1
    inbound_log_id = queued[0].pop("inbound_message_log_id")
    assert queued == [{
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "discord",
        "sender_id": "discord-user",
        "sender_name": "Discord Tester",
        "chat_id": "channel-456",
        "content": "hello",
    }]
    inbound_log = await db_session.get(MessageLog, inbound_log_id)
    assert inbound_log is not None
    assert inbound_log.status == "queued"
    assert inbound_log.external_id == "discord-message-1"
    assert await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
            MessageLog.channel_type == "discord",
            MessageLog.external_id == "discord-message-1",
        )
    ) == 1


@pytest.mark.asyncio
async def test_discord_gateway_rejects_deleted_workspace(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services.channels.discord_gateway import ingest_discord_message
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    owner, config = await _seed_discord_connection(client, db_session)
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        name="Deleted Discord Workspace",
        deleted_at=datetime.now(timezone.utc),
    )
    config.workspace_id = workspace.id
    db_session.add(workspace)
    await db_session.commit()
    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **_kwargs: pytest.fail("deleted workspace queued"))

    result = await ingest_discord_message(
        _message_create_payload(),
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    )

    assert result == "not_connected"
    assert await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
        )
    ) == 0


@pytest.mark.asyncio
async def test_discord_gateway_recovers_receipt_after_broker_failure(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services.channels.discord_gateway import (
        ingest_discord_message,
        recover_discord_gateway_messages,
    )
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    _owner, config = await _seed_discord_connection(client, db_session)

    def unavailable(**_kwargs: Any) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(dispatch_inbound_task, "delay", unavailable)
    result = await ingest_discord_message(
        _message_create_payload(),
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    )

    assert result == "stored_for_retry"
    receipt = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config.id,
                MessageLog.external_id == "discord-message-1",
            )
        )
    ).scalar_one()
    await db_session.refresh(receipt)
    assert receipt.status == "received"
    assert receipt.attachments["_manor_inbound_dispatch"]["source"] == (
        "discord_gateway"
    )

    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )

    assert await recover_discord_gateway_messages(
        now=datetime.now(timezone.utc) + timedelta(seconds=31),
    ) == 1
    await db_session.refresh(receipt)
    assert receipt.status == "queued"
    assert len(queued) == 1
    assert queued[0]["inbound_message_log_id"] == receipt.id


@pytest.mark.asyncio
async def test_discord_gateway_recovery_ignores_slash_command_receipts(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services.channels.discord_gateway import (
        recover_discord_gateway_messages,
    )
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    owner, config = await _seed_discord_connection(client, db_session)
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="discord",
        from_address="discord-user",
        to_address="channel-456",
        content="slash command",
        external_id="discord-interaction-1",
        status="received",
    )
    db_session.add(receipt)
    await db_session.commit()
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )

    assert await recover_discord_gateway_messages() == 0
    assert queued == []


@pytest.mark.asyncio
async def test_discord_gateway_recovery_fails_closed_for_deleted_workspace(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services.channels.discord_gateway import recover_discord_gateway_messages
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    owner, config = await _seed_discord_connection(client, db_session)
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        name="Deleted Discord Workspace",
        deleted_at=datetime.now(timezone.utc),
    )
    config.workspace_id = workspace.id
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="discord",
        from_address="discord-user",
        to_address="channel-456",
        content="recover deleted",
        external_id="discord-message-deleted",
        status="received",
        attachments={
            "_manor_inbound_dispatch": {
                "source": "discord_gateway",
                "published_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=20)
                ).isoformat(),
            }
        },
    )
    db_session.add_all([workspace, receipt])
    await db_session.commit()
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))

    assert await recover_discord_gateway_messages() == 0
    await db_session.refresh(receipt)
    assert receipt.status == "failed"
    assert queued == []


@pytest.mark.asyncio
async def test_discord_gateway_claim_fences_concurrent_worker_delivery(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway
    from packages.core.services.channels.discord_gateway import (
        ingest_discord_message,
    )
    from packages.core.tasks.channel_tasks import (
        _dispatch_slack_inbound_once,
        dispatch_inbound_task,
    )

    owner, config = await _seed_discord_connection(client, db_session)
    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **_kwargs: None)
    assert await ingest_discord_message(
        _message_create_payload(),
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    ) == "queued"
    receipt = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config.id,
                MessageLog.external_id == "discord-message-1",
            )
        )
    ).scalar_one()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def dispatch(**_kwargs: Any) -> dict[str, str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", dispatch)
    kwargs = {
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "discord",
        "sender_id": "discord-user",
        "sender_name": "Discord Tester",
        "chat_id": "channel-456",
        "content": "hello",
    }
    first = asyncio.create_task(
        _dispatch_slack_inbound_once(
            inbound_message_log_id=receipt.id,
            dispatch_claim_id="claim-a",
            **kwargs,
        )
    )
    await entered.wait()

    duplicate = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="claim-b",
        **kwargs,
    )
    release.set()

    assert duplicate == {"status": "duplicate_inflight"}
    assert await first == {"status": "ok"}
    assert calls == 1


@pytest.mark.asyncio
async def test_discord_gateway_stale_claim_can_be_recovered(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    owner, config = await _seed_discord_connection(client, db_session)
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="discord",
        from_address="discord-user",
        to_address="channel-456",
        content="recover stale claim",
        external_id="discord-message-stale",
        status="processing",
        attachments={
            "_manor_inbound_dispatch": {
                "source": "discord_gateway",
                "sender_name": "Discord Tester",
                "claim_id": "dead-worker",
                "claimed_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=20)
                ).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()
    calls = 0

    async def dispatch(**_kwargs: Any) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", dispatch)

    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="replacement-worker",
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content="recover stale claim",
    )

    assert result == {"status": "ok"}
    assert calls == 1


@pytest.mark.asyncio
async def test_discord_gateway_reuses_prepared_reply_on_worker_redelivery(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway
    from packages.core.services.channels.discord_gateway import ingest_discord_message
    from packages.core.tasks.channel_tasks import (
        _dispatch_slack_inbound_once,
        dispatch_inbound_task,
    )

    owner, config = await _seed_discord_connection(client, db_session)
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Discord prepared reply",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        source_id="discord-user",
        status="active",
    )
    db_session.add_all([binding, contact])
    await db_session.commit()
    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **_kwargs: None)
    assert await ingest_discord_message(
        _message_create_payload(),
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    ) == "queued"
    receipt = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config.id,
                MessageLog.external_id == "discord-message-1",
            )
        )
    ).scalar_one()
    receipt.attachments = {
            "_manor_inbound_dispatch": {
                "source": "discord_gateway",
                "sender_name": "Discord Tester",
                "prepared_reply": "already prepared",
                "prepared_reply_source": "channel_gateway",
                "channel_binding_id": binding.id,
                "channel_contact_id": contact.id,
                "agent_id": "manor-master",
                "route_snapshot": {
                    "version": 1,
                    "config_workspace_id": None,
                    "binding_workspace_id": None,
                    "runtime_workspace_id": None,
                },
            }
    }
    await db_session.commit()
    monkeypatch.setattr(
        channel_gateway,
        "dispatch_inbound",
        lambda **_kwargs: pytest.fail("agent turn repeated"),
    )
    sent: list[dict[str, Any]] = []

    async def send_reply(**kwargs: Any) -> bool:
        sent.append(kwargs)
        return True

    from packages.core.services import channel_outbound_delivery

    monkeypatch.setattr(channel_outbound_delivery, "send_channel_text_reply", send_reply)
    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="redelivery",
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content="hello",
    )

    assert result == {"status": "ok", "sent": True}
    assert sent == [{
        "cc_id": config.id,
        "channel_type": "discord",
        "chat_id": "channel-456",
        "text": "already prepared",
        "idempotency_key": receipt.id,
        "channel_binding_id": binding.id,
        "require_active_binding": True,
    }]


@pytest.mark.asyncio
async def test_discord_legacy_prepared_reply_without_exact_route_fails_closed(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway, channel_outbound_delivery
    from packages.core.services.channels.discord_gateway import ingest_discord_message
    from packages.core.tasks.channel_tasks import (
        _dispatch_slack_inbound_once,
        dispatch_inbound_task,
    )

    owner, config = await _seed_discord_connection(client, db_session)
    db_session.add(Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Legacy Discord prepared reply",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    ))
    await db_session.commit()
    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **_kwargs: None)
    assert await ingest_discord_message(
        _message_create_payload(message_id="discord-legacy-prepared"),
        application_id="discord-app-id",
        bot_user_id="discord-bot-user",
    ) == "queued"
    receipt = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config.id,
                MessageLog.external_id == "discord-legacy-prepared",
            )
        )
    ).scalar_one()
    receipt.attachments = {
        "_manor_inbound_dispatch": {
            "source": "discord_gateway",
            "sender_name": "Discord Tester",
            "prepared_reply": "must not follow a reassigned route",
            "prepared_reply_source": "channel_gateway",
        },
    }
    await db_session.commit()
    monkeypatch.setattr(
        channel_gateway,
        "dispatch_inbound",
        lambda **_kwargs: pytest.fail("legacy prepared reply must not rerun the agent"),
    )

    async def unexpected_send(**_kwargs: Any) -> bool:
        raise AssertionError("legacy prepared reply must not reach provider I/O")

    monkeypatch.setattr(
        channel_outbound_delivery,
        "send_channel_text_reply",
        unexpected_send,
    )

    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="legacy-redelivery",
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content="hello",
    )

    await db_session.refresh(receipt)
    assert result == {"status": "skipped", "reason": "reply_route_unavailable"}
    assert receipt.status == "processed"


@pytest.mark.asyncio
async def test_discord_prepared_reply_stops_after_binding_is_disabled(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway, channel_outbound_delivery
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    owner, config = await _seed_discord_connection(client, db_session)
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Disabled Discord prepared reply",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="inactive",
    )
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="discord",
        from_address="discord-user",
        to_address="channel-456",
        content="do not send after disable",
        external_id="discord-disabled-prepared-reply",
        status="queued",
        attachments={
            "_manor_inbound_dispatch": {
                "source": "discord_gateway",
                "prepared_reply": "stale prepared reply",
                "prepared_reply_source": "channel_gateway",
                "channel_binding_id": binding.id,
            }
        },
    )
    db_session.add_all([binding, receipt])
    await db_session.commit()
    monkeypatch.setattr(
        channel_gateway,
        "dispatch_inbound",
        lambda **_kwargs: pytest.fail("prepared reply must not repeat the agent turn"),
    )

    async def unexpected_send(**_kwargs: Any) -> bool:
        raise AssertionError("disabled channel binding must block the prepared reply")

    monkeypatch.setattr(
        channel_outbound_delivery,
        "send_channel_text_reply",
        unexpected_send,
    )

    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="disabled-route-redelivery",
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content=receipt.content,
    )

    await db_session.refresh(receipt)
    assert result == {"status": "skipped", "reason": "reply_route_unavailable"}
    assert receipt.status == "processed"


@pytest.mark.asyncio
async def test_discord_prepared_reply_route_rejects_paused_workspace(
    client,
    db_session,
) -> None:
    from packages.core.services.channel_outbound_delivery import (
        channel_reply_route_is_active,
    )

    owner, config = await _seed_discord_connection(client, db_session)
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        name="Paused Discord workspace",
        status="paused",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        workspace_id=workspace.id,
        type="discord",
        name="Paused Workspace Discord",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    config.workspace_id = workspace.id
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        source_id="paused-discord-contact",
        status="active",
    )
    db_session.add_all([workspace, binding, contact])
    await db_session.commit()

    assert not await channel_reply_route_is_active(
        entity_id=owner["entity_id"],
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="manor-master",
        agent_subscription_id=None,
        route_snapshot={
            "version": 1,
            "config_workspace_id": workspace.id,
            "binding_workspace_id": workspace.id,
            "runtime_workspace_id": workspace.id,
        },
        workspace_id=workspace.id,
    )


@pytest.mark.asyncio
async def test_discord_prepared_reply_rejects_new_workspace_scope(
    client,
    db_session,
) -> None:
    from packages.core.services.channel_outbound_delivery import (
        build_channel_reply_route_snapshot,
        channel_reply_route_is_active,
    )

    owner, config = await _seed_discord_connection(client, db_session)
    binding = Channel(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Initially global Discord route",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    route_snapshot = build_channel_reply_route_snapshot(
        config_workspace_id=None,
        binding_workspace_id=None,
        runtime_workspace_id=None,
    )
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        name="New Discord scope",
        status="active",
    )
    config.workspace_id = workspace.id
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        source_id="new-scope-discord-contact",
        status="active",
    )
    db_session.add_all([binding, workspace, contact])
    await db_session.commit()

    assert not await channel_reply_route_is_active(
        entity_id=owner["entity_id"],
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="manor-master",
        agent_subscription_id=None,
        route_snapshot=route_snapshot,
        workspace_id=None,
    )


@pytest.mark.asyncio
async def test_discord_prepared_reply_route_rejects_agent_reassignment(
    client,
    db_session,
) -> None:
    from packages.core.services.channel_outbound_delivery import (
        channel_reply_route_is_active,
    )

    owner, config = await _seed_discord_connection(client, db_session)
    binding = Channel(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Reassigned Discord route",
        config={"channel_config_id": config.id},
        agent_id="replacement-agent",
        status="active",
    )
    contact = ChannelContact(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        source_id="reassigned-discord-contact",
        status="active",
    )
    db_session.add_all([binding, contact])
    await db_session.commit()

    assert not await channel_reply_route_is_active(
        entity_id=owner["entity_id"],
        cc_id=config.id,
        channel_type="discord",
        channel_binding_id=binding.id,
        channel_contact_id=contact.id,
        agent_id="original-agent",
        agent_subscription_id=None,
        route_snapshot={
            "version": 1,
            "config_workspace_id": None,
            "binding_workspace_id": None,
            "runtime_workspace_id": None,
        },
        workspace_id=None,
    )


@pytest.mark.asyncio
async def test_discord_gateway_rejects_foreign_agent_subscription(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway

    owner, config = await _seed_discord_connection(client, db_session)
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        name="Local Discord Workspace",
        status="active",
    )
    foreign_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        agent_id="foreign-agent",
        workspace_id=workspace.id,
        status="active",
    )
    binding = Channel(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        workspace_id=workspace.id,
        type="discord",
        name="Foreign subscription",
        config={"channel_config_id": config.id},
        agent_id=foreign_subscription.agent_id,
        agent_subscription_id=foreign_subscription.id,
        status="active",
    )
    config.workspace_id = workspace.id
    db_session.add_all([workspace, foreign_subscription, binding])
    await db_session.commit()

    async def unexpected_agent(**_kwargs: Any):
        raise AssertionError("foreign subscription must not enter the agent runtime")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", unexpected_agent)

    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content="do not cross entities",
    )

    assert result == {
        "status": "unbound",
        "channel_config_id": config.id,
        "reason": "invalid_agent_subscription",
    }


@pytest.mark.asyncio
async def test_discord_first_reply_revalidates_route_after_agent_run(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway
    from packages.core.services.channel_agent_runtime import ChannelAgentRunResult

    owner, config = await _seed_discord_connection(client, db_session)
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="discord",
        name="Discord route revalidation",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    db_session.add(binding)
    await db_session.commit()

    async def run_agent(**_kwargs: Any) -> ChannelAgentRunResult:
        return ChannelAgentRunResult(content="prepared but no longer routable")

    async def no_hold(**_kwargs: Any) -> None:
        return None

    async def no_runtime_events(*_args: Any, **_kwargs: Any) -> None:
        return None

    route_checks: list[dict[str, Any]] = []

    async def route_inactive(**kwargs: Any) -> bool:
        route_checks.append(kwargs)
        return False

    async def unexpected_send(**_kwargs: Any) -> bool:
        raise AssertionError("a route disabled during the agent run must block provider I/O")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", run_agent)
    monkeypatch.setattr(channel_gateway, "maybe_hold_external_reply_for_approval", no_hold)
    monkeypatch.setattr(channel_gateway, "runtime_persist_channel_runtime_events", no_runtime_events)
    monkeypatch.setattr(channel_gateway, "channel_reply_route_is_active", route_inactive)
    monkeypatch.setattr(channel_gateway, "send_channel_text_reply", unexpected_send)

    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content="run, then disable",
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "reply_route_unavailable"
    assert len(route_checks) == 1
    assert route_checks[0].pop("channel_contact_id")
    assert route_checks == [{
        "entity_id": owner["entity_id"],
        "cc_id": config.id,
        "channel_type": "discord",
        "channel_binding_id": binding.id,
        "agent_id": "manor-master",
        "agent_subscription_id": None,
        "route_snapshot": {
            "version": 1,
            "config_workspace_id": None,
            "binding_workspace_id": None,
            "runtime_workspace_id": None,
        },
        "workspace_id": None,
    }]


@pytest.mark.asyncio
async def test_inactive_channel_config_stops_before_agent_runtime(
    client,
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import channel_gateway

    owner, config = await _seed_discord_connection(client, db_session)
    config.status = "inactive"
    await db_session.commit()

    async def unexpected_agent(**_kwargs: Any):
        raise AssertionError("inactive channel config must stop before agent runtime")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", unexpected_agent)

    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="discord",
        sender_id="discord-user",
        sender_name="Discord Tester",
        chat_id="channel-456",
        content="do not run",
    )

    assert result == {"status": "skipped", "reason": "channel_config_inactive"}
