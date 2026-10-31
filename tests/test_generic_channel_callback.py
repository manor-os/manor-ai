from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from apps.api.routers.channels.generic import DEDICATED_INBOUND_ENDPOINTS
from packages.core.services.channels import registered_channel_types
from packages.core.services.channels.base import NormalizedInbound


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, events, *, config=None, duplicate=None):
        self.events = events
        self.config = config
        self.duplicate = duplicate

    async def execute(self, _statement):
        return _Result(self.config)

    async def scalar(self, _statement):
        return self.duplicate

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class _Adapter:
    async def verify_inbound(self, _cc, *, headers, query, body):
        return True

    async def parse_inbound(self, cc, *, headers, query, body):
        return NormalizedInbound(
            channel_type=cc.channel_type,
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id="user-1",
            reply_to="user-1",
            content="hello",
            external_message_id="message-1",
        )


class _EmailAttachmentAdapter(_Adapter):
    async def parse_inbound(self, cc, *, headers, query, body):
        return NormalizedInbound(
            channel_type=cc.channel_type,
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id="alice@example.com",
            reply_to="alice@example.com",
            content="Please review",
            attachments=[{
                "filename": "resume.txt",
                "content_type": "text/plain",
                "data_base64": "candidates-private-bytes",
            }],
            external_message_id="message-1",
        )


def _request(body: bytes):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/channels/inapp/callback",
        "headers": [],
        "query_string": b"config_id=cc-1",
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_generic_non_durable_channel_commits_before_enqueue(monkeypatch):
    from apps.api.routers.channels import generic

    events: list[str] = []
    config = SimpleNamespace(
        id="cc-1",
        entity_id="entity-1",
        channel_type="inapp",
    )
    sessions = iter([
        _SessionContext(_Session(events, config=config)),
        _SessionContext(_Session(events)),
    ])
    monkeypatch.setattr(generic, "async_session", lambda: next(sessions))
    monkeypatch.setattr(generic, "get_adapter", lambda _channel_type: _Adapter())
    monkeypatch.setattr(
        generic,
        "channel_credential_source_is_available",
        lambda _db, _cc: _async_true(),
    )

    async def handle(_db, **_kwargs):
        return SimpleNamespace(id="receipt-1")

    monkeypatch.setattr(generic, "handle_inbound_message", handle)
    monkeypatch.setattr(
        generic.dispatch_inbound_task,
        "delay",
        lambda **_kwargs: events.append("delay"),
    )

    response = await generic.channel_callback(
        "inapp",
        _request(b"{}"),
        config_id="cc-1",
    )

    assert response.status_code == 200
    assert events == ["commit", "delay"]


@pytest.mark.asyncio
async def test_generic_non_wechat_integrity_error_is_not_duplicate_ack(monkeypatch):
    from apps.api.routers.channels import generic

    events: list[str] = []
    config = SimpleNamespace(
        id="cc-1",
        entity_id="entity-1",
        channel_type="inapp",
    )
    sessions = iter([
        _SessionContext(_Session(events, config=config)),
        _SessionContext(_Session(events)),
        _SessionContext(_Session(events, duplicate="existing-receipt")),
    ])
    monkeypatch.setattr(generic, "async_session", lambda: next(sessions))
    monkeypatch.setattr(generic, "get_adapter", lambda _channel_type: _Adapter())
    monkeypatch.setattr(
        generic,
        "channel_credential_source_is_available",
        lambda _db, _cc: _async_true(),
    )

    async def handle(_db, **_kwargs):
        raise IntegrityError("insert", {}, RuntimeError("constraint"))

    monkeypatch.setattr(generic, "handle_inbound_message", handle)

    with pytest.raises(IntegrityError, match="insert"):
        await generic.channel_callback(
            "inapp",
            _request(b"{}"),
            config_id="cc-1",
        )


