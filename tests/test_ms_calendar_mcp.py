from __future__ import annotations

import pytest


class _Response:
    status_code = 200
    text = "{}"

    @property
    def is_success(self) -> bool:
        return True

    def json(self) -> dict:
        return {"value": []}


class _Client:
    calls: list[dict] = []
    response = _Response()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, headers=None, json=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "params": params,
            }
        )
        return self.response


@pytest.fixture
def calendar_http(monkeypatch):
    from packages.core.ai.mcp import ms_calendar

    _Client.calls = []
    monkeypatch.setattr(ms_calendar.httpx, "AsyncClient", _Client)
    return ms_calendar


@pytest.mark.asyncio
async def test_list_events_rejects_non_positive_top_without_http(calendar_http):
    result = await calendar_http.call_tool(
        "list_events",
        {"top": 0},
        "ms-calendar-test-token",
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_events_rejects_fractional_top_without_http(calendar_http):
    result = await calendar_http.call_tool(
        "list_events",
        {"top": 1.5},
        "ms-calendar-test-token",
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_event_id_is_path_encoded(calendar_http):
    await calendar_http.call_tool(
        "get_event",
        {"event_id": "event?id#fragment"},
        "ms-calendar-test-token",
    )

    assert _Client.calls[0]["url"].endswith("/me/events/event%3Fid%23fragment")


@pytest.mark.asyncio
async def test_calendar_token_whitespace_is_normalized(calendar_http):
    await calendar_http.call_tool("list_calendars", {}, "  ms-calendar-test-token  ")

    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer ms-calendar-test-token"


@pytest.mark.asyncio
async def test_calendar_rejects_non_string_token_without_http(calendar_http):
    result = await calendar_http.call_tool("list_calendars", {}, {"token": "bad"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_calendar_rejects_non_object_arguments_without_http(calendar_http):
    result = await calendar_http.call_tool("list_calendars", [], "ms-calendar-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []
