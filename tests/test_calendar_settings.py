from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User, UserMembership


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
            owner_user_id=user.id,
            provider="ms_calendar",
            status="active",
            config={"email": "calendar-ms@example.com", "is_default": True},
            credentials={"access_token": "microsoft-token"},
        )
    )
    await db_session.commit()

    async def fake_google_list_calendars_data(bearer_token: str) -> list[dict]:
        assert bearer_token == "google-token"
        return [{"id": "calendar-google@example.com", "primary": True}]

    monkeypatch.setattr(
        google_calendar,
        "list_calendars_data",
        fake_google_list_calendars_data,
    )

    resp = await client.get("/api/v1/calendar-settings", headers=headers)
    assert resp.status_code == 200, resp.text
    connections = resp.json()["connections"]
    labels = {item["provider"]: item["display_name"] for item in connections}
    assert labels["google_calendar"] == "calendar-google@example.com"
    assert labels["ms_calendar"] == "calendar-ms@example.com"


@pytest.mark.asyncio
async def test_calendar_accounts_require_owner_or_explicit_share(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.mcp import google_calendar
    from packages.core.models.base import generate_ulid
    from packages.core.services.auth_service import create_access_token, hash_password
    from packages.core.services.integration_access import grant_connection_use

    owner_headers = await _auth(client, "calendar_private_owner")
    owner_data = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    owner = (
        await db_session.execute(select(User).where(User.id == owner_data["id"]))
    ).scalar_one()
    grantee_id = generate_ulid()
    account_id = generate_ulid()
    oauth_account_id = generate_ulid()
    db_session.add_all([
        User(
            id=grantee_id,
            entity_id=owner.entity_id,
            email="calendar_private_grantee@example.test",
            display_name="Calendar Grantee",
            password_hash=hash_password("securepass123"),
            role="member",
            status="active",
        ),
        UserMembership(
            id=generate_ulid(),
            user_id=grantee_id,
            entity_id=owner.entity_id,
            role="member",
            status="active",
            is_primary=True,
        ),
        Integration(
            id=account_id,
            entity_id=owner.entity_id,
            owner_user_id=owner.id,
            provider="google_calendar",
            status="active",
            config={"email": "owner-calendar@example.test"},
            credentials={"access_token": "owner-calendar-token"},
        ),
        OAuthAccount(
            id=oauth_account_id,
            user_id=owner.id,
            provider="google_calendar",
            provider_user_id="owner-oauth-calendar@example.test",
            access_token="owner-oauth-calendar-token",
            profile={"email": "owner-oauth-calendar@example.test"},
        ),
    ])
    await db_session.commit()
    grantee_headers = {
        "Authorization": "Bearer "
        + create_access_token(grantee_id, owner.entity_id, "member")
    }
    provider_calls: list[str] = []

    async def fake_list_calendars_data(token: str) -> list[dict]:
        provider_calls.append(token)
        return [{"id": "primary", "summary": "Owner calendar", "primary": True}]

    monkeypatch.setattr(
        google_calendar,
        "list_calendars_data",
        fake_list_calendars_data,
    )

    private_settings = await client.get(
        "/api/v1/calendar-settings",
        headers=grantee_headers,
    )
    assert private_settings.status_code == 200, private_settings.text
    private_connection_ids = {
        connection["id"] for connection in private_settings.json()["connections"]
    }
    assert account_id not in private_connection_ids
    assert oauth_account_id not in private_connection_ids
    private_calendars = await client.get(
        "/api/v1/calendar-settings/calendars"
        f"?provider=google_calendar&connection_id={account_id}",
        headers=grantee_headers,
    )
    assert private_calendars.status_code == 200, private_calendars.text
    assert private_calendars.json()["calendars"] == []
    assert provider_calls == []
    private_selection = await client.put(
        "/api/v1/calendar-settings",
        headers=grantee_headers,
        json={"provider": "google_calendar", "connection_id": account_id},
    )
    assert private_selection.status_code == 422, private_selection.text

    await grant_connection_use(
        db_session,
        kind="integration",
        connection_id=account_id,
        entity_id=owner.entity_id,
        owner_user_id=owner.id,
        grantee_user_id=grantee_id,
    )
    await grant_connection_use(
        db_session,
        kind="oauth_account",
        connection_id=oauth_account_id,
        entity_id=owner.entity_id,
        owner_user_id=owner.id,
        grantee_user_id=grantee_id,
    )
    await db_session.commit()

    shared_settings = await client.get(
        "/api/v1/calendar-settings",
        headers=grantee_headers,
    )
    assert shared_settings.status_code == 200, shared_settings.text
    shared = next(
        connection
        for connection in shared_settings.json()["connections"]
        if connection["id"] == account_id
    )
    assert shared["display_name"] == "owner-calendar@example.test"
    assert oauth_account_id in {
        connection["id"] for connection in shared_settings.json()["connections"]
    }
    shared_calendars = await client.get(
        "/api/v1/calendar-settings/calendars"
        f"?provider=google_calendar&connection_id={account_id}",
        headers=grantee_headers,
    )
    assert shared_calendars.status_code == 200, shared_calendars.text
    assert shared_calendars.json()["calendars"][0]["name"] == "Owner calendar"
    assert provider_calls == ["owner-calendar-token"]
    shared_oauth_calendars = await client.get(
        "/api/v1/calendar-settings/calendars"
        f"?provider=google_calendar&connection_id={oauth_account_id}",
        headers=grantee_headers,
    )
    assert shared_oauth_calendars.status_code == 200, shared_oauth_calendars.text
    assert provider_calls == [
        "owner-calendar-token",
        "owner-oauth-calendar-token",
    ]


@pytest.mark.asyncio
async def test_calendar_get_canonicalizes_legacy_nango_account_ids(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.base import generate_ulid

    headers = await _auth(client, "calendar_legacy_nango")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = (
        await db_session.execute(select(User).where(User.id == me["id"]))
    ).scalar_one()
    account_id = generate_ulid()
    nango_connection_id = (
        f"{user.entity_id}--{user.id}--google_calendar--{generate_ulid()}"
    )
    db_session.add(Integration(
        id=account_id,
        entity_id=user.entity_id,
        owner_user_id=user.id,
        provider="google_calendar",
        status="active",
        config={
            "email": "legacy-nango@example.test",
            "nango": {
                "connection_id": nango_connection_id,
                "provider_config_key": "google_calendar",
            },
        },
        credentials={},
    ))
    user.preferences = {
        **(user.preferences or {}),
        "calendar_settings": {
            "provider": "google_calendar",
            "connection_id": nango_connection_id,
            "default_calendar_id": "team-calendar",
            "conflict_calendar_ids": ["team-calendar", "busy-calendar"],
            "visible_calendar_ids": ["team-calendar"],
            "conflict_sources": [{
                "provider": "google_calendar",
                "connection_id": nango_connection_id,
                "calendar_ids": ["team-calendar", "busy-calendar"],
            }],
        },
    }
    await db_session.commit()

    response = await client.get("/api/v1/calendar-settings", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    settings = payload["settings"]
    assert settings["connection_id"] == account_id
    assert settings["default_calendar_id"] == "team-calendar"
    assert settings["conflict_calendar_ids"] == [
        "team-calendar",
        "busy-calendar",
    ]
    assert settings["conflict_sources"] == [{
        "provider": "google_calendar",
        "connection_id": account_id,
        "calendar_ids": ["team-calendar", "busy-calendar"],
    }]
    connection = next(
        item for item in payload["connections"] if item["id"] == account_id
    )
    assert connection["provider_user_id"] == account_id
    assert nango_connection_id not in response.text


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

    disconnected = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={"provider": ""},
    )
    assert disconnected.status_code == 200, disconnected.text

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
async def test_public_booking_pages_the_full_rolling_window_by_month(client: AsyncClient):
    headers = await _auth(client, "calendar_long_booking_window")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "",
            "timezone": "UTC",
            "working_hours": [
                {
                    "day_of_week": day,
                    "enabled": day < 5,
                    "start": "09:00",
                    "end": "17:00",
                }
                for day in range(7)
            ],
            "booking_defaults": {
                "duration_minutes": 30,
                "buffer_after_minutes": 0,
                "min_notice_minutes": 0,
                "rolling_window_days": 365,
            },
        },
    )
    assert update.status_code == 200, update.text
    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Long booking window",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 365,
        },
    )
    assert create.status_code == 200, create.text

    legacy = await client.get(
        "/api/v1/calendar-settings/public/booking-links/u/"
        f"{me['id']}/{create.json()['slug']}"
    )
    assert legacy.status_code == 200, legacy.text
    legacy_slots = legacy.json()["available_slots"]
    assert len(legacy_slots) > 720
    assert max(
        datetime.fromisoformat(slot["starts_at"]).date()
        for slot in legacy_slots
    ) >= datetime.now(timezone.utc).date() + timedelta(days=360)

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    first_page = await client.get(
        "/api/v1/calendar-settings/public/booking-links/u/"
        f"{me['id']}/{create.json()['slug']}?month={current_month}&timezone=UTC"
    )
    assert first_page.status_code == 200, first_page.text
    first_page_slots = first_page.json()["available_slots"]
    assert all(
        datetime.fromisoformat(slot["starts_at"]).strftime("%Y-%m") == current_month
        for slot in first_page_slots
    )
    assert len(first_page_slots) <= 31 * 24 * 4
    assert first_page.json()["availability_range_start"]
    assert first_page.json()["availability_range_end"]

    far_date = datetime.now(timezone.utc).date() + timedelta(days=360)
    public = await client.get(
        "/api/v1/calendar-settings/public/booking-links/u/"
        f"{me['id']}/{create.json()['slug']}"
        f"?month={far_date:%Y-%m}&timezone=UTC"
    )

    assert public.status_code == 200, public.text
    slot_dates = [
        datetime.fromisoformat(slot["starts_at"]).date()
        for slot in public.json()["available_slots"]
    ]
    assert max(slot_dates) >= datetime.now(timezone.utc).date() + timedelta(days=360)


def test_booking_validator_uses_the_existing_bookings_buffer():
    from apps.api.routers import calendar_settings as calendar_router

    target = datetime.now(timezone.utc).replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=7)
    existing_link = calendar_router.BookingLink(
        id="buffered-link",
        slug="buffered-link",
        name="Buffered meeting",
        buffer_after_minutes=0,
        min_notice_minutes=0,
    )
    requested_link = calendar_router.BookingLink(
        id="unbuffered-link",
        slug="unbuffered-link",
        name="Unbuffered meeting",
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        min_notice_minutes=0,
    )
    settings = calendar_router.CalendarSettings(
        provider="",
        timezone="UTC",
        working_hours=[
            calendar_router.WorkingHourWindow(
                day_of_week=day,
                enabled=True,
                start="09:00",
                end="17:00",
            )
            for day in range(7)
        ],
        booking_links=[existing_link, requested_link],
        bookings=[
            calendar_router.BookingRecord(
                id="buffered-booking",
                booking_link_id=existing_link.id,
                booking_link_slug=existing_link.slug,
                guest_name="Existing Guest",
                guest_email="existing@example.com",
                starts_at=target.isoformat(),
                ends_at=(target + timedelta(minutes=30)).isoformat(),
                timezone="UTC",
                buffer_before_minutes=0,
                buffer_after_minutes=60,
            )
        ],
    )

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        calendar_router._validate_booking_slot(
            settings,
            requested_link,
            target + timedelta(minutes=30),
        )

    assert exc_info.value.status_code == 409


def test_calendar_provider_factory_covers_every_provider():
    from apps.api.routers import calendar_settings as calendar_router

    assert set(calendar_router._CALENDAR_PROVIDER_ADAPTERS) == set(
        calendar_router.CalendarProvider,
    )
    for provider in calendar_router.CalendarProvider:
        adapter = calendar_router._calendar_provider_adapter(provider)
        assert callable(adapter.busy_lookup)
        assert callable(adapter.visible_event_lookup)
        assert callable(adapter.normalize_event)


def test_microsoft_event_times_are_normalized_to_offset_aware_iso():
    from apps.api.routers import calendar_settings as calendar_router

    event = calendar_router._normalize_ms_event(
        {
            "id": "event-1",
            "subject": "Customer call",
            "start": {"dateTime": "2026-08-22T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-08-22T09:30:00", "timeZone": "UTC"},
        },
        calendar_id="primary",
        calendar_name="Primary",
        settings=calendar_router.CalendarSettings(timezone="America/Los_Angeles"),
    )

    assert event is not None
    assert event.starts_at == "2026-08-22T09:00:00+00:00"
    assert event.ends_at == "2026-08-22T09:30:00+00:00"


