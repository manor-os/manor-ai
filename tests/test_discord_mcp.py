from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.core.ai.mcp import discord


_CREDENTIALS = json.dumps(
    {
        "bot_token": "deployment-bot-token",
        "application_id": "discord-app-id",
        "guild_id": "guild-123",
    }
)


def _payload(result: dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(discord.httpx, "AsyncClient", factory)


def test_discord_exposes_only_guild_bounded_v1_tools() -> None:
    assert {tool["name"] for tool in discord.list_tools()} == {
        "get_connection_info",
        "list_channels",
        "send_message",
        "add_reaction",
    }


@pytest.mark.asyncio
async def test_discord_lists_channels_from_connected_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json=[
                {"id": "channel-1", "guild_id": "guild-123", "name": "general"},
                {"id": "channel-2", "guild_id": "guild-123", "name": "qa"},
            ],
        )

    _install_transport(monkeypatch, handler)

    result = await discord.call_tool("list_channels", {}, _CREDENTIALS)

    assert result["isError"] is False
    assert [channel["id"] for channel in _payload(result)] == ["channel-1", "channel-2"]
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v10/guilds/guild-123/channels"
    assert requests[0].headers["Authorization"] == "Bot deployment-bot-token"


@pytest.mark.asyncio
async def test_discord_send_message_validates_channel_guild_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"id": "channel-1", "guild_id": "guild-123"},
            )
        return httpx.Response(
            200,
            request=request,
            json={"id": "message-1", "channel_id": "channel-1"},
        )

    _install_transport(monkeypatch, handler)

    result = await discord.call_tool(
        "send_message",
        {"channel_id": "channel-1", "content": "hello"},
        _CREDENTIALS,
    )

    assert result["isError"] is False
    assert _payload(result)["id"] == "message-1"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v10/channels/channel-1"),
        ("POST", "/api/v10/channels/channel-1/messages"),
    ]
    assert json.loads(requests[1].content) == {"content": "hello"}


@pytest.mark.asyncio
async def test_discord_rejects_channel_outside_connected_guild_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"id": "foreign-channel", "guild_id": "guild-999"},
        )

    _install_transport(monkeypatch, handler)

    result = await discord.call_tool(
        "send_message",
        {"channel_id": "foreign-channel", "content": "must not send"},
        _CREDENTIALS,
    )

    assert result["isError"] is True
    assert "outside the connected Discord Server" in result["content"][0]["text"]
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v10/channels/foreign-channel"),
    ]


@pytest.mark.asyncio
async def test_discord_reaction_is_guild_checked_and_url_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={"id": "channel-1", "guild_id": "guild-123"},
            )
        return httpx.Response(204, request=request)

    _install_transport(monkeypatch, handler)

    result = await discord.call_tool(
        "add_reaction",
        {
            "channel_id": "channel-1",
            "message_id": "message-1",
            "emoji": "ship_it:123",
        },
        _CREDENTIALS,
    )

    assert result["isError"] is False
    assert [(request.method, request.url.raw_path.decode()) for request in requests] == [
        ("GET", "/api/v10/channels/channel-1"),
        (
            "PUT",
            "/api/v10/channels/channel-1/messages/message-1/reactions/ship_it%3A123/@me",
        ),
    ]


@pytest.mark.asyncio
async def test_discord_rejects_malformed_credential_bundle() -> None:
    result = await discord.call_tool("get_connection_info", {}, "not-json")

    assert result["isError"] is True
    assert "credentials are unavailable" in result["content"][0]["text"]