@pytest.mark.asyncio
async def test_generic_email_callback_forwards_attachments_to_worker(monkeypatch):
    from apps.api.routers.channels import generic

    events: list[str] = []
    captured_payload = {}
    captured_dispatch = {}
    config = SimpleNamespace(
        id="cc-1",
        entity_id="entity-1",
        channel_type="email",
    )
    sessions = iter([
        _SessionContext(_Session(events, config=config)),
        _SessionContext(_Session(events)),
    ])
    monkeypatch.setattr(generic, "async_session", lambda: next(sessions))
    monkeypatch.setattr(
        generic,
        "get_adapter",
        lambda _channel_type: _EmailAttachmentAdapter(),
    )
    monkeypatch.setattr(
        generic,
        "channel_credential_source_is_available",
        lambda _db, _cc: _async_true(),
    )

    async def handle(_db, **kwargs):
        captured_payload.update(kwargs["payload"])
        return SimpleNamespace(id="receipt-1")

    def delay(**kwargs):
        captured_dispatch.update(kwargs)
        events.append("delay")

    monkeypatch.setattr(generic, "handle_inbound_message", handle)
    monkeypatch.setattr(generic.dispatch_inbound_task, "delay", delay)

    response = await generic.channel_callback(
        "email",
        _request(b"{}"),
        config_id="cc-1",
    )

    assert response.status_code == 200
    assert events == ["commit", "delay"]
    assert captured_payload["attachments"][0]["filename"] == "resume.txt"
    assert captured_dispatch["attachments"] == captured_payload["attachments"]


async def _async_true() -> bool:
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", ["twilio_sms", "twilio_voice", "webchat"])
async def test_generic_route_refuses_channels_with_a_dedicated_endpoint(
    monkeypatch, channel_type,
):
    """Twilio's signature check and the public webchat endpoint cannot be
    bypassed by posting to the generic route instead."""
    from fastapi import HTTPException
    from apps.api.routers.channels import generic

    def unreachable(*_args, **_kwargs):
        raise AssertionError("dedicated-endpoint channels must stop before any DB access")

    monkeypatch.setattr(generic, "async_session", unreachable)

    with pytest.raises(HTTPException) as rejected:
        await generic.channel_callback(
            channel_type, _request(b"{}"), config_id="cc-1",
        )

    assert rejected.value.status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_type",
    sorted(
        set(registered_channel_types())
        - set(DEDICATED_INBOUND_ENDPOINTS)
    ),
)
async def test_generic_route_rejects_unsigned_posts_for_every_reachable_channel(
    monkeypatch, channel_type,
):
    """Every adapter the generic route still serves must prove the sender.

    A forged in-app ``user_id`` or an unauthenticated WeChat Personal post
    is rejected before a MessageLog row or a dispatch exists.
    """
    from fastapi import HTTPException
    from apps.api.routers.channels import generic
    from packages.core.services.channels.base import ChannelAdapter

    async def no_secret(self, cc, *, reason: str) -> dict:
        return {}

    monkeypatch.setattr(ChannelAdapter, "credentials", no_secret)
    config = SimpleNamespace(
        id="cc-1",
        entity_id="entity-1",
        channel_type=channel_type,
        config={},
        credentials={},
    )
    monkeypatch.setattr(
        generic,
        "async_session",
        lambda: _SessionContext(_Session([], config=config)),
    )
    monkeypatch.setattr(
        generic,
        "channel_credential_source_is_available",
        lambda _db, _cc: _async_true(),
    )

    async def unexpected_log(*_args, **_kwargs):
        raise AssertionError("an unverified post must not write a MessageLog")

    def unexpected_dispatch(**_kwargs):
        raise AssertionError("an unverified post must not dispatch")

    monkeypatch.setattr(generic, "handle_inbound_message", unexpected_log)
    monkeypatch.setattr(generic.dispatch_inbound_task, "delay", unexpected_dispatch)

    forged = json.dumps({
        "user_id": "victim-user",
        "session_id": "victim-session",
        "from": "victim",
        "chat_id": "victim",
        "text": "transfer everything",
    }).encode()
    with pytest.raises(HTTPException) as rejected:
        await generic.channel_callback(
            channel_type, _request(forged), config_id="cc-1",
        )

    assert rejected.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "accepted"),
    [("", False), ("Bearer wrong", False), ("Bearer runner-secret", True)],
)
async def test_wechat_personal_inbound_requires_the_runner_bearer(
    monkeypatch, authorization, accepted,
):
    from packages.core.services.channels.base import ChannelAdapter
    from packages.core.services.channels.wechat_personal_adapter import (
        WeChatPersonalChannelAdapter,
    )

    async def runner_credentials(self, cc, *, reason: str) -> dict:
        return {"bearer_token": "runner-secret"}

    monkeypatch.setattr(ChannelAdapter, "credentials", runner_credentials)

    assert await WeChatPersonalChannelAdapter().verify_inbound(
        SimpleNamespace(id="cc-1"),
        headers={"authorization": authorization},
        query={},
        body=b"{}",
    ) is accepted
