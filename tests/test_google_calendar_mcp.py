from __future__ import annotations

import pytest


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"items": []}
        self.text = "{}"

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._payload


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
    from packages.core.ai.mcp import google_calendar

    _Client.calls = []
    _Client.response = _Response()
    monkeypatch.setattr(google_calendar.httpx, "AsyncClient", _Client)
    return google_calendar


@pytest.mark.asyncio
async def test_missing_token_is_rejected_without_http(calendar_http):
    result = await calendar_http.call_tool("list_calendars", {}, "")

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_events_rejects_non_positive_limit_without_http(calendar_http):
    result = await calendar_http.call_tool(
        "list_events",
        {"max_results": 0},
        "calendar-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_events_rejects_fractional_limit_without_http(calendar_http):
    result = await calendar_http.call_tool(
        "list_events",
        {"max_results": 1.5},
        "calendar-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_create_event_rejects_blank_summary_without_http(calendar_http):
    result = await calendar_http.call_tool(
        "create_event",
        {"summary": "  ", "start_time": "2026-08-27T10:00:00Z"},
        "calendar-test-token",
    )

    assert result["isError"] is True
    assert "summary" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_freebusy_empty_calendar_list_is_mcp_error_without_http(calendar_http):
    result = await calendar_http.call_tool(
        "freebusy_query",
        {
            "calendars": [],
            "time_min": "2026-08-27T00:00:00Z",
            "time_max": "2026-08-28T00:00:00Z",
        },
        "calendar-test-token",
    )

    assert result["isError"] is True
    assert "at least one calendar" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_create_event_sends_google_bearer_and_send_updates(calendar_http):
    _Client.response = _Response(payload={"id": "event-1"})
    result = await calendar_http.call_tool(
        "create_event",
        {
            "calendar_id": "qa@example.com",
            "summary": "QA event",
            "start_time": "2026-08-27T10:00:00Z",
            "end_time": "2026-08-27T11:00:00Z",
        },
        "calendar-test-token",
    )

    assert result["isError"] is False
    call = _Client.calls[0]
    assert call["url"].endswith("/calendars/qa@example.com/events")
    assert call["headers"]["Authorization"] == "Bearer calendar-test-token"
    assert call["params"] == {"sendUpdates": "all"}
    assert call["json"]["summary"] == "QA event"


@pytest.mark.asyncio
async def test_calendar_token_whitespace_is_normalized(calendar_http):
    await calendar_http.call_tool("list_calendars", {}, "  calendar-test-token  ")

    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer calendar-test-token"


@pytest.mark.asyncio
async def test_calendar_rejects_non_string_token_without_http(calendar_http):
    result = await calendar_http.call_tool("list_calendars", {}, {"token": "bad"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_calendar_rejects_non_object_arguments_without_http(calendar_http):
    result = await calendar_http.call_tool("list_calendars", [], "calendar-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []
