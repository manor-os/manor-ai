from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User


async def _auth(client: AsyncClient, username: str = "calendaruser") -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": "Calendar Test Co",
        },
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_calendar_settings_defaults_and_connection_options(client: AsyncClient, db_session):
    headers = await _auth(client, "calendar_defaults")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    db_session.add(
        OAuthAccount(
            user_id=user.id,
            provider="google_calendar",
            provider_user_id="primary@example.com",
            access_token="token",
            profile={"email": "primary@example.com", "is_default": True},
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/calendar-settings", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["settings"]["default_calendar_id"] == "primary"
    assert data["settings"]["conflict_calendar_ids"] == ["primary"]
    assert data["settings"]["visible_calendar_ids"] == ["primary"]
    assert len(data["settings"]["working_hours"]) == 7
    assert data["connections"][0]["display_name"] == "primary@example.com"
    assert data["connections"][0]["is_default"] is True


@pytest.mark.asyncio
async def test_calendar_connection_options_prefer_email_labels(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.mcp import google_calendar

    headers = await _auth(client, "calendar_connection_email")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    db_session.add(
        OAuthAccount(
            user_id=user.id,
            provider="google_calendar",
            provider_user_id="opaque-google-id",
            access_token="google-token",
            profile={},
        )
    )
    db_session.add(
        Integration(
            entity_id=user.entity_id,
            provider="ms_calendar",
            status="active",
            config={"email": "calendar-ms@example.com", "is_default": True},
            credentials={},
        )
    )
    await db_session.commit()

    async def fake_google_call_tool(name: str, arguments: dict, bearer_token: str) -> dict:
        assert name == "list_calendars"
        assert bearer_token == "google-token"
        return {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "items": [
                                {"id": "calendar-google@example.com", "primary": True},
                            ],
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(google_calendar, "call_tool", fake_google_call_tool)

    resp = await client.get("/api/v1/calendar-settings", headers=headers)
    assert resp.status_code == 200, resp.text
    connections = resp.json()["connections"]
    labels = {item["provider"]: item["display_name"] for item in connections}
    assert labels["google_calendar"] == "calendar-google@example.com"
    assert labels["ms_calendar"] == "calendar-ms@example.com"


@pytest.mark.asyncio
async def test_calendar_settings_update_and_booking_links(client: AsyncClient):
    headers = await _auth(client, "calendar_links")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "default_calendar_id": "primary",
            "timezone": "America/Los_Angeles",
            "booking_defaults": {
                "duration_minutes": 45,
                "buffer_after_minutes": 15,
                "min_notice_minutes": 240,
                "rolling_window_days": 45,
            },
        },
    )
    assert update.status_code == 200, update.text
    settings = update.json()["settings"]
    assert settings["provider"] == "google_calendar"
    assert settings["timezone"] == "America/Los_Angeles"
    assert settings["booking_defaults"]["duration_minutes"] == 45

    first = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Discovery Call",
        },
    )
    assert first.status_code == 200, first.text
    first_link = first.json()
    assert first_link["slug"] == "discovery-call"
    assert first_link["duration_minutes"] == 45
    assert first_link["url"].endswith(f"/book/u/{me['id']}/discovery-call")

    second = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Discovery Call",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["slug"] == "discovery-call-2"

    listed = await client.get("/api/v1/calendar-settings", headers=headers)
    links = listed.json()["settings"]["booking_links"]
    assert len(links) == 2
    assert links[0]["url"].endswith(f"/book/u/{me['id']}/discovery-call")

    public = await client.get(f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/discovery-call")
    assert public.status_code == 200, public.text
    assert public.json()["owner_id"] == me["id"]
    assert public.json()["name"] == "Discovery Call"
    assert public.json()["duration_minutes"] == 45

    patched = await client.put(
        f"/api/v1/calendar-settings/booking-links/{first_link['id']}",
        headers=headers,
        json={"name": "Intro Session", "slug": "intro"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["slug"] == "intro"

    deleted = await client.delete(
        f"/api/v1/calendar-settings/booking-links/{first_link['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_account_timezone_update_syncs_calendar_settings(client: AsyncClient):
    headers = await _auth(client, "calendar_timezone_sync")

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "timezone": "America/Los_Angeles",
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["settings"]["timezone"] == "America/Los_Angeles"

    profile = await client.put(
        "/api/v1/auth/me",
        headers=headers,
        json={
            "timezone": "America/New_York",
        },
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["timezone"] == "America/New_York"

    settings = await client.get("/api/v1/calendar-settings", headers=headers)
    assert settings.status_code == 200, settings.text
    assert settings.json()["settings"]["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_public_booking_links_are_owner_scoped(client: AsyncClient):
    first_headers = await _auth(client, "calendar_owner_one")
    first_me = (await client.get("/api/v1/auth/me", headers=first_headers)).json()
    second_headers = await _auth(client, "calendar_owner_two")
    second_me = (await client.get("/api/v1/auth/me", headers=second_headers)).json()

    first = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=first_headers,
        json={
            "name": "Personal meeting",
            "duration_minutes": 30,
        },
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=second_headers,
        json={
            "name": "Personal meeting",
            "duration_minutes": 45,
        },
    )
    assert second.status_code == 200, second.text
    assert first.json()["slug"] == second.json()["slug"] == "personal-meeting"
    assert first.json()["url"].endswith(f"/book/u/{first_me['id']}/personal-meeting")
    assert second.json()["url"].endswith(f"/book/u/{second_me['id']}/personal-meeting")

    first_public = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{first_me['id']}/personal-meeting"
    )
    second_public = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{second_me['id']}/personal-meeting"
    )

    assert first_public.status_code == 200, first_public.text
    assert second_public.status_code == 200, second_public.text
    assert first_public.json()["owner_id"] == first_me["id"]
    assert second_public.json()["owner_id"] == second_me["id"]
    assert first_public.json()["duration_minutes"] == 30
    assert second_public.json()["duration_minutes"] == 45


@pytest.mark.asyncio
async def test_public_booking_flow_confirms_and_blocks_duplicate_slot(client: AsyncClient):
    headers = await _auth(client, "calendar_booking")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    working_hours = [
        {
            "day_of_week": day,
            "enabled": day == target_day.weekday(),
            "start": "09:00",
            "end": "12:00",
        }
        for day in range(7)
    ]

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "",
            "timezone": "UTC",
            "working_hours": working_hours,
            "booking_defaults": {
                "duration_minutes": 30,
                "buffer_after_minutes": 0,
                "min_notice_minutes": 0,
                "rolling_window_days": 10,
            },
        },
    )
    assert update.status_code == 200, update.text

    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Product consult",
            "location_type": "video",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    slug = create.json()["slug"]

    public = await client.get(f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}")
    assert public.status_code == 200, public.text
    slots = public.json()["available_slots"]
    assert slots

    booking = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": slots[0]["starts_at"],
            "guest_name": "Ada Lovelace",
            "guest_email": "ada@example.com",
            "note": "Looking forward to it.",
        },
    )
    assert booking.status_code == 200, booking.text
    confirmation = booking.json()
    assert confirmation["status"] == "confirmed"
    assert confirmation["guest_email"] == "ada@example.com"
    assert confirmation["calendar_event_created"] is False
    assert confirmation["email_sent"] is True

    agenda = await client.get(
        f"/api/v1/calendar-settings/day?day={target_day.isoformat()}",
        headers=headers,
    )
    assert agenda.status_code == 200, agenda.text
    booking_items = [item for item in agenda.json()["items"] if item["source"] == "booking"]
    assert len(booking_items) == 1
    assert booking_items[0]["booking_id"] == confirmation["id"]
    assert booking_items[0]["booking_link_id"] == create.json()["id"]
    assert booking_items[0]["booking_link_slug"] == slug
    assert booking_items[0]["guest_name"] == "Ada Lovelace"
    assert booking_items[0]["guest_email"] == "ada@example.com"
    assert booking_items[0]["starts_at"] == confirmation["starts_at"]
    assert booking_items[0]["ends_at"] == confirmation["ends_at"]

    notifications = await client.get("/api/v1/notifications", headers=headers)
    assert notifications.status_code == 200, notifications.text
    created_notifications = [item for item in notifications.json()["items"] if item["type"] == "booking_confirmed"]
    assert len(created_notifications) == 1
    assert created_notifications[0]["title"] == "New booking: Product consult"
    assert "Ada Lovelace" in created_notifications[0]["content"]
    assert created_notifications[0]["metadata"]["booking_id"] == confirmation["id"]
    assert created_notifications[0]["metadata"]["booking_link_slug"] == slug
    assert created_notifications[0]["metadata"]["guest_email"] == "ada@example.com"
    assert created_notifications[0]["metadata"]["link"] == "/tasks?view=calendar"

    duplicate = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": slots[0]["starts_at"],
            "guest_name": "Grace Hopper",
            "guest_email": "grace@example.com",
        },
    )
    assert duplicate.status_code == 409

    refreshed = await client.get(f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}")
    assert refreshed.status_code == 200, refreshed.text
    refreshed_starts = {slot["starts_at"] for slot in refreshed.json()["available_slots"]}
    assert slots[0]["starts_at"] not in refreshed_starts


