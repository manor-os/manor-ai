"""Slack Events API routing, deduplication, and reply context."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from sqlalchemy import func, select

from packages.core.credentials import get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, ChannelContact, MessageLog
from packages.core.models.document import Channel
from packages.core.models.task import Conversation
from packages.core.models.user import OAuthAccount
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter
from packages.core.services.channels.slack_adapter import SlackChannelAdapter
from tests.test_document_permissions import _auth


_SIGNING_SECRET = "slack-events-signing-secret"


def _signed_headers(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        _SIGNING_SECRET.encode(),
        f"v0:{timestamp}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={signature}",
    }


def _event_payload(
    *,
    event_id: str,
    event_type: str = "app_mention",
    app_id: str = "A-MANOR",
    team_id: str | None = "T-MANOR",
    enterprise_id: str | None = None,
    channel_type: str | None = None,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "user": "U-SENDER",
        "channel": "C-SUPPORT",
        "text": "<@U-MANOR-BOT> can you help?",
        "ts": "1724457600.000100",
    }
    if channel_type is not None:
        event["channel_type"] = channel_type
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    payload = {
        "type": "event_callback",
        "event_id": event_id,
        "api_app_id": app_id,
        "event": event,
    }
    if team_id is not None:
        payload["team_id"] = team_id
    if enterprise_id is not None:
        payload["enterprise_id"] = enterprise_id
    return payload


async def _current_user(client, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _seed_slack_connection(
    client,
    db_session,
    *,
    username: str,
    app_id: str = "A-MANOR",
    team_id: str | None = "T-MANOR",
    enterprise_id: str | None = None,
    source_available: bool = True,
) -> tuple[dict[str, Any], ChannelConfig]:
    headers = await _auth(client, username)
    owner = await _current_user(client, headers)
    account_id = generate_ulid()
    if source_available:
        account = OAuthAccount(
            id=account_id,
            user_id=owner["id"],
            provider="slack",
            provider_user_id=f"U-{username}",
            profile={"app_id": app_id, "team_id": team_id},
        )
        get_credential_service().store_oauth_account(
            account,
            {"access_token": f"xoxb-{username}"},
        )
        db_session.add(account)

    routing_config = {"slack_app_id": app_id}
    if team_id is not None:
        routing_config["slack_team_id"] = team_id
    if enterprise_id is not None:
        routing_config["slack_enterprise_id"] = enterprise_id
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="slack",
        provider="slack_app",
        name="Manor QA",
        credential_source_kind="oauth_account",
        credential_source_id=account_id,
        config={
            "connection_kind": "oauth_account",
            "connection_id": account_id,
            **routing_config,
        },
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    return owner, config


@pytest.mark.asyncio
async def test_slack_events_url_verification_has_a_fixed_signed_endpoint(
    client,
    monkeypatch,
):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    body = json.dumps(
        {"type": "url_verification", "challenge": "fixed-endpoint-challenge"},
        separators=(",", ":"),
    ).encode()

    rejected = await client.post("/api/v1/channels/slack/events", content=body)
    assert rejected.status_code == 403

    verified = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )
    assert verified.status_code == 200
    assert verified.text == "fixed-endpoint-challenge"


@pytest.mark.asyncio
async def test_slack_app_mention_resolves_private_connection_and_deduplicates(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_event_owner",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))
    payload = _event_payload(event_id="Ev-MANOR-1", thread_ts="1724457500.000050")
    body = json.dumps(payload, separators=(",", ":")).encode()

    first = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )
    duplicate = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == {"ok": True, "noop": True, "reason": "duplicate_event"}
    assert len(queued) == 1
    inbound_message_log_id = queued[0].pop("inbound_message_log_id")
    assert queued == [{
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "slack",
        "sender_id": "U-SENDER",
        "sender_name": None,
        "chat_id": "C-SUPPORT",
        "content": "<@U-MANOR-BOT> can you help?",
        "thread_ts": "1724457500.000050",
    }]
    log_count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.direction == "inbound",
            MessageLog.external_id == "Ev-MANOR-1",
        )
    )
    assert log_count == 1
    inbound_log = await db_session.get(MessageLog, inbound_message_log_id)
    assert inbound_log is not None
    assert inbound_log.external_id == "Ev-MANOR-1"


@pytest.mark.asyncio
async def test_slack_queue_failure_rolls_back_receipt_so_slack_can_retry(
    client,
    db_session,
    monkeypatch,
):
    _, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_queue_retry_owner",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    attempts = 0
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    def _delay(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("broker unavailable")
        queued.append(kwargs)

    monkeypatch.setattr(dispatch_inbound_task, "delay", _delay)
    payload = _event_payload(event_id="Ev-QUEUE-RETRY")
    body = json.dumps(payload, separators=(",", ":")).encode()

    failed = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )
    retried = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )

    assert failed.status_code == 503
    assert retried.status_code == 200
    assert len(queued) == 1
    log_count = await db_session.scalar(
        select(func.count(MessageLog.id)).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.external_id == "Ev-QUEUE-RETRY",
        )
    )
    assert log_count == 1


@pytest.mark.asyncio
async def test_slack_direct_message_dispatches_but_public_message_does_not(
    client,
    db_session,
    monkeypatch,
):
    _, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_dm_owner",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))

    for payload in (
        _event_payload(event_id="Ev-DM", event_type="message", channel_type="im"),
        _event_payload(event_id="Ev-PUBLIC", event_type="message", channel_type="channel"),
    ):
        body = json.dumps(payload, separators=(",", ":")).encode()
        response = await client.post(
            "/api/v1/channels/slack/events",
            content=body,
            headers=_signed_headers(body),
        )
        assert response.status_code == 200

    assert len(queued) == 1
    assert queued[0]["channel_config_id"] == config.id
    assert queued[0]["thread_ts"] is None


@pytest.mark.asyncio
async def test_slack_enterprise_installation_routes_without_a_team_id(
    client,
    db_session,
    monkeypatch,
):
    _, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_grid_owner",
        team_id=None,
        enterprise_id="E-MANOR-GRID",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))
    payload = _event_payload(
        event_id="Ev-GRID",
        team_id=None,
        enterprise_id="E-MANOR-GRID",
    )
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200
    assert queued[0]["channel_config_id"] == config.id


@pytest.mark.asyncio
async def test_slack_exact_team_route_wins_over_enterprise_installation(
    client,
    db_session,
    monkeypatch,
):
    _, enterprise_config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_grid_enterprise_owner",
        team_id=None,
        enterprise_id="E-MANOR-GRID-PREFERENCE",
    )
    _, team_config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_grid_team_owner",
        team_id="T-MANOR-GRID-PREFERENCE",
        enterprise_id="E-MANOR-GRID-PREFERENCE",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))
    payload = _event_payload(
        event_id="Ev-GRID-PREFERENCE",
        team_id="T-MANOR-GRID-PREFERENCE",
        enterprise_id="E-MANOR-GRID-PREFERENCE",
    )
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200
    assert enterprise_config.id != team_config.id
    assert queued[0]["channel_config_id"] == team_config.id


@pytest.mark.asyncio
async def test_slack_event_without_team_only_routes_enterprise_installation(
    client,
    db_session,
    monkeypatch,
):
    await _seed_slack_connection(
        client,
        db_session,
        username="slack_grid_scoped_team_owner",
        team_id="T-MANOR-GRID-SCOPED",
        enterprise_id="E-MANOR-GRID-SCOPED",
    )
    _, enterprise_config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_grid_scoped_enterprise_owner",
        team_id=None,
        enterprise_id="E-MANOR-GRID-SCOPED",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))
    payload = _event_payload(
        event_id="Ev-GRID-NO-TEAM",
        team_id=None,
        enterprise_id="E-MANOR-GRID-SCOPED",
    )
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200
    assert queued[0]["channel_config_id"] == enterprise_config.id


@pytest.mark.asyncio
async def test_slack_workspace_install_does_not_route_another_enterprise_team(
    client,
    db_session,
    monkeypatch,
):
    await _seed_slack_connection(
        client,
        db_session,
        username="slack_grid_workspace_owner",
        team_id="T-GRID-ONE",
        enterprise_id="E-MANOR-GRID",
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))
    payload = _event_payload(
        event_id="Ev-OTHER-GRID-TEAM",
        team_id="T-GRID-TWO",
        enterprise_id="E-MANOR-GRID",
    )
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = await client.post(
        "/api/v1/channels/slack/events",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "connection_not_found"
    assert queued == []


@pytest.mark.asyncio
async def test_slack_events_unknown_or_unavailable_connection_is_a_safe_noop(
    client,
    db_session,
    monkeypatch,
):
    await _seed_slack_connection(
        client,
        db_session,
        username="slack_unavailable_owner",
        source_available=False,
    )
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    queued: list[dict[str, Any]] = []
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    monkeypatch.setattr(dispatch_inbound_task, "delay", lambda **kwargs: queued.append(kwargs))

    for payload in (
        _event_payload(event_id="Ev-UNAVAILABLE"),
        _event_payload(event_id="Ev-UNKNOWN", team_id="T-UNKNOWN"),
    ):
        body = json.dumps(payload, separators=(",", ":")).encode()
        response = await client.post(
            "/api/v1/channels/slack/events",
            content=body,
            headers=_signed_headers(body),
        )
        assert response.status_code == 200
        assert response.json()["noop"] is True

    assert queued == []


@pytest.mark.asyncio
async def test_slack_parser_keeps_original_thread_context():
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        channel_type="slack",
        provider="slack_app",
        config={},
        credentials={},
        status="active",
    )
    payload = _event_payload(event_id="Ev-PARSE", thread_ts="1724457000.000001")

    adapter = SlackChannelAdapter()
    parsed = await adapter.parse_inbound(
        config,
        headers={},
        query={},
        body=json.dumps(payload).encode(),
    )

    assert parsed is not None
    assert parsed.thread_ts == "1724457000.000001"
    assert adapter.webhook_path(config) == "/api/v1/channels/slack/events"

    direct_message = _event_payload(
        event_id="Ev-PARSE-DM",
        event_type="message",
        channel_type="im",
    )
    parsed_dm = await adapter.parse_inbound(
        config,
        headers={},
        query={},
        body=json.dumps(direct_message).encode(),
    )
    assert parsed_dm is not None
    assert parsed_dm.thread_ts is None


@pytest.mark.asyncio
async def test_slack_adapter_uses_client_message_id_for_retry_deduplication(
    monkeypatch,
):
    from types import SimpleNamespace

    from packages.core.services.channels import slack_adapter as module

    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        is_success = True

        @staticmethod
        def json():
            return {"ok": True, "ts": "1724457000.000001"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    adapter = SlackChannelAdapter()

    async def credentials(*_args, **_kwargs):
        return {"bot_token": "xoxb-test"}

    monkeypatch.setattr(adapter, "credentials", credentials)
    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=Client))

    result = await adapter.send_text(
        SimpleNamespace(),
        "C-SUPPORT",
        "Approved reply",
        thread_ts="1724456999.000001",
        idempotency_key="approval-message-1",
    )

    assert captured["json"] == {
        "channel": "C-SUPPORT",
        "text": "Approved reply",
        "thread_ts": "1724456999.000001",
        "client_msg_id": "approval-message-1",
    }
    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_slack_adapter_classifies_api_rejection_as_determinate(
    monkeypatch,
):
    from types import SimpleNamespace

    from packages.core.services.channels import slack_adapter as module
    from packages.core.services.channels.base import (
        ChannelTextSendError,
        ChannelTextSendFailureDisposition,
    )

    class Response:
        status_code = 200
        is_success = True

        @staticmethod
        def json():
            return {"ok": False, "error": "channel_not_found"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            return Response()

    adapter = SlackChannelAdapter()

    async def credentials(*_args, **_kwargs):
        return {"bot_token": "xoxb-test"}

    monkeypatch.setattr(adapter, "credentials", credentials)
    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=Client))

    with pytest.raises(ChannelTextSendError) as exc_info:
        await adapter.send_text(
            SimpleNamespace(),
            "missing-channel",
            "Approved reply",
        )

    assert (
        exc_info.value.disposition
        is ChannelTextSendFailureDisposition.DETERMINATE
    )


@pytest.mark.asyncio
async def test_slack_adapter_classifies_http_5xx_as_ambiguous(
    monkeypatch,
):
    from types import SimpleNamespace

    from packages.core.services.channels import slack_adapter as module
    from packages.core.services.channels.base import (
        ChannelTextSendError,
        ChannelTextSendFailureDisposition,
    )

    class Response:
        status_code = 503
        is_success = False

        @staticmethod
        def json():
            return {"ok": False, "error": "internal_error"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            return Response()

    adapter = SlackChannelAdapter()

    async def credentials(*_args, **_kwargs):
        return {"bot_token": "xoxb-test"}

    monkeypatch.setattr(adapter, "credentials", credentials)
    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=Client))

    with pytest.raises(ChannelTextSendError) as exc_info:
        await adapter.send_text(
            SimpleNamespace(),
            "C-SUPPORT",
            "Approved reply",
        )

    assert (
        exc_info.value.disposition
        is ChannelTextSendFailureDisposition.AMBIGUOUS
    )


class _RecordingSlackAdapter(ChannelAdapter):
    channel_type = "slack"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, cc, to, text, **kwargs):
        self.sent.append({"cc_id": cc.id, "to": to, "text": text, **kwargs})
        return {"status": "sent", "external_id": "slack-reply-ts"}

    async def parse_inbound(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_slack_gateway_replies_in_the_inbound_thread(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_thread_owner",
    )
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Slack support",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        source_id="U-SENDER",
        user_id=owner["id"],
        role="member",
        status="active",
    )
    db_session.add_all([binding, contact])
    await db_session.commit()

    from packages.core.services import channel_gateway

    async def _fake_run_agent(**kwargs):
        return channel_gateway.ChannelAgentRunResult(content="Threaded response")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _fake_run_agent)
    fake = _RecordingSlackAdapter()
    original = ADAPTERS["slack"]
    ADAPTERS["slack"] = fake
    try:
        result = await channel_gateway.dispatch_inbound(
            entity_id=owner["entity_id"],
            channel_config_id=config.id,
            channel_type="slack",
            sender_id="U-SENDER",
            sender_name="Slack Sender",
            chat_id="C-SUPPORT",
            content="Please answer here",
            thread_ts="1724457000.000001",
        )
    finally:
        ADAPTERS["slack"] = original

    assert result["status"] == "ok"
    assert fake.sent == [{
        "cc_id": config.id,
        "to": "C-SUPPORT",
        "text": "Threaded response",
        "thread_ts": "1724457000.000001",
    }]


@pytest.mark.asyncio
async def test_slack_gateway_propagates_worker_soft_timeout(
    client,
    db_session,
    monkeypatch,
):
    from celery.exceptions import SoftTimeLimitExceeded

    from packages.core.services import channel_gateway

    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_soft_timeout_owner",
    )
    db_session.add_all([
        Channel(
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            type="slack",
            name="Slack timeout",
            config={"channel_config_id": config.id},
            agent_id="manor-master",
            status="active",
        ),
        ChannelContact(
            entity_id=owner["entity_id"],
            channel_config_id=config.id,
            channel_type="slack",
            source_id="U-SENDER",
            user_id=owner["id"],
            role="member",
            status="active",
        ),
    ])
    await db_session.commit()

    async def _timeout_agent(**_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _timeout_agent)

    with pytest.raises(SoftTimeLimitExceeded):
        await channel_gateway.dispatch_inbound(
            entity_id=owner["entity_id"],
            channel_config_id=config.id,
            channel_type="slack",
            sender_id="U-SENDER",
            sender_name="Slack Sender",
            chat_id="C-SUPPORT",
            content="This run must terminate",
            thread_ts="1724457000.000001",
        )


@pytest.mark.asyncio
async def test_slack_gateway_stops_if_connection_disappears_before_worker_run(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_deleted_before_worker",
    )
    account = await db_session.get(OAuthAccount, config.credential_source_id)
    assert account is not None
    await db_session.delete(account)
    await db_session.commit()

    from packages.core.services import channel_gateway

    async def _unexpected_agent_run(**kwargs):
        raise AssertionError("Agent must not run for a deleted Slack connection")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _unexpected_agent_run)
    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        sender_id="U-SENDER",
        sender_name="Slack Sender",
        chat_id="C-SUPPORT",
        content="This event was already queued",
        thread_ts="1724457000.000001",
    )

    assert result == {
        "status": "skipped",
        "reason": "channel credential source unavailable",
    }


@pytest.mark.asyncio
async def test_approved_slack_reply_keeps_the_original_thread(
    client,
    db_session,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_approved_thread",
    )
    conversation = Conversation(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        channel="slack",
        title="Slack approval",
        meta={"channel_config_id": config.id},
    )
    binding = Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Slack approval route",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    )
    contact = ChannelContact(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        source_id="U-SENDER",
        status="active",
    )
    db_session.add_all([conversation, binding, contact])
    await db_session.commit()

    from packages.core.services.channel_outbound_delivery import (
        deliver_approved_external_reply,
    )

    fake = _RecordingSlackAdapter()
    original = ADAPTERS["slack"]
    approval_key = generate_ulid()
    ADAPTERS["slack"] = fake
    try:
        result = await deliver_approved_external_reply(
            db_session,
            entity_id=owner["entity_id"],
            channel_config_id=config.id,
            channel_type="slack",
            channel_conversation_id=conversation.id,
            chat_id="C-SUPPORT",
            text="Approved threaded response",
            channel_binding_id=binding.id,
            channel_contact_id=contact.id,
            agent_id="manor-master",
            agent_subscription_id=None,
            route_snapshot={
                "version": 1,
                "config_workspace_id": None,
                "binding_workspace_id": None,
                "runtime_workspace_id": None,
            },
            workspace_id=None,
            thread_ts="1724457000.000001",
            idempotency_key=approval_key,
        )
    finally:
        ADAPTERS["slack"] = original

    assert result["sent"] is True
    assert fake.sent == [{
        "cc_id": config.id,
        "to": "C-SUPPORT",
        "text": "Approved threaded response",
        "thread_ts": "1724457000.000001",
        "idempotency_key": approval_key,
    }]


@pytest.mark.asyncio
async def test_slack_gateway_does_not_run_an_unbound_agent(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_unbound_agent",
    )
    from packages.core.services import channel_gateway

    async def _unexpected_agent_run(**kwargs):
        raise AssertionError("Agent must not run before Slack is bound")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _unexpected_agent_run)
    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        sender_id="U-SENDER",
        sender_name="Slack Sender",
        chat_id="C-SUPPORT",
        content="No binding yet",
        thread_ts="1724457000.000001",
    )

    assert result == {"status": "unbound", "channel_config_id": config.id}


@pytest.mark.asyncio
async def test_slack_gateway_does_not_run_channel_without_agent(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_binding_without_agent",
    )
    db_session.add(Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Unassigned Slack",
        config={"channel_config_id": config.id},
        status="active",
    ))
    await db_session.commit()
    from packages.core.services import channel_gateway

    async def _unexpected_agent_run(**kwargs):
        raise AssertionError("Agent must not run before Slack has an agent")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _unexpected_agent_run)
    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        sender_id="U-SENDER",
        sender_name="Slack Sender",
        chat_id="C-SUPPORT",
        content="No agent yet",
        thread_ts="1724457000.000001",
    )

    assert result == {
        "status": "unbound",
        "channel_config_id": config.id,
        "reason": "agent_not_bound",
    }


@pytest.mark.asyncio
async def test_slack_gateway_rejects_task_entity_mismatch(
    client,
    db_session,
    monkeypatch,
):
    _owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_gateway_entity_mismatch",
    )
    from packages.core.services import channel_gateway

    async def unexpected_agent_run(**_kwargs):
        raise AssertionError("a cross-entity dispatch must not run an Agent")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", unexpected_agent_run)

    result = await channel_gateway.dispatch_inbound(
        entity_id="another-entity",
        channel_config_id=config.id,
        channel_type="slack",
        sender_id="U-SENDER",
        sender_name="Slack Sender",
        chat_id="C-SUPPORT",
        content="wrong entity",
    )

    assert result == {"status": "skipped", "reason": "entity mismatch"}


@pytest.mark.asyncio
async def test_slack_gateway_rejects_multiple_active_bindings(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_ambiguous_binding",
    )
    db_session.add_all([
        Channel(
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            type="slack",
            name="Slack binding one",
            config={"channel_config_id": config.id},
            agent_id="manor-master",
            status="active",
        ),
        Channel(
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            type="slack",
            name="Slack binding two",
            config={"channel_config_id": config.id},
            agent_id="manor-master",
            status="active",
        ),
    ])
    await db_session.commit()
    from packages.core.services import channel_gateway

    async def _unexpected_agent_run(**kwargs):
        raise AssertionError("Ambiguous Slack bindings must not run an Agent")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _unexpected_agent_run)
    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        sender_id="U-SENDER",
        sender_name="Slack Sender",
        chat_id="C-SUPPORT",
        content="Which binding?",
        thread_ts="1724457000.000001",
    )

    assert result == {
        "status": "unbound",
        "channel_config_id": config.id,
        "reason": "ambiguous_bindings",
    }


@pytest.mark.asyncio
async def test_slack_gateway_rejects_binding_owned_by_another_user(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_binding_owner_mismatch",
    )
    db_session.add(Channel(
        entity_id=owner["entity_id"],
        user_id=generate_ulid(),
        type="slack",
        name="Foreign Slack binding",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    ))
    await db_session.commit()
    from packages.core.services import channel_gateway

    async def _unexpected_agent_run(**kwargs):
        raise AssertionError("Another user's binding must not run the Agent")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _unexpected_agent_run)
    result = await channel_gateway.dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        channel_type="slack",
        sender_id="U-SENDER",
        sender_name="Slack Sender",
        chat_id="C-SUPPORT",
        content="Wrong binding owner",
        thread_ts="1724457000.000001",
    )

    assert result == {
        "status": "unbound",
        "channel_config_id": config.id,
        "reason": "binding_owner_mismatch",
    }


@pytest.mark.asyncio
async def test_slack_thread_is_the_conversation_boundary(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_thread_boundary",
    )
    db_session.add(Channel(
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        type="slack",
        name="Shared Slack thread",
        config={"channel_config_id": config.id},
        agent_id="manor-master",
        status="active",
    ))
    await db_session.commit()
    from packages.core.services import channel_gateway

    async def _fake_run_agent(**kwargs):
        return channel_gateway.ChannelAgentRunResult(content="Thread response")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _fake_run_agent)
    fake = _RecordingSlackAdapter()
    original = ADAPTERS["slack"]
    ADAPTERS["slack"] = fake
    try:
        for sender in ("U-FIRST", "U-SECOND"):
            result = await channel_gateway.dispatch_inbound(
                entity_id=owner["entity_id"],
                channel_config_id=config.id,
                channel_type="slack",
                sender_id=sender,
                sender_name=sender,
                chat_id="C-SHARED",
                content=f"Message from {sender}",
                thread_ts="1724457000.000001",
            )
            assert result["status"] == "ok"
        third_result = await channel_gateway.dispatch_inbound(
            entity_id=owner["entity_id"],
            channel_config_id=config.id,
            channel_type="slack",
            sender_id="U-FIRST",
            sender_name="U-FIRST",
            chat_id="C-SHARED",
            content="A separate thread",
            thread_ts="1724457000.000999",
        )
        assert third_result["status"] == "ok"
    finally:
        ADAPTERS["slack"] = original

    conversations = list((await db_session.execute(
        select(Conversation).where(
            Conversation.entity_id == owner["entity_id"],
            Conversation.channel == "slack",
        )
    )).scalars().all())
    assert len(conversations) == 2
    assert {row.meta["conversation_key"] for row in conversations} == {
        "slack:C-SHARED:thread:1724457000.000001",
        "slack:C-SHARED:thread:1724457000.000999",
    }


@pytest.mark.asyncio
async def test_completed_slack_receipt_is_not_executed_on_worker_redelivery(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_worker_redelivery",
    )
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="slack",
        from_address="U-SENDER",
        to_address="C-SUPPORT",
        content="Run once",
        external_id="Ev-WORKER-REDELIVERY",
        status="received",
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    calls: list[dict[str, Any]] = []

    async def _fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", _fake_dispatch)
    kwargs = {
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "slack",
        "sender_id": "U-SENDER",
        "sender_name": "Slack Sender",
        "chat_id": "C-SUPPORT",
        "content": "Run once",
        "thread_ts": "1724457000.000001",
    }

    first = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="claim-first",
        **kwargs,
    )
    redelivery = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="claim-redelivery",
        **kwargs,
    )

    assert first == {"status": "ok"}
    assert redelivery == {"status": "duplicate_event"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slack_receipt_claim_fences_concurrent_worker_delivery(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_worker_concurrent_claim",
    )
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="slack",
        from_address="U-SENDER",
        to_address="C-SUPPORT",
        content="Run once concurrently",
        external_id="Ev-WORKER-CONCURRENT",
        status="queued",
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _fake_dispatch(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", _fake_dispatch)
    kwargs = {
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "slack",
        "sender_id": "U-SENDER",
        "sender_name": "Slack Sender",
        "chat_id": "C-SUPPORT",
        "content": "Run once concurrently",
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
async def test_slack_receipt_fences_concurrent_delivery_of_same_claim(
    client,
    db_session,
    monkeypatch,
):
    owner, config = await _seed_slack_connection(
        client,
        db_session,
        username="slack_worker_same_concurrent_claim",
    )
    receipt = MessageLog(
        entity_id=owner["entity_id"],
        channel_config_id=config.id,
        direction="inbound",
        channel_type="slack",
        from_address="U-SENDER",
        to_address="C-SUPPORT",
        content="Run one logical claim once",
        external_id="Ev-WORKER-SAME-CONCURRENT",
        status="queued",
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _fake_dispatch(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", _fake_dispatch)
    kwargs = {
        "entity_id": owner["entity_id"],
        "channel_config_id": config.id,
        "channel_type": "slack",
        "sender_id": "U-SENDER",
        "sender_name": "Slack Sender",
        "chat_id": "C-SUPPORT",
        "content": "Run one logical claim once",
    }
    first = asyncio.create_task(
        _dispatch_slack_inbound_once(
            inbound_message_log_id=receipt.id,
            dispatch_claim_id="same-claim",
            **kwargs,
        )
    )
    await entered.wait()

    duplicate = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="same-claim",
        **kwargs,
    )
    release.set()

    assert duplicate == {"status": "duplicate_inflight"}
    assert await first == {"status": "ok"}
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_slack_thread_events_create_one_conversation():
    from packages.core.database import async_session
    from packages.core.services.channel_conversations import (
        get_or_create_channel_conversation,
    )

    entity_id = generate_ulid()
    channel_config_id = generate_ulid()
    conversation_key = "slack:C-CONCURRENT:thread:1724457000.000001"

    async def _create(contact_id: str) -> str:
        async with async_session() as db:
            conversation = await get_or_create_channel_conversation(
                db,
                entity_id=entity_id,
                channel_type="slack",
                channel_config_id=channel_config_id,
                channel_contact_id=contact_id,
                sender_id=contact_id,
                sender_name=contact_id,
                chat_id="C-CONCURRENT",
                agent_id="manor-master",
                conversation_key=conversation_key,
            )
            await db.commit()
            return conversation.id

    conversation_ids = await asyncio.gather(
        _create("U-CONCURRENT-ONE"),
        _create("U-CONCURRENT-TWO"),
    )

    assert len(set(conversation_ids)) == 1