@pytest.mark.asyncio
async def test_calendar_event_provider_call_releases_the_db_transaction(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router
    from packages.core.ai.mcp import google_calendar

    class FakeSession:
        def __init__(self):
            self.active = False
            self.rollback_calls = 0

        def in_transaction(self):
            return self.active

        async def rollback(self):
            self.active = False
            self.rollback_calls += 1

    fake_db = FakeSession()

    async def resolve_credential(db, *_args, **_kwargs):
        db.active = True
        return "google-token"

    async def create_event(bearer_token: str, arguments: dict):
        assert bearer_token == "google-token"
        assert arguments["attendees"] == ["guest@example.com"]
        assert not fake_db.in_transaction()
        return {
            "id": "event-1",
            "htmlLink": "https://calendar.google.com/event/event-1",
            "organizer": {"email": "host@example.com"},
        }

    monkeypatch.setattr(
        calendar_router,
        "_resolve_calendar_credential",
        resolve_credential,
    )
    monkeypatch.setattr(google_calendar, "create_event_data", create_event)

    booking = calendar_router.BookingRecord(
        id="booking-1",
        booking_link_id="link-1",
        booking_link_slug="intro",
        guest_name="Guest",
        guest_email="guest@example.com",
        starts_at="2026-08-25T16:00:00+00:00",
        ends_at="2026-08-25T16:30:00+00:00",
        timezone="UTC",
        calendar_provider="google_calendar",
        calendar_account_id="account-1",
        calendar_event_intent=calendar_router.BookingCalendarEventIntent(
            provider="google_calendar",
            account_id="account-1",
            calendar_id="primary",
            summary="Intro with Guest",
            description="Booked via Manor AI.",
        ),
    )

    event = await calendar_router._create_external_calendar_event(
        fake_db,
        SimpleNamespace(id="owner-1"),
        calendar_router.CalendarSettings(timezone="UTC"),
        None,
        booking,
    )

    assert fake_db.rollback_calls == 1
    assert event["calendar_event_id"] == "event-1"
    assert event["host_email"] == "host@example.com"


def test_public_availability_range_uses_the_requested_viewer_month():
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(timezone="America/Los_Angeles")
    starts_at, ends_at = calendar_router._public_availability_range(
        settings,
        calendar_router.BookingLink(id="range-link", slug="range-link", name="Range"),
        "2026-09",
        "Asia/Tokyo",
    )

    assert starts_at.isoformat() == "2026-09-01T00:00:00+09:00"
    assert ends_at.isoformat() == "2026-10-01T00:00:00+09:00"


def test_public_availability_range_preserves_the_legacy_rolling_window():
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(timezone="UTC")
    link = calendar_router.BookingLink(
        id="range-link",
        slug="range-link",
        name="Range",
        rolling_window_days=30,
    )
    starts_at, ends_at = calendar_router._public_availability_range(
        settings,
        link,
        None,
        "Asia/Tokyo",
    )

    assert starts_at.date() == datetime.now(timezone.utc).date()
    assert ends_at - starts_at == timedelta(days=31)


def test_public_availability_range_rejects_malformed_month():
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(timezone="UTC")
    with pytest.raises(calendar_router.HTTPException) as exc_info:
        calendar_router._public_availability_range(
            settings,
            calendar_router.BookingLink(id="range-link", slug="range-link", name="Range"),
            "2026-9",
            "UTC",
        )

    assert exc_info.value.status_code == 400


def test_public_availability_range_rejects_unrepresentable_next_month():
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(timezone="UTC")
    with pytest.raises(calendar_router.HTTPException) as exc_info:
        calendar_router._public_availability_range(
            settings,
            calendar_router.BookingLink(id="range-link", slug="range-link", name="Range"),
            "9999-12",
            "UTC",
        )

    assert exc_info.value.status_code == 400


def test_public_availability_range_rejects_cross_timezone_minimum_year():
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(timezone="America/Los_Angeles")
    with pytest.raises(calendar_router.HTTPException) as exc_info:
        calendar_router._public_availability_range(
            settings,
            calendar_router.BookingLink(id="range-link", slug="range-link", name="Range"),
            "0001-01",
            "Asia/Tokyo",
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_external_busy_lookup_releases_db_before_provider_io(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router
    from packages.core.ai.mcp import google_calendar

    class FakeSession:
        transaction_open = True

        async def rollback(self):
            self.transaction_open = False

    fake_db = FakeSession()

    async def resolve_credential(db, *_args, **_kwargs):
        assert db.transaction_open
        return "calendar-token"

    async def create_snapshot(db, _owner, *, providers):
        assert db.transaction_open
        assert set(providers) == {"google_calendar"}
        return SimpleNamespace()

    async def query_freebusy(token, arguments):
        assert token == "calendar-token"
        assert not fake_db.transaction_open
        return {
            "calendars": {
                calendar_id: {"busy": []}
                for calendar_id in arguments["calendars"]
            }
        }

    monkeypatch.setattr(calendar_router, "_resolve_calendar_credential", resolve_credential)
    monkeypatch.setattr(
        calendar_router._CalendarConnectionSnapshotFactory,
        "create",
        create_snapshot,
    )
    monkeypatch.setattr(google_calendar, "query_freebusy_data", query_freebusy)
    settings = calendar_router.CalendarSettings(
        timezone="UTC",
        conflict_sources=[
            calendar_router.CalendarConflictSource(
                provider="google_calendar",
                calendar_ids=["primary"],
            )
        ],
    )
    starts_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    result = await calendar_router._external_busy_ranges(
        fake_db,
        SimpleNamespace(id="owner"),
        settings,
        calendar_router.BookingLink(id="link", slug="link", name="Link"),
        starts_at,
        starts_at + timedelta(days=1),
    )

    assert result == []


@pytest.mark.asyncio
async def test_external_busy_lookup_reuses_one_connection_snapshot(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router
    from packages.core.services.integration_account_service import IntegrationAccountKind

    class FakeSession:
        async def rollback(self):
            return None

    account_ids = {
        "google_calendar": "google-account",
        "ms_calendar": "microsoft-account",
    }
    rows = {
        account_id: SimpleNamespace(id=account_id)
        for account_id in account_ids.values()
    }
    create_calls: list[set[str]] = []

    async def create_snapshot(_db, _owner, *, providers):
        create_calls.append(set(providers))

        def select(provider, _account_id):
            return SimpleNamespace(
                id=account_ids[str(provider)],
                kind=IntegrationAccountKind.INTEGRATION,
            )

        return SimpleNamespace(
            select=select,
            oauth_rows={},
            integration_rows=rows,
        )

    async def integration_credential(_db, _owner, row, _provider):
        return f"token-{row.id}"

    async def busy_lookup(_token, _calendar_ids, _timezone, _start, _end):
        return []

    monkeypatch.setattr(
        calendar_router._CalendarConnectionSnapshotFactory,
        "create",
        create_snapshot,
    )
    monkeypatch.setattr(
        calendar_router,
        "_integration_credential",
        integration_credential,
    )
    for provider in (
        calendar_router.CalendarProvider.GOOGLE,
        calendar_router.CalendarProvider.MICROSOFT,
    ):
        adapter = calendar_router._CALENDAR_PROVIDER_ADAPTERS[provider]
        monkeypatch.setitem(
            calendar_router._CALENDAR_PROVIDER_ADAPTERS,
            provider,
            adapter.__class__(
                busy_lookup=busy_lookup,
                visible_event_lookup=adapter.visible_event_lookup,
                normalize_event=adapter.normalize_event,
            ),
        )

    settings = calendar_router.CalendarSettings(
        timezone="UTC",
        conflict_sources=[
            calendar_router.CalendarConflictSource(
                provider="google_calendar",
                connection_id="google-account",
                calendar_ids=["primary"],
            ),
            calendar_router.CalendarConflictSource(
                provider="ms_calendar",
                connection_id="microsoft-account",
                calendar_ids=["calendar"],
            ),
        ],
    )
    starts_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    result = await calendar_router._external_busy_ranges(
        FakeSession(),
        SimpleNamespace(id="owner", entity_id="entity"),
        settings,
        calendar_router.BookingLink(id="link", slug="link", name="Link"),
        starts_at,
        starts_at + timedelta(days=1),
    )

    assert result == []
    assert create_calls == [{"google_calendar", "ms_calendar"}]


@pytest.mark.asyncio
async def test_canonical_conflict_sources_reuse_one_connection_snapshot(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router

    create_calls: list[set[str]] = []

    async def create_snapshot(_db, _owner, *, providers):
        create_calls.append(set(providers))
        return SimpleNamespace(
            select=lambda provider, account_id: SimpleNamespace(
                id=f"canonical-{provider}-{account_id}",
            ),
        )

    monkeypatch.setattr(
        calendar_router._CalendarConnectionSnapshotFactory,
        "create",
        create_snapshot,
    )
    sources = [
        calendar_router.CalendarConflictSource(
            provider="google_calendar",
            connection_id="legacy-google",
            calendar_ids=["primary"],
        ),
        calendar_router.CalendarConflictSource(
            provider="ms_calendar",
            connection_id="legacy-ms",
            calendar_ids=["calendar"],
        ),
    ]

    canonical = await calendar_router._canonical_conflict_sources(
        SimpleNamespace(),
        SimpleNamespace(id="owner", entity_id="entity"),
        sources,
    )

    assert [source.connection_id for source in canonical] == [
        "canonical-google_calendar-legacy-google",
        "canonical-ms_calendar-legacy-ms",
    ]
    assert create_calls == [{"google_calendar", "ms_calendar"}]


@pytest.mark.asyncio
async def test_nango_token_network_lookup_runs_after_db_release(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router
    from packages.core.ai.mcp import google_calendar, nango
    from packages.core.services import nango_bridge
    from packages.core.services.integration_account_service import IntegrationAccountKind

    integration = SimpleNamespace(
        id="integration-1",
        entity_id="entity-1",
        owner_user_id="owner-1",
        config={
            "nango": {
                "connection_id": "entity-1--owner-1--google_calendar--nonce",
                "provider_config_key": "google-calendar",
            }
        },
    )

    class FakeSession:
        transaction_open = True

        async def rollback(self):
            self.transaction_open = False

    fake_db = FakeSession()

    async def create_snapshot(_db, _owner, *, providers):
        assert _db is fake_db
        assert set(providers) == {"google_calendar"}
        account = SimpleNamespace(
            id=integration.id,
            kind=IntegrationAccountKind.INTEGRATION,
        )
        return SimpleNamespace(
            select=lambda provider, account_id: account,
            oauth_rows={},
            integration_rows={integration.id: integration},
        )

    async def get_secret(db, entity_id):
        assert db is fake_db
        assert db.transaction_open
        assert entity_id == "entity-1"
        return "nango-secret"

    async def fetch_token(**kwargs):
        assert not fake_db.transaction_open
        assert kwargs == {
            "secret": "nango-secret",
            "provider_config_key": "google-calendar",
            "connection_id": "entity-1--owner-1--google_calendar--nonce",
        }
        return "nango-access-token"

    async def query_freebusy(token, arguments):
        assert not fake_db.transaction_open
        assert token == "nango-access-token"
        return {
            "calendars": {
                calendar_id: {"busy": []}
                for calendar_id in arguments["calendars"]
            }
        }

    monkeypatch.setattr(nango, "get_nango_secret", get_secret)
    monkeypatch.setattr(
        calendar_router._CalendarConnectionSnapshotFactory,
        "create",
        create_snapshot,
    )
    monkeypatch.setattr(nango_bridge, "fetch_nango_access_token", fetch_token)
    monkeypatch.setattr(google_calendar, "query_freebusy_data", query_freebusy)
    settings = calendar_router.CalendarSettings(
        timezone="UTC",
        conflict_sources=[
            calendar_router.CalendarConflictSource(
                provider="google_calendar",
                calendar_ids=["primary"],
            )
        ],
    )
    starts_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    result = await calendar_router._external_busy_ranges(
        fake_db,
        SimpleNamespace(id="owner-1", entity_id="entity-1"),
        settings,
        calendar_router.BookingLink(id="link", slug="link", name="Link"),
        starts_at,
        starts_at + timedelta(days=1),
    )

    assert result == []


def test_calendar_provider_enum_drives_the_busy_lookup_factory():
    from apps.api.routers import calendar_settings as calendar_router

    source = calendar_router.CalendarConflictSource(
        provider="google_calendar",
        calendar_ids=["primary"],
    )

    assert source.provider is calendar_router.CalendarProvider.GOOGLE
    assert (
        calendar_router._calendar_provider_adapter(source.provider).busy_lookup
        is calendar_router._google_calendar_busy_ranges
    )


@pytest.mark.asyncio
async def test_legacy_full_availability_has_a_dedicated_rate_limit(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router

    captured = {}

    async def deny(key, max_requests, window_seconds):
        captured.update(
            key=key,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )
        return SimpleNamespace(allowed=False, retry_after=17)

    monkeypatch.setattr(
        calendar_router._PUBLIC_LEGACY_AVAILABILITY_LIMITER,
        "check",
        deny,
    )
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.10"),
    )

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        await calendar_router._limit_legacy_public_availability(
            request,
            "owner-1",
            "intro",
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "17"}
    assert captured == {
        "key": "calendar-legacy-availability:owner-1:intro:203.0.113.10",
        "max_requests": 10,
        "window_seconds": 60,
    }


@pytest.mark.asyncio
async def test_month_scoped_public_availability_is_rate_limited(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router

    captured = {}

    async def deny(key, max_requests, window_seconds):
        captured.update(
            key=key,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )
        return SimpleNamespace(allowed=False, retry_after=11)

    monkeypatch.setattr(
        calendar_router._PUBLIC_AVAILABILITY_LIMITER,
        "check",
        deny,
    )
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.11"),
    )

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        await calendar_router._limit_public_availability(request, "owner-1")

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "11"}
    assert captured == {
        "key": "calendar-public-availability:owner-1:203.0.113.11",
        "max_requests": 60,
        "window_seconds": 60,
    }


@pytest.mark.asyncio
async def test_public_lookup_rate_limit_uses_only_the_client_ip(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router

    captured = {}

    async def deny(key, max_requests, window_seconds):
        captured.update(
            key=key,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )
        return SimpleNamespace(allowed=False, retry_after=13)

    monkeypatch.setattr(
        calendar_router._PUBLIC_LOOKUP_LIMITER,
        "check",
        deny,
    )
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.11"),
    )

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        await calendar_router._limit_public_lookup(request)

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "13"}
    assert captured == {
        "key": "calendar-public-lookup:203.0.113.11",
        "max_requests": 180,
        "window_seconds": 60,
    }


@pytest.mark.asyncio
async def test_public_availability_resolves_owner_before_creating_limit_bucket(
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router

    calls: list[tuple[str, str]] = []

    async def limit_public_lookup(_request):
        calls.append(("lookup-limit", "ip"))

    async def find_public_booking(_db, slug, *, owner_id=None):
        calls.append(("find", owner_id or slug))
        return (
            SimpleNamespace(id="resolved-owner"),
            calendar_router.CalendarSettings(),
            calendar_router.BookingLink(id="link-1", slug=slug, name="Link"),
        )

    async def limit_public_availability(_request, owner_scope):
        calls.append(("limit", owner_scope))

    async def public_response(*_args, **_kwargs):
        return "response"

    monkeypatch.setattr(calendar_router, "_find_public_booking", find_public_booking)
    monkeypatch.setattr(
        calendar_router,
        "_limit_public_lookup",
        limit_public_lookup,
        raising=False,
    )
    monkeypatch.setattr(
        calendar_router,
        "_limit_public_availability",
        limit_public_availability,
    )
    monkeypatch.setattr(
        calendar_router,
        "_public_booking_link_response",
        public_response,
    )

    result = await calendar_router.get_public_booking_link_for_owner(
        "untrusted-owner",
        "intro",
        SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.11")),
        month="2026-08",
        viewer_timezone="UTC",
        db=SimpleNamespace(),
    )

    assert result == "response"
    assert calls == [
        ("lookup-limit", "ip"),
        ("find", "untrusted-owner"),
        ("limit", "resolved-owner"),
    ]


@pytest.mark.asyncio
async def test_public_lookup_limit_rejects_before_querying_users(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router

    async def deny_lookup(_request):
        raise calendar_router.HTTPException(429, "Too many public link lookups")

    async def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("database lookup must not run after the pre-lookup limit rejects")

    monkeypatch.setattr(
        calendar_router,
        "_limit_public_lookup",
        deny_lookup,
        raising=False,
    )
    monkeypatch.setattr(calendar_router, "_find_public_booking", unexpected_lookup)

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        await calendar_router.get_public_booking_link_for_owner(
            "untrusted-owner",
            "missing",
            SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.11")),
            month="2026-08",
            viewer_timezone="UTC",
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_public_booking_rate_limit_covers_owner_ip_and_email(monkeypatch):
    from apps.api.routers import calendar_settings as calendar_router

    captured: list[tuple[str, int, int]] = []

    async def allow(key, max_requests, window_seconds):
        captured.append((key, max_requests, window_seconds))
        return SimpleNamespace(allowed=True, retry_after=0)

    monkeypatch.setattr(
        calendar_router._PUBLIC_BOOKING_LIMITER,
        "check",
        allow,
    )
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.12"),
    )

    await calendar_router._limit_public_booking(
        request,
        "owner-1",
        "guest@example.com",
    )

    assert captured == [
        ("calendar-public-booking-ip:owner-1:203.0.113.12", 5, 60),
        ("calendar-public-booking-email:owner-1:guest@example.com", 3, 3600),
        ("calendar-public-booking-owner:owner-1", 30, 60),
    ]


def test_booking_validator_rejects_start_outside_generated_slot_grid():
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(
        provider="",
        timezone="UTC",
        working_hours=[
            calendar_router.WorkingHourWindow(
                day_of_week=day,
                enabled=True,
                start="09:10",
                end="17:00",
            )
            for day in range(7)
        ],
    )
    link = calendar_router.BookingLink(
        id="link-id",
        slug="slot-grid",
        name="Slot grid",
        duration_minutes=30,
        min_notice_minutes=0,
        rolling_window_days=10,
    )
    unaligned = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).replace(hour=9, minute=17, second=0, microsecond=0)

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        calendar_router._validate_booking_slot(settings, link, unaligned)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "This time must match an available slot"


@pytest.mark.asyncio
async def test_booking_link_custom_hours_and_time_exclusions(client: AsyncClient):
    headers = await _auth(client, "calendar_link_availability")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    excluded_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    available_day = excluded_day + timedelta(days=1)
    custom_hours = [
        {
            "day_of_week": day,
            "enabled": day in {excluded_day.weekday(), available_day.weekday()},
            "start": "10:00",
            "end": "11:00",
        }
        for day in range(7)
    ]

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "timezone": "UTC",
            "working_hours": [
                {
                    "day_of_week": day,
                    "enabled": True,
                    "start": "09:00",
                    "end": "17:00",
                }
                for day in range(7)
            ],
            "booking_defaults": {
                "duration_minutes": 30,
                "buffer_after_minutes": 0,
                "min_notice_minutes": 0,
                "rolling_window_days": 7,
            },
        },
    )
    assert update.status_code == 200, update.text

    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={"name": "Custom availability", "min_notice_minutes": 0},
    )
    assert create.status_code == 200, create.text
    link = create.json()

    invalid_hours = [
        {
            "day_of_week": day,
            "enabled": day == available_day.weekday(),
            "start": "17:00",
            "end": "09:00",
        }
        for day in range(7)
    ]
    invalid_edit = await client.put(
        f"/api/v1/calendar-settings/booking-links/{link['id']}",
        headers=headers,
        json={
            "name": link["name"],
            "availability_mode": "custom",
            "working_hours": invalid_hours,
        },
    )
    assert invalid_edit.status_code == 422, invalid_edit.text

    edit = await client.put(
        f"/api/v1/calendar-settings/booking-links/{link['id']}",
        headers=headers,
        json={
            "name": link["name"],
            "availability_mode": "custom",
            "working_hours": custom_hours,
            "unavailable_dates": [excluded_day.isoformat()],
            "unavailable_times": [
                {
                    "date": available_day.isoformat(),
                    "start": "10:30",
                    "end": "11:00",
                }
            ],
        },
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["availability_mode"] == "custom"
    assert edit.json()["unavailable_dates"] == [excluded_day.isoformat()]
    assert edit.json()["unavailable_times"] == [
        {
            "date": available_day.isoformat(),
            "start": "10:30",
            "end": "11:00",
        }
    ]

    public = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{link['slug']}"
    )
    assert public.status_code == 200, public.text
    data = public.json()
    assert data["working_hours"] == custom_hours
    slot_starts = [datetime.fromisoformat(item["starts_at"]) for item in data["available_slots"]]
    assert all(item.date() != excluded_day for item in slot_starts)
    available_day_slots = [item for item in slot_starts if item.date() == available_day]
    assert available_day_slots
    assert [(item.hour, item.minute) for item in available_day_slots] == [(10, 0)]

    cleared = await client.put(
        f"/api/v1/calendar-settings/booking-links/{link['id']}",
        headers=headers,
        json={
            "name": link["name"],
            "availability_mode": None,
            "unavailable_dates": None,
            "unavailable_times": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["availability_mode"] == "custom"
    assert cleared.json()["unavailable_dates"] == []
    assert cleared.json()["unavailable_times"] == []
    refreshed = await client.get("/api/v1/calendar-settings", headers=headers)
    assert refreshed.status_code == 200, refreshed.text


@pytest.mark.asyncio
async def test_calendar_options_list_connected_account_calendars(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.mcp import google_calendar

    headers = await _auth(client, "calendar_options")
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
    explicit_oauth = OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="explicit@example.com",
        access_token="explicit-google-token",
        profile={"email": "explicit@example.com", "is_default": False},
    )
    db_session.add(explicit_oauth)
    await db_session.commit()

    async def fake_google_list_calendars_data(bearer_token: str) -> list[dict]:
        assert bearer_token == "google-token"
        return [
            {
                "id": "primary@example.com",
                "summary": "Personal",
                "primary": True,
                "accessRole": "owner",
            },
            {
                "id": "team@example.com",
                "summary": "Team",
                "accessRole": "reader",
            },
        ]

    monkeypatch.setattr(
        google_calendar,
        "list_calendars_data",
        fake_google_list_calendars_data,
    )
    settings_update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "connection_id": explicit_oauth.id,
        },
    )
    assert settings_update.status_code == 200, settings_update.text
    response = await client.get(
        "/api/v1/calendar-settings/calendars?provider=google_calendar&connection_id=",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["calendars"] == [
        {"id": "primary", "name": "Personal", "is_primary": True, "read_only": False},
        {"id": "team@example.com", "name": "Team", "is_primary": False, "read_only": True},
    ]

    async def fail_google_list_calendars_data(bearer_token: str) -> list[dict]:
        raise RuntimeError("calendar provider unavailable")

    monkeypatch.setattr(
        google_calendar,
        "list_calendars_data",
        fail_google_list_calendars_data,
    )
    unavailable = await client.get(
        "/api/v1/calendar-settings/calendars?provider=google_calendar&connection_id=",
        headers=headers,
    )
    assert unavailable.status_code == 503, unavailable.text
    assert unavailable.json()["detail"] == "Calendars could not be loaded"


@pytest.mark.asyncio
async def test_changing_calendar_account_resets_account_scoped_link_calendars(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "calendar_account_switch")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    google_account = OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="google-account",
        access_token="google-token",
    )
    microsoft_account = OAuthAccount(
        user_id=user.id,
        provider="ms_calendar",
        provider_user_id="microsoft-account",
        access_token="microsoft-token",
    )
    db_session.add_all([google_account, microsoft_account])
    await db_session.flush()
    google_connection_id = google_account.id
    microsoft_connection_id = microsoft_account.id
    await db_session.commit()
    first_settings = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "connection_id": google_connection_id,
            "default_calendar_id": "google-team",
            "conflict_calendar_ids": ["google-team", "google-busy"],
            "visible_calendar_ids": ["google-team", "google-visible"],
        },
    )
    assert first_settings.status_code == 200, first_settings.text

    created = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={"name": "Account-bound link", "calendar_id": "google-destination"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["calendar_id"] == "google-destination"

    switched = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "ms_calendar",
            "connection_id": microsoft_connection_id,
        },
    )
    assert switched.status_code == 200, switched.text
    settings = switched.json()["settings"]
    assert settings["default_calendar_id"] == "primary"
    assert settings["conflict_calendar_ids"] == ["primary"]
    assert settings["visible_calendar_ids"] == ["primary"]
    assert settings["booking_links"][0]["calendar_id"] == "primary"


