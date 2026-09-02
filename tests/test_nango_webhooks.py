from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api.routers import nango_webhooks
from packages.core.ai.mcp import nango
from packages.core.services import nango_bootstrap


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def parse_inbound(self, cc, *, headers, query, body):
        self.calls.append(cc.id)
        return SimpleNamespace(
            source_id="U123",
            reply_to="D123",
            content="hello",
            external_message_id="evt-1",
            message_type="text",
            sender_name="Tester",
        )


class _SessionContext:
    def __init__(self, db_session) -> None:
        self.db_session = db_session

    async def __aenter__(self):
        return self.db_session

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_nango_proxy_rejects_unknown_connection_before_upstream_request(
    monkeypatch,
) -> None:
    nango.set_call_context({
        "entity_id": "entity-a",
        "user_id": "user-b",
        "nango_allowed_connection_ids": [
            "entity-a--user-a--gmail--allowed",
        ],
    })

    class _UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("unauthorized connection reached Nango")

    monkeypatch.setattr(nango.httpx, "AsyncClient", _UnexpectedClient)
    try:
        with pytest.raises(PermissionError, match="connection_not_authorized"):
            await nango._proxy(
                {
                    "provider_config_key": "gmail",
                    "connection_id": "entity-a--user-a--gmail--unknown",
                    "method": "GET",
                    "endpoint": "/v1/users/me",
                },
                "secret",
            )
    finally:
        nango.clear_call_context()


@pytest.mark.asyncio
async def test_nango_connection_list_filters_cross_user_connections(monkeypatch) -> None:
    nango.set_call_context({
        "entity_id": "entity-a",
        "user_id": "user-b",
        "nango_allowed_connection_ids": [
            "entity-a--user-a--gmail--allowed",
        ],
    })

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            request = __import__("httpx").Request("GET", url)
            return __import__("httpx").Response(
                200,
                json={"connections": [
                    {
                        "connection_id": "entity-a--user-a--gmail--allowed",
                        "provider_config_key": "gmail",
                    },
                    {
                        "connection_id": "entity-a--user-a--gmail--hidden",
                        "provider_config_key": "gmail",
                    },
                    {
                        "connection_id": "entity-b--user-c--gmail--other",
                        "provider_config_key": "gmail",
                    },
                ]},
                request=request,
            )

    monkeypatch.setattr(nango.httpx, "AsyncClient", _Client)
    try:
        result = await nango._list_connections({}, "secret")
    finally:
        nango.clear_call_context()

    assert result["count"] == 1
    assert result["connections"][0]["connection_id"] == (
        "entity-a--user-a--gmail--allowed"
    )


def _patch_nango_dispatch_dependencies(
    monkeypatch, db_session, adapter, dispatch_calls: list[dict] | None = None,
):
    import packages.core.database as db_module
    from packages.core.services import channels

    monkeypatch.setattr(
        nango_webhooks,
        "async_session",
        lambda: _SessionContext(db_session),
    )
    monkeypatch.setattr(channels, "get_adapter", lambda _channel_type: adapter)
    async def fake_handle(*_args, **_kwargs):
        return SimpleNamespace(id="receipt-1")

    monkeypatch.setattr(nango_webhooks, "handle_inbound_message", fake_handle)
    monkeypatch.setattr(
        nango_webhooks.dispatch_inbound_task,
        "delay",
        lambda **kwargs: dispatch_calls.append(kwargs)
        if dispatch_calls is not None
        else None,
    )
    # Keep the module-level database override explicit in case this test is
    # run without the client fixture having imported the API app first.
    monkeypatch.setattr(db_module, "async_session", lambda: _SessionContext(db_session))