@pytest.mark.asyncio
async def test_google_calendar_busy_time_blocks_public_booking_slots(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.mcp import google_calendar

    headers = await _auth(client, "calendar_freebusy_google")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    busy_start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9, minute=30)
    busy_end = busy_start + timedelta(minutes=30)
    working_hours = [
        {
            "day_of_week": day,
            "enabled": day == target_day.weekday(),
            "start": "09:00",
            "end": "12:00",
        }
        for day in range(7)
    ]

    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    db_session.add(
        OAuthAccount(
            user_id=user.id,
            provider="google_calendar",
            provider_user_id="primary@example.com",
            access_token="google-token",
            profile={"email": "primary@example.com", "is_default": True},
        )
    )
    await db_session.commit()

    calls: list[tuple[str, dict, str]] = []

    async def fake_google_call_tool(name: str, arguments: dict, bearer_token: str) -> dict:
        calls.append((name, arguments, bearer_token))
        assert name == "freebusy_query"
        return {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "calendars": {
                                "primary": {
                                    "busy": [
                                        {
                                            "start": busy_start.isoformat().replace("+00:00", "Z"),
                                            "end": busy_end.isoformat().replace("+00:00", "Z"),
                                        }
                                    ],
                                }
                            }
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(google_calendar, "call_tool", fake_google_call_tool)

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "default_calendar_id": "primary",
            "conflict_calendar_ids": ["primary"],
            "timezone": "UTC",
            "working_hours": working_hours,
            "booking_defaults": {
                "duration_minutes": 30,
                "buffer_after_minutes": 0,
                "min_notice_minutes": 0,
                "rolling_window_days": 10,
            },
        },
    )
    assert update.status_code == 200, update.text

    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Google busy check",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    slug = create.json()["slug"]

    public = await client.get(f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}")
    assert public.status_code == 200, public.text
    slot_starts = {slot["starts_at"] for slot in public.json()["available_slots"]}
    assert busy_start.isoformat() not in slot_starts
    assert any(slot["starts_at"] == busy_end.isoformat() for slot in public.json()["available_slots"])
    assert calls[0][0] == "freebusy_query"
    assert calls[0][1]["calendars"] == ["primary"]
    assert calls[0][2] == "google-token"

    booking = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": busy_start.isoformat(),
            "guest_name": "Busy Guest",
            "guest_email": "busy@example.com",
        },
    )
    assert booking.status_code == 409