@pytest.mark.asyncio
async def test_canonical_connection_id_migration_does_not_reset_calendars(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "calendar_connection_id_migration")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    oauth = OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="legacy-calendar@example.com",
        access_token="google-token",
        profile={"email": "legacy-calendar@example.com"},
    )
    db_session.add(oauth)
    await db_session.flush()
    oauth_id = oauth.id
    user.preferences = {
        **(user.preferences or {}),
        "calendar_settings": {
            "provider": "google_calendar",
            "connection_id": "legacy-calendar@example.com",
            "default_calendar_id": "team-destination",
            "conflict_calendar_ids": ["team-destination", "busy-calendar"],
            "visible_calendar_ids": ["team-destination", "visible-calendar"],
            "booking_links": [
                {
                    "id": "legacy-link",
                    "slug": "migrated-account-link",
                    "name": "Migrated account link",
                    "calendar_id": "link-destination",
                }
            ],
        },
    }
    await db_session.commit()

    migrated = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "connection_id": oauth_id,
            "track_task_deadlines": False,
        },
    )
    assert migrated.status_code == 200, migrated.text
    settings = migrated.json()["settings"]
    assert settings["connection_id"] == oauth_id
    assert settings["default_calendar_id"] == "team-destination"
    assert settings["conflict_calendar_ids"] == ["team-destination", "busy-calendar"]
    assert settings["visible_calendar_ids"] == ["team-destination", "visible-calendar"]
    assert settings["booking_links"][0]["calendar_id"] == "link-destination"