@pytest.mark.asyncio
async def test_nango_dispatch_passes_durable_receipt_to_worker(db_session, monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig

    entity_id = "entity-nango-receipt"
    connection_id = "conn-receipt"
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id="user-a",
        channel_type="slack",
        provider="slack",
        config={"nango": {"connection_id": connection_id}},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    adapter = _FakeAdapter()
    calls: list[dict] = []
    _patch_nango_dispatch_dependencies(monkeypatch, db_session, adapter, calls)

    result = await nango_webhooks._try_dispatch_to_channel(
        provider="slack",
        entity_id=entity_id,
        inner_payload={"event": "message"},
        envelope={"connectionId": connection_id},
    )

    assert result["ok"] is True
    assert calls and calls[0]["inbound_message_log_id"] == "receipt-1"


@pytest.mark.asyncio
async def test_nango_dispatch_rolls_back_receipt_when_queue_is_unavailable(
    db_session, monkeypatch,
):
    import packages.core.database as db_module
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig, MessageLog
    from packages.core.services import channels

    entity_id = "entity-nango-queue-failure"
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id="user-a",
        channel_type="slack",
        provider="slack",
        config={"nango": {"connection_id": "conn-queue-failure"}},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()
    config_id = config.id

    adapter = _FakeAdapter()
    monkeypatch.setattr(
        nango_webhooks,
        "async_session",
        lambda: _SessionContext(db_session),
    )
    monkeypatch.setattr(channels, "get_adapter", lambda _channel_type: adapter)
    monkeypatch.setattr(
        db_module, "async_session", lambda: _SessionContext(db_session),
    )

    def fail_queue(**_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(nango_webhooks.dispatch_inbound_task, "delay", fail_queue)

    result = await nango_webhooks._try_dispatch_to_channel(
        provider="slack",
        entity_id=entity_id,
        inner_payload={"event": "message"},
        envelope={"connectionId": "conn-queue-failure"},
    )

    assert result["ok"] is False
    assert "queue" in result["reason"]
    rows = (await db_session.execute(
        select(MessageLog).where(MessageLog.channel_config_id == config_id)
    )).scalars().all()
    assert rows == []


def test_nango_036_bare_post_accepts_derived_query_token_without_header(monkeypatch) -> None:
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)
    monkeypatch.setenv("NANGO_SECRET_KEY", "fresh-nango-secret")

    assert nango_webhooks._verify_webhook_token(
        nango_bootstrap._derive_webhook_token()
    ) is True


def test_nango_webhook_token_rejects_missing_or_wrong_value_and_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)
    assert nango_webhooks._verify_webhook_token(None) is False
    assert nango_webhooks._verify_webhook_token("wrong") is False

    monkeypatch.delenv("NANGO_CONNECT_HMAC_KEY", raising=False)
    monkeypatch.delenv("NANGO_SECRET_KEY", raising=False)

    assert nango_webhooks._verify_webhook_token("anything") is False


def test_nango_webhook_token_rejects_non_ascii_value(monkeypatch) -> None:
    monkeypatch.setenv("NANGO_CONNECT_HMAC_KEY", "a" * 64)

    assert nango_webhooks._verify_webhook_token("invalid-\N{SNOWMAN}") is False


@pytest.mark.asyncio
async def test_nango_mapped_provider_resolves_registered_channel_adapter(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig

    entity_id = "entity-nango-adapter"
    connection_id = "entity-nango-adapter--user-a--slack"
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id="user-a",
        channel_type="slack",
        provider="slack",
        config={"nango": {"connection_id": connection_id}},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    adapter = _FakeAdapter()
    _patch_nango_dispatch_dependencies(monkeypatch, db_session, adapter)

    result = await nango_webhooks._try_dispatch_to_channel(
        provider="slack",
        entity_id=entity_id,
        inner_payload={"event": "message"},
        envelope={"connectionId": connection_id},
    )

    assert result["ok"] is True
    assert result["channel_config_id"] == config.id
    assert adapter.calls == [config.id]


@pytest.mark.asyncio
async def test_nango_dispatch_selects_exact_connection_without_cross_account_fallback(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig

    entity_id = "entity-nango-routing"
    first = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id="user-a",
        channel_type="slack",
        provider="slack",
        config={"connection_id": "conn-first"},
        credentials={},
        status="active",
    )
    second = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id="user-b",
        channel_type="slack",
        provider="slack",
        config={"nango": {"connection_id": "conn-second"}},
        credentials={},
        status="active",
    )
    db_session.add_all([first, second])
    await db_session.commit()

    adapter = _FakeAdapter()
    _patch_nango_dispatch_dependencies(monkeypatch, db_session, adapter)

    matching = await nango_webhooks._try_dispatch_to_channel(
        provider="slack",
        entity_id=entity_id,
        inner_payload={"event": "message"},
        envelope={"connectionId": "conn-second"},
    )

    assert matching["ok"] is True
    assert matching["channel_config_id"] == second.id

    unmatched = await nango_webhooks._try_dispatch_to_channel(
        provider="slack",
        entity_id=entity_id,
        inner_payload={"event": "message"},
        envelope={"connectionId": "conn-missing"},
    )

    assert unmatched["ok"] is False
    assert "connection" in unmatched["reason"]
    assert adapter.calls == [second.id]
