from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

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


async def _async_true() -> bool:
    return True