@pytest.mark.asyncio
async def test_calendar_settings_reject_unknown_conflict_source_account(client: AsyncClient):
    headers = await _auth(client, "calendar_unknown_conflict_source")

    response = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "conflict_sources": [
                {
                    "provider": "google_calendar",
                    "connection_id": "missing-calendar-account",
                    "calendar_ids": ["primary"],
                }
            ]
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Calendar account is not connected"


@pytest.mark.asyncio
async def test_calendar_settings_reject_unknown_primary_account(client: AsyncClient):
    headers = await _auth(client, "calendar_unknown_primary_account")

    response = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "connection_id": "missing-calendar-account",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Calendar account is not connected"


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

    ambiguous_legacy = await client.get(
        "/api/v1/calendar-settings/public/booking-links/personal-meeting"
    )
    assert ambiguous_legacy.status_code == 409, ambiguous_legacy.text
    assert ambiguous_legacy.json()["detail"] == (
        "Booking link is ambiguous. Ask the host for the current link"
    )


@pytest.mark.parametrize("provider", ["google_calendar", "ms_calendar"])
@pytest.mark.asyncio
async def test_booked_meeting_uses_owner_calendar_account_as_host(
    client: AsyncClient,
    db_session,
    monkeypatch,
    provider: str,
):
    from packages.core.ai.mcp import google_calendar, ms_calendar

    username = f"calendar_host_{provider}"
    headers = await _auth(client, username)
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

    host_email = f"host-{provider}@example.com"
    host_token = f"{provider}-host-token"
    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    host_account = OAuthAccount(
        user_id=user.id,
        provider=provider,
        provider_user_id=host_email,
        access_token=host_token,
        profile={"email": host_email, "is_default": True},
    )
    db_session.add(host_account)
    await db_session.flush()
    host_connection_id = host_account.id
    await db_session.commit()

    event_calls: list[tuple[str, dict, str]] = []

    async def fake_create_event(bearer_token: str, arguments: dict) -> dict:
        name = "create_event"
        event_calls.append((name, arguments, bearer_token))
        return {
            "id": f"{provider}-event",
            "htmlLink": "https://calendar.google.com/event",
            "webLink": "https://outlook.office.com/calendar/event",
            "organizer": (
                {"email": host_email}
                if provider == "google_calendar"
                else {"emailAddress": {"address": host_email}}
            ),
        }

    if provider == "google_calendar":
        async def no_google_busy(bearer_token: str, arguments: dict) -> dict:
            assert bearer_token == host_token
            return {
                "calendars": {
                    calendar_id: {"busy": []}
                    for calendar_id in arguments["calendars"]
                }
            }

        monkeypatch.setattr(google_calendar, "query_freebusy_data", no_google_busy)
        monkeypatch.setattr(google_calendar, "create_event_data", fake_create_event)
    else:
        async def no_ms_busy(bearer_token: str, arguments: dict) -> list[dict]:
            assert bearer_token == host_token
            return []

        monkeypatch.setattr(ms_calendar, "list_events_data", no_ms_busy)
        monkeypatch.setattr(ms_calendar, "create_event_data", fake_create_event)

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": provider,
            "connection_id": host_connection_id,
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

    created_link = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": f"{provider} hosted meeting",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert created_link.status_code == 200, created_link.text
    slug = created_link.json()["slug"]

    public = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}"
    )
    assert public.status_code == 200, public.text
    starts_at = public.json()["available_slots"][0]["starts_at"]
    guest_email = "guest-booker@example.com"
    booked = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": starts_at,
            "guest_name": "Guest Booker",
            "guest_email": guest_email,
            "timezone": "UTC",
        },
    )
    assert booked.status_code == 200, booked.text
    assert booked.json()["calendar_event_created"] is True
    assert booked.json()["host_email"] == host_email
    assert booked.json()["email_sent"] is True

    assert len(event_calls) == 1
    name, arguments, bearer_token = event_calls[0]
    assert name == "create_event"
    assert bearer_token == host_token
    assert arguments["attendees"] == [guest_email]
    assert "organizer" not in arguments
    if provider == "google_calendar":
        assert arguments["event_id"]
    else:
        assert arguments["transaction_id"] == booked.json()["id"]

    saved = await client.get("/api/v1/calendar-settings", headers=headers)
    assert saved.status_code == 200, saved.text
    booking = saved.json()["settings"]["bookings"][0]
    assert booking["calendar_account_id"] == host_connection_id
    assert booking["host_email"] == host_email
    assert booking["guest_email"] == guest_email
    assert booking["calendar_event_intent"]["provider"] == provider
    assert booking["calendar_event_intent"]["account_id"] == host_connection_id
    assert booking["calendar_event_intent"]["calendar_id"] == "primary"


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
            "timezone": "America/New_York",
        },
    )
    assert booking.status_code == 200, booking.text
    confirmation = booking.json()
    assert confirmation["status"] == "confirmed"
    assert confirmation["guest_email"] == "ada@example.com"
    assert confirmation["calendar_event_created"] is False
    assert confirmation["email_sent"] is True
    assert confirmation["timezone"] == "America/New_York"

    saved_settings = await client.get("/api/v1/calendar-settings", headers=headers)
    assert saved_settings.status_code == 200, saved_settings.text
    assert saved_settings.json()["settings"]["bookings"][0]["guest_timezone"] == "America/New_York"

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
async def test_confirmed_booking_survives_metadata_persistence_failure(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router

    headers = await _auth(client, "calendar_booking_metadata_failure")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "",
            "timezone": "UTC",
            "working_hours": [
                {
                    "day_of_week": day,
                    "enabled": day == target_day.weekday(),
                    "start": "09:00",
                    "end": "10:00",
                }
                for day in range(7)
            ],
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
            "name": "Durable confirmation",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    slug = create.json()["slug"]
    endpoint = f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}"
    public = await client.get(endpoint)
    assert public.status_code == 200, public.text
    starts_at = public.json()["available_slots"][0]["starts_at"]

    original_save_settings = calendar_router._save_settings
    save_calls = 0

    async def fail_metadata_save(db, owner, settings):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise RuntimeError("metadata write failed")
        return await original_save_settings(db, owner, settings)

    monkeypatch.setattr(calendar_router, "_save_settings", fail_metadata_save)
    booked = await client.post(
        f"{endpoint}/book",
        json={
            "starts_at": starts_at,
            "guest_name": "Durable Guest",
            "guest_email": "durable@example.com",
            "timezone": "UTC",
        },
    )

    assert booked.status_code == 200, booked.text
    assert booked.json()["status"] == "confirmed"
    assert save_calls == 3
    saved = await client.get("/api/v1/calendar-settings", headers=headers)
    assert saved.status_code == 200, saved.text
    assert any(
        booking["id"] == booked.json()["id"]
        and booking["status"] == "confirmed"
        for booking in saved.json()["settings"]["bookings"]
    )


@pytest.mark.asyncio
async def test_pending_google_booking_metadata_reconciles_idempotently(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router
    from packages.core.ai.mcp import google_calendar
    from packages.core.models.base import generate_ulid

    headers = await _auth(client, "calendar_booking_metadata_reconcile")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    account = OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="metadata-host@example.com",
        access_token="google-token",
        profile={"email": "metadata-host@example.com", "is_default": True},
    )
    db_session.add(account)
    await db_session.flush()
    connection_id = account.id
    await db_session.commit()

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "connection_id": connection_id,
            "default_calendar_id": "primary",
            "timezone": "UTC",
        },
    )
    assert update.status_code == 200, update.text
    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={"name": "Metadata recovery"},
    )
    assert create.status_code == 200, create.text

    await db_session.rollback()
    user = (await db_session.execute(
        select(User)
        .where(User.id == me["id"])
        .execution_options(populate_existing=True)
    )).scalar_one()
    settings = calendar_router._normalize_settings(user)
    link = settings.booking_links[0]
    booking = calendar_router.BookingRecord(
        id=generate_ulid(),
        booking_link_id=link.id,
        booking_link_slug=link.slug,
        guest_name="Recovery Guest",
        guest_email="recovery@example.com",
        starts_at="2026-08-25T16:00:00+00:00",
        ends_at="2026-08-25T16:30:00+00:00",
        timezone="UTC",
        calendar_provider="google_calendar",
        calendar_account_id=connection_id,
        calendar_metadata_sync_pending=True,
    )
    booking.calendar_event_intent = calendar_router._calendar_event_intent(
        settings,
        link,
        booking,
        user,
    )
    settings.bookings.append(booking)
    settings.provider = calendar_router.CalendarProvider.MICROSOFT
    settings.connection_id = None
    settings.default_calendar_id = "changed-calendar"
    settings.booking_links = []
    await calendar_router._save_settings(db_session, user, settings)
    await db_session.commit()

    calls: list[tuple[str, dict]] = []

    async def create_existing_google_event(bearer_token: str, arguments: dict) -> dict:
        assert bearer_token == "google-token"
        assert not db_session.in_transaction()
        calls.append(("create_event", arguments))
        raise RuntimeError("event already exists")

    async def get_existing_google_event(bearer_token: str, arguments: dict) -> dict:
        assert bearer_token == "google-token"
        calls.append(("get_event", arguments))
        return {
            "id": arguments["event_id"],
            "htmlLink": "https://calendar.google.com/event/recovered",
            "hangoutLink": "https://meet.google.com/recovered",
            "organizer": {"email": "metadata-host@example.com"},
        }

    monkeypatch.setattr(google_calendar, "create_event_data", create_existing_google_event)
    monkeypatch.setattr(google_calendar, "get_event_data", get_existing_google_event)

    assert await calendar_router.reconcile_booking_metadata(
        db_session,
        user.id,
        booking.id,
    ) is True
    await db_session.rollback()
    refreshed_user = (await db_session.execute(
        select(User)
        .where(User.id == user.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    recovered = next(
        item
        for item in calendar_router._normalize_settings(refreshed_user).bookings
        if item.id == booking.id
    )

    assert [name for name, _arguments in calls] == ["create_event", "get_event"]
    assert calls[0][1]["event_id"] == calls[1][1]["event_id"]
    assert calls[0][1]["calendar_id"] == "primary"
    assert calls[0][1]["summary"] == "Metadata recovery with Recovery Guest"
    assert recovered.calendar_metadata_sync_pending is False
    assert recovered.calendar_event_created is True
    assert recovered.calendar_event_id == calls[1][1]["event_id"]
    assert recovered.host_email == "metadata-host@example.com"


@pytest.mark.asyncio
async def test_concurrent_calendar_writes_preserve_confirmed_booking(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router

    headers = await _auth(client, "calendar_concurrent_booking")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    working_hours = [
        {
            "day_of_week": day,
            "enabled": day == target_day.weekday(),
            "start": "09:00",
            "end": "10:00",
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
            "name": "Concurrent booking",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    slug = create.json()["slug"]
    endpoint = f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}"
    public = await client.get(endpoint)
    assert public.status_code == 200, public.text
    starts_at = public.json()["available_slots"][0]["starts_at"]

    side_effect_started = asyncio.Event()
    release_side_effect = asyncio.Event()
    persisted_before_side_effect: list[bool] = []

    async def delayed_calendar_event(db, owner, settings, link, booking):
        del db, settings, link
        await db_session.rollback()
        stored_owner = (await db_session.execute(
            select(User)
            .where(User.id == owner.id)
            .execution_options(populate_existing=True)
        )).scalar_one()
        stored_bookings = (
            (stored_owner.preferences or {}).get("calendar_settings", {}).get("bookings", [])
        )
        persisted_before_side_effect.append(
            any(item.get("id") == booking.id for item in stored_bookings)
        )
        side_effect_started.set()
        await release_side_effect.wait()
        return {}

    monkeypatch.setattr(
        calendar_router,
        "_create_external_calendar_event",
        delayed_calendar_event,
    )
    first_task = asyncio.create_task(client.post(
        f"{endpoint}/book",
        json={
            "starts_at": starts_at,
            "guest_name": "First Guest",
            "guest_email": "first@example.com",
        },
    ))
    await asyncio.wait_for(side_effect_started.wait(), timeout=5)
    try:
        settings_update = await client.put(
            "/api/v1/calendar-settings",
            headers=headers,
            json={"track_task_deadlines": False},
        )
        second = await client.post(
            f"{endpoint}/book",
            json={
                "starts_at": starts_at,
                "guest_name": "Second Guest",
                "guest_email": "second@example.com",
            },
        )
    finally:
        release_side_effect.set()
    first = await first_task

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert settings_update.status_code == 200, settings_update.text
    assert persisted_before_side_effect == [True]
    saved = await client.get("/api/v1/calendar-settings", headers=headers)
    assert saved.json()["settings"]["track_task_deadlines"] is False
    confirmed = [
        booking
        for booking in saved.json()["settings"]["bookings"]
        if booking["starts_at"] == starts_at and booking["status"] == "confirmed"
    ]
    assert len(confirmed) == 1


@pytest.mark.asyncio
async def test_public_booking_releases_db_before_slow_provider_and_locks_afterward(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router

    headers = await _auth(client, "calendar_booking_lock_order")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "",
            "timezone": "UTC",
            "working_hours": [
                {
                    "day_of_week": day,
                    "enabled": day == target_day.weekday(),
                    "start": "09:00",
                    "end": "10:00",
                }
                for day in range(7)
            ],
            "booking_defaults": {
                "duration_minutes": 30,
                "buffer_after_minutes": 0,
                "min_notice_minutes": 0,
                "rolling_window_days": 10,
            },
        },
    )
    assert update.status_code == 200, update.text
    created = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Lock ordered booking",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert created.status_code == 200, created.text
    endpoint = (
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/"
        f"{created.json()['slug']}"
    )
    public = await client.get(endpoint)
    assert public.status_code == 200, public.text
    starts_at = public.json()["available_slots"][0]["starts_at"]

    order: list[str] = []
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    original_lock = calendar_router._lock_public_booking

    async def tracked_lock(*args, **kwargs):
        order.append("lock")
        return await original_lock(*args, **kwargs)

    async def tracked_busy(db, *_args, **_kwargs):
        assert not db.in_transaction()
        order.append("busy")
        provider_started.set()
        await release_provider.wait()
        return []

    async def no_distributed_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(calendar_router, "_lock_public_booking", tracked_lock)
    monkeypatch.setattr(calendar_router, "_external_busy_ranges", tracked_busy)
    monkeypatch.setattr(calendar_router.cache, "acquire_lease", no_distributed_cache)

    booking_task = asyncio.create_task(client.post(
        f"{endpoint}/book",
        json={
            "starts_at": starts_at,
            "guest_name": "Lock Order Guest",
            "guest_email": "lock-order@example.com",
        },
    ))
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    assert order == ["busy"]

    changed_hours = [
        {
            "day_of_week": day,
            "enabled": day == target_day.weekday(),
            "start": "10:00",
            "end": "11:00",
        }
        for day in range(7)
    ]
    try:
        settings_update = await asyncio.wait_for(client.put(
            "/api/v1/calendar-settings",
            headers=headers,
            json={"working_hours": changed_hours},
        ), timeout=5)
    finally:
        release_provider.set()
    booked = await booking_task

    assert settings_update.status_code == 200, settings_update.text
    assert booked.status_code == 409, booked.text
    assert order == ["busy", "lock"]


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

    calls: list[tuple[dict, str]] = []
    availability_error = {"enabled": False}

    async def fake_google_query_freebusy_data(bearer_token: str, arguments: dict) -> dict:
        calls.append((arguments, bearer_token))
        if availability_error["enabled"]:
            raise RuntimeError("calendar unavailable")
        return {
            "calendars": {
                "primary": {"busy": []},
                "team@example.com": {
                    "busy": [
                        {
                            "start": busy_start.isoformat().replace("+00:00", "Z"),
                            "end": busy_end.isoformat().replace("+00:00", "Z"),
                        }
                    ],
                },
            }
        }

    monkeypatch.setattr(
        google_calendar,
        "query_freebusy_data",
        fake_google_query_freebusy_data,
    )

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "default_calendar_id": "primary",
            "conflict_calendar_ids": ["primary", "team@example.com"],
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
    assert calls[0][0]["calendars"] == ["primary", "team@example.com"]
    assert calls[0][1] == "google-token"

    booking = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": busy_start.isoformat(),
            "guest_name": "Busy Guest",
            "guest_email": "busy@example.com",
        },
    )
    assert booking.status_code == 409

    from apps.api.routers import calendar_settings as calendar_router

    monkeypatch.setattr(calendar_router, "_PUBLIC_AVAILABILITY_CACHE_TTL_SECONDS", 0)
    availability_error["enabled"] = True
    unavailable = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}"
    )
    assert unavailable.status_code == 503, unavailable.text


