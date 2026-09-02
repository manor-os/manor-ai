from __future__ import annotations

import json

import pytest

from packages.core.ai.mcp import google_calendar, ms_calendar


@pytest.mark.asyncio
async def test_google_calendar_data_listing_paginates_without_mcp_truncation(monkeypatch):
    first_page = [
        {
            "id": f"calendar-{index}@example.com",
            "summary": f"Calendar {index} " + ("x" * 400),
        }
        for index in range(40)
    ]
    assert len(json.dumps({"items": first_page}, indent=2)) > google_calendar._MAX_CHARS
    calls: list[dict] = []

    async def fake_api_json(token, method, path, body=None, params=None):
        assert token == "google-token"
        assert method == "GET"
        assert path == "users/me/calendarList"
        calls.append(dict(params or {}))
        if not params.get("pageToken"):
            return {"items": first_page, "nextPageToken": "next-page"}
        return {"items": [{"id": "last@example.com", "summary": "Last"}]}

    monkeypatch.setattr(google_calendar, "_api_json", fake_api_json)

    calendars = await google_calendar.list_calendars_data("google-token")

    assert len(calendars) == 41
    assert calls == [
        {"maxResults": 250},
        {"maxResults": 250, "pageToken": "next-page"},
    ]


@pytest.mark.asyncio
async def test_google_calendar_event_data_paginates_without_mcp_truncation(monkeypatch):
    first_page = [
        {
            "id": f"event-{index}",
            "summary": "Event " + ("x" * 400),
            "start": {"dateTime": "2026-08-22T09:00:00Z"},
            "end": {"dateTime": "2026-08-22T09:30:00Z"},
        }
        for index in range(40)
    ]
    assert len(json.dumps({"items": first_page}, indent=2)) > google_calendar._MAX_CHARS
    calls: list[dict] = []

    async def fake_api_json(token, method, path, body=None, params=None):
        assert token == "google-token"
        assert method == "GET"
        assert path == "calendars/team@example.com/events"
        calls.append(dict(params or {}))
        if not params.get("pageToken"):
            return {
                "summary": "Team calendar",
                "items": first_page,
                "nextPageToken": "next-page",
            }
        return {"items": [{"id": "last-event", "summary": "Last"}]}

    monkeypatch.setattr(google_calendar, "_api_json", fake_api_json)

    result = await google_calendar.list_events_data(
        "google-token",
        {
            "calendar_id": "team@example.com",
            "time_min": "2026-08-22T00:00:00Z",
            "time_max": "2026-09-22T00:00:00Z",
            "max_results": 250,
        },
    )

    assert result["summary"] == "Team calendar"
    assert len(result["items"]) == 41
    assert calls == [
        {
            "maxResults": 250,
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": "2026-08-22T00:00:00Z",
            "timeMax": "2026-09-22T00:00:00Z",
        },
        {
            "maxResults": 250,
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": "2026-08-22T00:00:00Z",
            "timeMax": "2026-09-22T00:00:00Z",
            "pageToken": "next-page",
        },
    ]


@pytest.mark.asyncio
async def test_google_calendar_encodes_ids_used_as_url_path_segments(monkeypatch):
    paths: list[str] = []

    async def fake_api_json(token, method, path, body=None, params=None):
        assert token == "google-token"
        assert method == "GET"
        paths.append(path)
        return {"items": []}

    monkeypatch.setattr(google_calendar, "_api_json", fake_api_json)

    await google_calendar.list_events_data(
        "google-token",
        {"calendar_id": "en.usa#holiday/group@group.v.calendar.google.com"},
    )
    create_path, _, _ = google_calendar._create_event_request(
        {
            "calendar_id": "en.usa#holiday/group@group.v.calendar.google.com",
            "summary": "Holiday",
            "start_time": "2026-08-22",
        }
    )

    expected = "calendars/en.usa%23holiday%2Fgroup@group.v.calendar.google.com/events"
    assert paths == [expected]
    assert create_path == expected


@pytest.mark.asyncio
async def test_google_freebusy_data_bypasses_mcp_truncation(monkeypatch):
    busy = [
        {
            "start": f"2026-08-{(index % 28) + 1:02d}T09:00:00Z",
            "end": f"2026-08-{(index % 28) + 1:02d}T09:30:00Z",
        }
        for index in range(200)
    ]
    response = {"calendars": {"team@example.com": {"busy": busy}}}
    assert len(json.dumps(response, indent=2)) > google_calendar._MAX_CHARS

    async def fake_api_json(token, method, path, body=None, params=None):
        assert token == "google-token"
        assert method == "POST"
        assert path == "freeBusy"
        assert body == {
            "timeMin": "2026-08-01T00:00:00Z",
            "timeMax": "2026-09-01T00:00:00Z",
            "items": [{"id": "team@example.com"}],
            "timeZone": "UTC",
        }
        assert params is None
        return response

    monkeypatch.setattr(google_calendar, "_api_json", fake_api_json)

    result = await google_calendar.query_freebusy_data(
        "google-token",
        {
            "calendars": ["team@example.com"],
            "time_min": "2026-08-01T00:00:00Z",
            "time_max": "2026-09-01T00:00:00Z",
            "timezone": "UTC",
        },
    )

    assert result == response
    assert len(result["calendars"]["team@example.com"]["busy"]) == 200


@pytest.mark.asyncio
async def test_google_freebusy_data_batches_more_than_fifty_calendars(monkeypatch):
    calendar_ids = [f"calendar-{index}@example.com" for index in range(55)]
    calls: list[list[str]] = []

    async def fake_api_json(token, method, path, body=None, params=None):
        assert token == "google-token"
        assert method == "POST"
        assert path == "freeBusy"
        assert params is None
        batch = [item["id"] for item in body["items"]]
        assert len(batch) <= 50
        calls.append(batch)
        return {
            "timeMin": body["timeMin"],
            "timeMax": body["timeMax"],
            "calendars": {calendar_id: {"busy": []} for calendar_id in batch},
        }

    monkeypatch.setattr(google_calendar, "_api_json", fake_api_json)

    result = await google_calendar.query_freebusy_data(
        "google-token",
        {
            "calendars": calendar_ids,
            "time_min": "2026-08-01T00:00:00Z",
            "time_max": "2026-09-01T00:00:00Z",
            "timezone": "UTC",
        },
    )

    assert calls == [calendar_ids[:50], calendar_ids[50:]]
    assert list(result["calendars"]) == calendar_ids


@pytest.mark.asyncio
async def test_ms_calendar_event_data_paginates_without_mcp_truncation(monkeypatch):
    first_page = [
        {
            "id": "A" * 120 + str(index),
            "showAs": "busy",
            "isCancelled": False,
            "start": {"dateTime": "2026-08-22T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-08-22T09:30:00", "timeZone": "UTC"},
        }
        for index in range(40)
    ]
    assert len(json.dumps({"value": first_page}, indent=2)) > ms_calendar._MAX_CHARS
    calls: list[tuple[str, dict | None]] = []
    next_link = "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=next"

    async def fake_api_json(token, method, path, body=None, params=None):
        assert token == "ms-token"
        assert method == "GET"
        calls.append((path, params))
        if path == "me/calendarView":
            return {"value": first_page, "@odata.nextLink": next_link}
        assert path == next_link
        return {"value": [{"id": "last-event", "showAs": "busy"}]}

    monkeypatch.setattr(ms_calendar, "_api_json", fake_api_json)

    events = await ms_calendar.list_events_data(
        "ms-token",
        {
            "time_min": "2026-08-22T00:00:00Z",
            "time_max": "2026-09-22T00:00:00Z",
            "top": 500,
            "select": "id,start,end,showAs,isCancelled",
        },
    )

    assert len(events) == 41
    assert calls[0][0] == "me/calendarView"
    assert calls[0][1]["$top"] == 500
    assert calls[1] == (next_link, None)


def _mock_ms_http(monkeypatch, handler):
    async_client = ms_calendar.httpx.AsyncClient
    transport = ms_calendar.httpx.MockTransport(handler)
    monkeypatch.setattr(
        ms_calendar.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )


@pytest.mark.asyncio
async def test_ms_calendar_pagination_accepts_graph_next_link(monkeypatch):
    next_link = "https://graph.microsoft.com/v1.0/me/events?$skiptoken=next"
    requested_urls: list[str] = []

    async def handle(request):
        assert request.headers["Authorization"] == "Bearer ms-token"
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return ms_calendar.httpx.Response(200, json={
                "value": [{"id": "first-event"}],
                "@odata.nextLink": next_link,
            })
        return ms_calendar.httpx.Response(200, json={"value": [{"id": "last-event"}]})

    _mock_ms_http(monkeypatch, handle)

    events = await ms_calendar._paged_values("ms-token", "me/events")

    assert events == [{"id": "first-event"}, {"id": "last-event"}]
    assert requested_urls == [
        "https://graph.microsoft.com/v1.0/me/events",
        next_link,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_link",
    [
        "https://attacker.example/collect",
        "http://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=next",
    ],
)
async def test_ms_calendar_pagination_rejects_untrusted_next_link(
    monkeypatch,
    next_link,
):
    requested_urls: list[str] = []

    async def handle(request):
        assert request.headers["Authorization"] == "Bearer ms-token"
        requested_urls.append(str(request.url))
        return ms_calendar.httpx.Response(200, json={
            "value": [],
            "@odata.nextLink": next_link,
        })

    _mock_ms_http(monkeypatch, handle)

    with pytest.raises(RuntimeError, match="untrusted Microsoft Graph URL"):
        await ms_calendar._paged_values("ms-token", "me/events")

    assert requested_urls == ["https://graph.microsoft.com/v1.0/me/events"]


@pytest.mark.asyncio
async def test_google_calendar_create_event_uses_client_event_id(monkeypatch):
    captured: dict = {}

    async def fake_api(token, method, path, body=None, params=None):
        captured.update({
            "token": token,
            "method": method,
            "path": path,
            "body": body,
            "params": params,
        })
        return json.dumps({"id": body["id"]})

    monkeypatch.setattr(google_calendar, "_api", fake_api)
    await google_calendar._create_event("google-token", {
        "summary": "Idempotent booking",
        "start_time": "2026-08-24T09:00:00+00:00",
        "end_time": "2026-08-24T09:30:00+00:00",
        "event_id": "abcdef0123456789",
    })

    assert captured["body"]["id"] == "abcdef0123456789"


@pytest.mark.asyncio
async def test_google_calendar_create_event_data_preserves_oversized_response(monkeypatch):
    payload = {
        "id": "large-google-event",
        "description": "x" * (google_calendar._MAX_CHARS + 1),
        "organizer": {"email": "host@example.com"},
    }
    captured: dict = {}

    async def fake_api_json(token, method, path, body=None, params=None):
        captured.update({
            "token": token,
            "method": method,
            "path": path,
            "body": body,
            "params": params,
        })
        return payload

    monkeypatch.setattr(google_calendar, "_api_json", fake_api_json)
    result = await google_calendar.create_event_data("google-token", {
        "summary": "Large booking",
        "start_time": "2026-08-24T09:00:00+00:00",
        "end_time": "2026-08-24T09:30:00+00:00",
        "description": payload["description"],
        "event_id": "abcdef0123456789",
    })

    assert result == payload
    assert captured["method"] == "POST"
    assert captured["body"]["description"] == payload["description"]


@pytest.mark.asyncio
async def test_ms_calendar_create_event_uses_transaction_id(monkeypatch):
    captured: dict = {}

    async def fake_api(token, method, path, body=None, params=None):
        captured.update({
            "token": token,
            "method": method,
            "path": path,
            "body": body,
            "params": params,
        })
        return json.dumps({"id": "event-id"})

    monkeypatch.setattr(ms_calendar, "_api", fake_api)
    await ms_calendar._create_event("ms-token", {
        "subject": "Idempotent booking",
        "start_time": "2026-08-24T09:00:00",
        "end_time": "2026-08-24T09:30:00",
        "transaction_id": "booking-id",
    })

    assert captured["body"]["transactionId"] == "booking-id"


@pytest.mark.asyncio
async def test_ms_calendar_create_event_data_preserves_oversized_response(monkeypatch):
    payload = {
        "id": "large-ms-event",
        "body": {"content": "x" * (ms_calendar._MAX_CHARS + 1)},
        "organizer": {"emailAddress": {"address": "host@example.com"}},
    }
    captured: dict = {}

    async def fake_api_json(token, method, path, body=None, params=None):
        captured.update({
            "token": token,
            "method": method,
            "path": path,
            "body": body,
            "params": params,
        })
        return payload

    monkeypatch.setattr(ms_calendar, "_api_json", fake_api_json)
    result = await ms_calendar.create_event_data("ms-token", {
        "subject": "Large booking",
        "start_time": "2026-08-24T09:00:00",
        "end_time": "2026-08-24T09:30:00",
        "body": payload["body"]["content"],
        "transaction_id": "booking-id",
    })

    assert result == payload
    assert captured["method"] == "POST"
    assert captured["body"]["body"]["content"] == payload["body"]["content"]