@pytest.mark.asyncio
async def test_ms_calendar_busy_time_blocks_public_booking_slots(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.mcp import ms_calendar

    headers = await _auth(client, "calendar_freebusy_ms")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    busy_start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=10)
    busy_end = busy_start + timedelta(minutes=30)
    working_hours = [
        {
            "day_of_week": day,
            "enabled": day == target_day.weekday(),
            "start": "09:00",
            "end": "12:00",
        }
        for day in range(7)
    ]

    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    db_session.add(
        OAuthAccount(
            user_id=user.id,
            provider="ms_calendar",
            provider_user_id=me["email"],
            access_token="ms-token",
            profile={"email": me["email"], "is_default": True},
        )
    )
    await db_session.commit()

    calls: list[tuple[str, dict, str]] = []

    async def fake_ms_call_tool(name: str, arguments: dict, bearer_token: str) -> dict:
        calls.append((name, arguments, bearer_token))
        assert name == "get_schedule"
        return {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "value": [
                                {
                                    "scheduleId": me["email"],
                                    "scheduleItems": [
                                        {
                                            "status": "busy",
                                            "start": {
                                                "dateTime": busy_start.strftime("%Y-%m-%dT%H:%M:%S"),
                                                "timeZone": "UTC",
                                            },
                                            "end": {
                                                "dateTime": busy_end.strftime("%Y-%m-%dT%H:%M:%S"),
                                                "timeZone": "UTC",
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(ms_calendar, "call_tool", fake_ms_call_tool)

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "ms_calendar",
            "default_calendar_id": "primary",
            "conflict_calendar_ids": ["primary"],
            "timezone": "UTC",
            "working_hours": working_hours,
            "booking_defaults": {
                "duration_minutes": 30,
                "buffer_after_minutes": 0,
                "min_notice_minutes": 0,
                "rolling_window_days": 10,
            },
        },
    )
    assert update.status_code == 200, update.text

    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "MS busy check",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    slug = create.json()["slug"]

    public = await client.get(f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}")
    assert public.status_code == 200, public.text
    slot_starts = {slot["starts_at"] for slot in public.json()["available_slots"]}
    assert busy_start.isoformat() not in slot_starts
    assert calls[0][0] == "get_schedule"
    assert calls[0][1]["calendars"] == [me["email"]]
    assert calls[0][2] == "ms-token"

    booking = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": busy_start.isoformat(),
            "guest_name": "Busy Guest",
            "guest_email": "busy@example.com",
        },
    )
    assert booking.status_code == 409


@pytest.mark.asyncio
async def test_daily_agenda_uses_scheduled_time_before_deadline(client: AsyncClient):
    headers = await _auth(client, "calendar_agenda")

    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Prepare client brief",
            "deadline": "2026-07-15T14:00:00+00:00",
            "scheduled_at": "2026-07-15T16:00:00+00:00",
            "duration_minutes": 45,
        },
    )
    assert create.status_code == 201, create.text

    agenda = await client.get("/api/v1/calendar-settings/day?day=2026-07-15", headers=headers)
    assert agenda.status_code == 200, agenda.text
    data = agenda.json()
    assert data["date"] == "2026-07-15"
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "Prepare client brief"
    assert data["items"][0]["starts_at"] == "2026-07-15T16:00:00+00:00"
    assert data["items"][0]["ends_at"] == "2026-07-15T16:45:00+00:00"