@pytest.mark.asyncio
async def test_public_availability_coalesces_concurrent_calendar_reads(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router
    from packages.core.ai.mcp import google_calendar

    headers = await _auth(client, "calendar_availability_coalescing")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    working_hours = [
        {
            "day_of_week": day,
            "enabled": day == target_day.weekday(),
            "start": "09:00",
            "end": "10:00",
        }
        for day in range(7)
    ]
    user = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    db_session.add(OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="coalescing@example.com",
        access_token="google-token",
        profile={"email": "coalescing@example.com", "is_default": True},
    ))
    await db_session.commit()

    query_count = 0
    acquired_leases: list[tuple[str, str, int]] = []
    released_leases: list[tuple[str, str]] = []

    async def acquire_lease(key: str, token: str, ttl: int) -> bool:
        acquired_leases.append((key, token, ttl))
        return True

    async def release_lease(key: str, token: str) -> bool:
        released_leases.append((key, token))
        return True

    async def extend_lease(_key: str, _token: str, ttl: int) -> bool:
        assert ttl > 0
        return True

    async def delayed_freebusy(bearer_token: str, arguments: dict) -> dict:
        nonlocal query_count
        assert bearer_token == "google-token"
        query_count += 1
        await asyncio.sleep(0.05)
        return {
            "calendars": {
                calendar_id: {"busy": []}
                for calendar_id in arguments["calendars"]
            }
        }

    monkeypatch.setattr(
        google_calendar,
        "query_freebusy_data",
        delayed_freebusy,
    )
    monkeypatch.setattr(calendar_router.cache, "acquire_lease", acquire_lease)
    monkeypatch.setattr(calendar_router.cache, "extend_lease", extend_lease)
    monkeypatch.setattr(calendar_router.cache, "release_lease", release_lease)
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
            "name": "Coalesced availability",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    endpoint = (
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/"
        f"{create.json()['slug']}"
    )

    first, second = await asyncio.gather(client.get(endpoint), client.get(endpoint))

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert query_count == 1
    assert len(acquired_leases) == 1
    assert released_leases == [
        (acquired_leases[0][0], acquired_leases[0][1]),
    ]