@pytest.mark.asyncio
async def test_external_calendar_events_read_visible_calendars_and_skip_bookings(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = await _auth(client, "calendar_external_events")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    oauth = OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="primary@example.com",
        access_token="google-token",
        profile={"email": "primary@example.com", "is_default": True},
    )
    db_session.add(oauth)
    await db_session.flush()
    user.preferences = {
        "calendar_settings": {
            "provider": "google_calendar",
            "connection_id": oauth.id,
            "default_calendar_id": "primary",
            "conflict_calendar_ids": ["primary"],
            "visible_calendar_ids": ["primary", "team@example.com"],
            "timezone": "UTC",
            "bookings": [
                {
                    "id": "booking_1",
                    "booking_link_id": "link_1",
                    "booking_link_slug": "intro",
                    "guest_name": "Ada",
                    "guest_email": "ada@example.com",
                    "starts_at": "2026-06-10T15:00:00+00:00",
                    "ends_at": "2026-06-10T15:30:00+00:00",
                    "timezone": "UTC",
                    "calendar_event_id": "duplicate-event",
                }
            ],
        }
    }
    await db_session.commit()

    calls: list[dict] = []

    async def fake_google_call_tool(name: str, args: dict, bearer_token: str):
        calls.append({"name": name, "args": args, "token": bearer_token})
        calendar_id = args["calendar_id"]
        if calendar_id == "primary":
            payload = {
                "summary": "Primary calendar",
                "items": [
                    {
                        "id": "duplicate-event",
                        "summary": "Booking duplicate",
                        "start": {"dateTime": "2026-06-10T15:00:00Z", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-06-10T15:30:00Z", "timeZone": "UTC"},
                    },
                    {
                        "id": "external-1",
                        "summary": "Investor sync",
                        "start": {"dateTime": "2026-06-10T16:00:00Z", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-06-10T16:45:00Z", "timeZone": "UTC"},
                        "organizer": {"email": "host@example.com"},
                        "attendees": [{"email": "one@example.com"}, {"email": "two@example.com"}],
                        "htmlLink": "https://calendar.google.com/event?eid=external-1",
                        "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
                    },
                ],
            }
        else:
            payload = {
                "summary": "Team calendar",
                "items": [
                    {
                        "id": "all-day-1",
                        "summary": "Launch day",
                        "start": {"date": "2026-06-12"},
                        "end": {"date": "2026-06-13"},
                    }
                ],
            }
        return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}

    from packages.core.ai.mcp import google_calendar

    monkeypatch.setattr(google_calendar, "call_tool", fake_google_call_tool)

    resp = await client.get(
        "/api/v1/calendar-settings/events?start=2026-06-01&end=2026-07-01",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "google_calendar"
    assert data["connection_id"] == oauth.id
    assert [call["args"]["calendar_id"] for call in calls] == ["primary", "team@example.com"]
    assert all(call["token"] == "google-token" for call in calls)
    assert all(call["args"]["max_results"] == 250 for call in calls)

    events = data["events"]
    assert [event["external_event_id"] for event in events] == ["external-1", "all-day-1"]
    assert events[0]["title"] == "Investor sync"
    assert events[0]["calendar_name"] == "Primary calendar"
    assert events[0]["meeting_url"] == "https://meet.google.com/aaa-bbbb-ccc"
    assert events[0]["organizer_email"] == "host@example.com"
    assert events[0]["attendee_count"] == 2
    assert events[1]["calendar_id"] == "team@example.com"
    assert events[1]["calendar_name"] == "Team calendar"
    assert events[1]["all_day"] is True