@pytest.mark.asyncio
async def test_public_availability_does_not_publish_after_losing_refresh_lease(
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(provider="", timezone="UTC")
    link = calendar_router.BookingLink(
        id="stale-lease-link",
        slug="stale-lease-link",
        name="Stale lease link",
    )
    range_start = datetime(2026, 8, 23, tzinfo=timezone.utc)
    range_end = datetime(2026, 8, 24, tzinfo=timezone.utc)
    fetched_ranges = [(range_start, range_start + timedelta(minutes=30))]
    published: list[tuple[str, object, int]] = []

    async def cache_miss(_key: str):
        return None

    async def acquire_lease(_key: str, _token: str, ttl: int) -> bool:
        assert ttl > 0
        return True

    async def lose_lease(_key: str, _token: str, ttl: int) -> bool:
        assert ttl > 0
        return False

    async def release_lease(_key: str, _token: str) -> bool:
        return False

    async def fetch_busy_ranges(*_args, **_kwargs):
        return fetched_ranges

    async def publish(key: str, value: object, ttl: int):
        published.append((key, value, ttl))
        return True

    calendar_router._PUBLIC_AVAILABILITY_LOCAL_CACHE.clear()
    monkeypatch.setattr(calendar_router.cache, "get", cache_miss)
    monkeypatch.setattr(calendar_router.cache, "acquire_lease", acquire_lease)
    monkeypatch.setattr(calendar_router.cache, "extend_lease", lose_lease)
    monkeypatch.setattr(calendar_router.cache, "release_lease", release_lease)
    monkeypatch.setattr(calendar_router.cache, "set", publish)
    monkeypatch.setattr(calendar_router, "_external_busy_ranges", fetch_busy_ranges)

    result = await calendar_router._cached_external_busy_ranges(
        None,
        SimpleNamespace(id="stale-lease-owner"),
        settings,
        link,
        range_start,
        range_end,
    )

    assert result == fetched_ranges
    assert published == []
    assert calendar_router._PUBLIC_AVAILABILITY_LOCAL_CACHE == {}


@pytest.mark.asyncio
async def test_public_availability_never_fetches_without_a_held_distributed_lease(
    monkeypatch,
):
    from apps.api.routers import calendar_settings as calendar_router

    settings = calendar_router.CalendarSettings(
        provider="",
        timezone="UTC",
        working_hours=[],
    )
    link = calendar_router.BookingLink(
        id="lease-link",
        slug="lease-link",
        name="Lease link",
    )
    provider_calls = 0

    async def cache_miss(_key: str):
        return None

    async def lease_held(_key: str, _token: str, ttl: int):
        assert ttl > 0
        return False

    async def unexpected_provider_call(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return []

    calendar_router._PUBLIC_AVAILABILITY_LOCAL_CACHE.clear()
    monkeypatch.setattr(calendar_router.cache, "get", cache_miss)
    monkeypatch.setattr(calendar_router.cache, "acquire_lease", lease_held)
    monkeypatch.setattr(calendar_router, "_external_busy_ranges", unexpected_provider_call)
    monkeypatch.setattr(calendar_router, "_PUBLIC_AVAILABILITY_LEASE_WAIT_SECONDS", 0)

    with pytest.raises(calendar_router.HTTPException) as exc_info:
        await calendar_router._cached_external_busy_ranges(
            None,
            SimpleNamespace(id="lease-owner"),
            settings,
            link,
            datetime(2026, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 24, tzinfo=timezone.utc),
        )

    assert exc_info.value.status_code == 503
    assert provider_calls == 0


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
            provider_user_id="connected-calendar@example.com",
            access_token="ms-token",
            profile={"email": "connected-calendar@example.com", "is_default": True},
        )
    )
    await db_session.commit()

    calls: list[tuple[dict, str]] = []
    availability_error = {"enabled": False}

    async def fake_ms_list_events_data(bearer_token: str, arguments: dict) -> list[dict]:
        calls.append((arguments, bearer_token))
        if availability_error["enabled"]:
            raise RuntimeError("calendar unavailable")
        if arguments.get("calendar_id") != "team-calendar":
            return []
        return [
            {
                "id": "busy-event",
                "showAs": "busy",
                "isCancelled": False,
                "start": {
                    "dateTime": busy_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": busy_end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
            }
        ]

    monkeypatch.setattr(ms_calendar, "list_events_data", fake_ms_list_events_data)

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "ms_calendar",
            "default_calendar_id": "primary",
            "conflict_calendar_ids": ["primary", "team-calendar"],
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
    assert "calendar_id" not in calls[0][0]
    assert calls[1][0]["calendar_id"] == "team-calendar"
    assert calls[0][1] == "ms-token"
    assert calls[1][1] == "ms-token"

    booking = await client.post(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}/book",
        json={
            "starts_at": busy_start.isoformat(),
            "guest_name": "Busy Guest",
            "guest_email": "busy@example.com",
        },
    )
    assert booking.status_code == 409

    from apps.api.routers import calendar_settings as calendar_router

    monkeypatch.setattr(calendar_router, "_PUBLIC_AVAILABILITY_CACHE_TTL_SECONDS", 0)
    availability_error["enabled"] = True
    unavailable = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}"
    )
    assert unavailable.status_code == 503, unavailable.text


@pytest.mark.asyncio
async def test_booking_availability_combines_busy_time_across_calendar_accounts(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.mcp import google_calendar, ms_calendar

    headers = await _auth(client, "calendar_cross_account_busy")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    target_day = datetime.now(timezone.utc).date() + timedelta(days=2)
    busy_start = datetime.combine(
        target_day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).replace(hour=10)
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
    google_account = OAuthAccount(
        user_id=user.id,
        provider="google_calendar",
        provider_user_id="google@example.com",
        access_token="google-token",
        profile={"email": "google@example.com", "is_default": True},
    )
    ms_account = OAuthAccount(
        user_id=user.id,
        provider="ms_calendar",
        provider_user_id="ms@example.com",
        access_token="ms-token",
        profile={"email": "ms@example.com", "is_default": True},
    )
    db_session.add_all([google_account, ms_account])
    await db_session.flush()
    google_connection_id = google_account.id
    ms_connection_id = ms_account.id
    await db_session.commit()

    google_calls: list[tuple[dict, str]] = []
    ms_calls: list[tuple[dict, str]] = []

    async def fake_google_query_freebusy_data(bearer_token: str, arguments: dict) -> dict:
        google_calls.append((arguments, bearer_token))
        return {
            "calendars": {
                calendar_id: {"busy": []}
                for calendar_id in arguments["calendars"]
            }
        }

    async def fake_ms_list_events_data(bearer_token: str, arguments: dict) -> list[dict]:
        ms_calls.append((arguments, bearer_token))
        return [
            {
                "id": "busy-on-second-account",
                "showAs": "busy",
                "isCancelled": False,
                "start": {
                    "dateTime": busy_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": busy_end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
            }
        ]

    monkeypatch.setattr(
        google_calendar,
        "query_freebusy_data",
        fake_google_query_freebusy_data,
    )
    monkeypatch.setattr(ms_calendar, "list_events_data", fake_ms_list_events_data)

    update = await client.put(
        "/api/v1/calendar-settings",
        headers=headers,
        json={
            "provider": "google_calendar",
            "connection_id": google_connection_id,
            "default_calendar_id": "primary",
            "conflict_calendar_ids": ["primary"],
            "conflict_sources": [
                {
                    "provider": "google_calendar",
                    "connection_id": google_connection_id,
                    "calendar_ids": ["primary"],
                },
                {
                    "provider": "ms_calendar",
                    "connection_id": ms_connection_id,
                    "calendar_ids": ["primary"],
                },
            ],
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
    assert update.json()["settings"]["conflict_sources"] == [
        {
            "provider": "google_calendar",
            "connection_id": google_connection_id,
            "calendar_ids": ["primary"],
        },
        {
            "provider": "ms_calendar",
            "connection_id": ms_connection_id,
            "calendar_ids": ["primary"],
        },
    ]

    create = await client.post(
        "/api/v1/calendar-settings/booking-links",
        headers=headers,
        json={
            "name": "Cross-account busy check",
            "duration_minutes": 30,
            "buffer_after_minutes": 0,
            "min_notice_minutes": 0,
            "rolling_window_days": 10,
        },
    )
    assert create.status_code == 200, create.text
    slug = create.json()["slug"]

    public = await client.get(
        f"/api/v1/calendar-settings/public/booking-links/u/{me['id']}/{slug}"
    )
    assert public.status_code == 200, public.text
    slot_starts = {slot["starts_at"] for slot in public.json()["available_slots"]}
    assert busy_start.isoformat() not in slot_starts
    assert google_calls[0][0]["calendars"] == ["primary"]
    assert google_calls[0][1] == "google-token"
    assert "calendar_id" not in ms_calls[0][0]
    assert ms_calls[0][1] == "ms-token"

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

    from apps.api.routers import calendar_settings as calendar_router

    original_resolve = calendar_router._resolve_calendar_credential
    request_session = None

    async def tracking_resolve(db, *args, **kwargs):
        nonlocal request_session
        request_session = db
        return await original_resolve(db, *args, **kwargs)

    async def assert_released_before_materialize(credential):
        assert request_session is not None
        assert not request_session.in_transaction()
        return credential

    monkeypatch.setattr(
        calendar_router,
        "_resolve_calendar_credential",
        tracking_resolve,
    )
    monkeypatch.setattr(
        calendar_router,
        "_materialize_calendar_credential",
        assert_released_before_materialize,
    )

    async def fake_google_list_events_data(bearer_token: str, args: dict):
        calls.append({"args": args, "token": bearer_token})
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
        return payload

    from packages.core.ai.mcp import google_calendar

    monkeypatch.setattr(
        google_calendar,
        "list_events_data",
        fake_google_list_events_data,
    )

    resp = await client.get(
        "/api/v1/calendar-settings/events?start=2026-06-01&end=2026-07-01",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "google_calendar"
    assert data["connection_id"] == oauth.id
    assert data["synced_at"]
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
    assert events[1]["starts_at"] == "2026-06-12"
    assert events[1]["ends_at"] == "2026-06-13"

    async def fail_team_calendar(bearer_token: str, args: dict):
        if args["calendar_id"] == "team@example.com":
            raise RuntimeError("team calendar unavailable")
        return await fake_google_list_events_data(bearer_token, args)

    monkeypatch.setattr(google_calendar, "list_events_data", fail_team_calendar)
    failed_sync = await client.get(
        "/api/v1/calendar-settings/events?start=2026-06-01&end=2026-07-01",
        headers=headers,
    )
    assert failed_sync.status_code == 503
    assert failed_sync.json()["detail"] == (
        "External calendars could not be synced. Please try again"
    )
