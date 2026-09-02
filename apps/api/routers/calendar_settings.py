"""Personal calendar settings, booking links, and daily agenda endpoints."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from html import escape
from time import monotonic
from typing import Any, Literal
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.cache import cache
from packages.core.models.document import Integration
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.user import OAuthAccount, User
from packages.core.services.notify import notify
from packages.core.services.calendar_settings_lock import lock_calendar_settings_user
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
    RuntimeIntegrationAccount,
    RuntimeIntegrationRegistry,
    load_runtime_integration_registry,
    nango_connection_matches_runtime_scope,
)
from packages.core.services.settings_service import update_user_preferences
from apps.api.deps import get_current_user
from apps.api.middleware.rate_limit import RateLimiter, client_ip

router = APIRouter(prefix="/api/v1/calendar-settings", tags=["calendar-settings"])
logger = logging.getLogger(__name__)

_PREF_KEY = "calendar_settings"
_DEFAULT_COLOR = "#4f7d75"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CALENDAR_AVAILABILITY_ERROR = "Calendar availability could not be verified"
_CALENDAR_LIST_ERROR = "Calendars could not be loaded"
_CALENDAR_SYNC_ERROR = "External calendars could not be synced. Please try again"
_BOOKING_BUSY_ERROR = "Booking is temporarily busy. Please try again"
_PUBLIC_AVAILABILITY_CACHE_TTL_SECONDS = 10
_PUBLIC_AVAILABILITY_LEASE_TTL_SECONDS = 60
_PUBLIC_AVAILABILITY_LEASE_WAIT_SECONDS = 20
_PUBLIC_AVAILABILITY_LEASE_POLL_SECONDS = 0.05
_PUBLIC_BOOKING_LEASE_TTL_SECONDS = 60
_PUBLIC_BOOKING_LEASE_WAIT_SECONDS = 20
_PUBLIC_LOOKUP_RATE_LIMIT = 180
_PUBLIC_LOOKUP_RATE_WINDOW_SECONDS = 60
_PUBLIC_AVAILABILITY_RATE_LIMIT = 60
_PUBLIC_AVAILABILITY_RATE_WINDOW_SECONDS = 60
_PUBLIC_LEGACY_AVAILABILITY_RATE_LIMIT = 10
_PUBLIC_LEGACY_AVAILABILITY_RATE_WINDOW_SECONDS = 60
_PUBLIC_BOOKING_IP_RATE_LIMIT = 5
_PUBLIC_BOOKING_EMAIL_RATE_LIMIT = 3
_PUBLIC_BOOKING_OWNER_RATE_LIMIT = 30
_PUBLIC_BOOKING_RATE_WINDOW_SECONDS = 60
_PUBLIC_BOOKING_EMAIL_RATE_WINDOW_SECONDS = 3600
_PUBLIC_AVAILABILITY_LOCAL_CACHE_MAX = 256
_PUBLIC_AVAILABILITY_LOCAL_CACHE: dict[
    str,
    tuple[float, list[list[str]]],
] = {}
_PUBLIC_AVAILABILITY_LOCKS: WeakValueDictionary = WeakValueDictionary()
_PUBLIC_BOOKING_LOCKS: WeakValueDictionary = WeakValueDictionary()
_PUBLIC_LOOKUP_LIMITER = RateLimiter()
_PUBLIC_AVAILABILITY_LIMITER = RateLimiter()
_PUBLIC_LEGACY_AVAILABILITY_LIMITER = RateLimiter()
_PUBLIC_BOOKING_LIMITER = RateLimiter()


class CalendarProvider(StrEnum):
    GOOGLE = "google_calendar"
    MICROSOFT = "ms_calendar"


_SUPPORTED_PROVIDERS = {"", *(provider.value for provider in CalendarProvider)}


class WorkingHourWindow(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    enabled: bool = True
    start: str = "09:00"
    end: str = "17:00"


def _validate_working_hour_ranges(windows: list[WorkingHourWindow] | None) -> None:
    for window in windows or []:
        if not window.enabled:
            continue
        try:
            starts_at = time.fromisoformat(window.start)
            ends_at = time.fromisoformat(window.end)
        except ValueError as exc:
            raise ValueError("enabled working hours must use valid HH:MM times") from exc
        if starts_at >= ends_at:
            raise ValueError("end must be after start for enabled working hours")


class BookingDefaults(BaseModel):
    duration_minutes: int = Field(default=30, ge=5, le=480)
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=10, ge=0, le=240)
    min_notice_minutes: int = Field(default=120, ge=0, le=43200)
    rolling_window_days: int = Field(default=30, ge=1, le=365)


class BookingTimeExclusion(BaseModel):
    date: str
    start: str
    end: str

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise ValueError("date must use YYYY-MM-DD") from exc

    @field_validator("start", "end")
    @classmethod
    def validate_time(cls, value: str) -> str:
        try:
            parsed = time.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("time must use HH:MM") from exc
        return parsed.strftime("%H:%M")

    @model_validator(mode="after")
    def validate_range(self) -> "BookingTimeExclusion":
        if time.fromisoformat(self.start) >= time.fromisoformat(self.end):
            raise ValueError("end must be after start")
        return self


class BookingLink(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None = None
    duration_minutes: int = Field(default=30, ge=5, le=480)
    location_type: Literal["none", "phone", "video", "in_person", "custom"] = "video"
    location_detail: str | None = None
    calendar_id: str | None = None
    enabled: bool = True
    color: str = _DEFAULT_COLOR
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=10, ge=0, le=240)
    min_notice_minutes: int = Field(default=120, ge=0, le=43200)
    rolling_window_days: int = Field(default=30, ge=1, le=365)
    availability_mode: Literal["default", "custom"] = "default"
    working_hours: list[WorkingHourWindow] = Field(default_factory=list)
    unavailable_dates: list[str] = Field(default_factory=list)
    unavailable_times: list[BookingTimeExclusion] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    url: str | None = None

    @field_validator("unavailable_dates")
    @classmethod
    def validate_unavailable_dates(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            try:
                parsed = date.fromisoformat(str(value))
            except ValueError as exc:
                raise ValueError("unavailable_dates must use YYYY-MM-DD") from exc
            text = parsed.isoformat()
            if text not in normalized:
                normalized.append(text)
        return normalized


class BookingLinkWrite(BaseModel):
    slug: str | None = None
    name: str
    description: str | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    location_type: Literal["none", "phone", "video", "in_person", "custom"] | None = None
    location_detail: str | None = None
    calendar_id: str | None = None
    enabled: bool | None = None
    color: str | None = None
    buffer_before_minutes: int | None = Field(default=None, ge=0, le=240)
    buffer_after_minutes: int | None = Field(default=None, ge=0, le=240)
    min_notice_minutes: int | None = Field(default=None, ge=0, le=43200)
    rolling_window_days: int | None = Field(default=None, ge=1, le=365)
    availability_mode: Literal["default", "custom"] | None = None
    working_hours: list[WorkingHourWindow] | None = None
    unavailable_dates: list[str] | None = None
    unavailable_times: list[BookingTimeExclusion] | None = None

    @field_validator("unavailable_dates")
    @classmethod
    def validate_unavailable_dates(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        for value in values:
            try:
                parsed = date.fromisoformat(str(value))
            except ValueError as exc:
                raise ValueError("unavailable_dates must use YYYY-MM-DD") from exc
            text = parsed.isoformat()
            if text not in normalized:
                normalized.append(text)
        return normalized

    @model_validator(mode="after")
    def validate_working_hour_ranges(self) -> "BookingLinkWrite":
        _validate_working_hour_ranges(self.working_hours)
        return self


class BookingCalendarEventIntent(BaseModel):
    provider: CalendarProvider
    account_id: str | None = None
    calendar_id: str = "primary"
    summary: str
    description: str
    location: str | None = None
    create_online_meeting: bool = False
    timezone: str = "UTC"


class BookingRecord(BaseModel):
    id: str
    booking_link_id: str
    booking_link_slug: str
    guest_name: str
    guest_email: str
    note: str | None = None
    starts_at: str
    ends_at: str
    timezone: str
    guest_timezone: str | None = None
    buffer_before_minutes: int | None = Field(default=None, ge=0, le=240)
    buffer_after_minutes: int | None = Field(default=None, ge=0, le=240)
    status: Literal["confirmed", "cancelled"] = "confirmed"
    calendar_provider: CalendarProvider | None = None
    calendar_account_id: str | None = None
    calendar_event_intent: BookingCalendarEventIntent | None = None
    host_email: str | None = None
    calendar_event_id: str | None = None
    calendar_event_url: str | None = None
    meeting_url: str | None = None
    calendar_event_created: bool = False
    calendar_metadata_sync_pending: bool = False
    email_sent: bool = False
    created_at: str | None = None


class CalendarConflictSource(BaseModel):
    provider: CalendarProvider
    connection_id: str | None = None
    calendar_ids: list[str]

    @field_validator("connection_id")
    @classmethod
    def normalize_connection_id(cls, value: str | None) -> str | None:
        return str(value or "").strip() or None

    @field_validator("calendar_ids")
    @classmethod
    def normalize_calendar_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            calendar_id = str(value or "").strip()
            if calendar_id and calendar_id not in normalized:
                normalized.append(calendar_id)
        if not normalized:
            raise ValueError("calendar_ids must include at least one calendar")
        return normalized


class CalendarSettings(BaseModel):
    provider: CalendarProvider | Literal[""] = ""
    connection_id: str | None = None
    default_calendar_id: str = "primary"
    conflict_calendar_ids: list[str] = Field(default_factory=lambda: ["primary"])
    conflict_sources: list[CalendarConflictSource] = Field(default_factory=list)
    visible_calendar_ids: list[str] = Field(default_factory=lambda: ["primary"])
    timezone: str = "UTC"
    working_hours: list[WorkingHourWindow] = Field(default_factory=list)
    booking_defaults: BookingDefaults = Field(default_factory=BookingDefaults)
    booking_links: list[BookingLink] = Field(default_factory=list)
    bookings: list[BookingRecord] = Field(default_factory=list)
    auto_create_events_from_tasks: bool = False
    track_task_deadlines: bool = True
    track_scheduled_tasks: bool = True


class CalendarSettingsWrite(BaseModel):
    provider: CalendarProvider | Literal[""] | None = None
    connection_id: str | None = None
    default_calendar_id: str | None = None
    conflict_calendar_ids: list[str] | None = None
    conflict_sources: list[CalendarConflictSource] | None = None
    visible_calendar_ids: list[str] | None = None
    timezone: str | None = None
    working_hours: list[WorkingHourWindow] | None = None
    booking_defaults: BookingDefaults | None = None
    auto_create_events_from_tasks: bool | None = None
    track_task_deadlines: bool | None = None
    track_scheduled_tasks: bool | None = None

    @model_validator(mode="after")
    def validate_working_hour_ranges(self) -> "CalendarSettingsWrite":
        _validate_working_hour_ranges(self.working_hours)
        return self


class CalendarConnectionOption(BaseModel):
    id: str
    provider: str
    display_name: str
    provider_user_id: str
    is_default: bool = False
    expires_at: str | None = None


class CalendarOption(BaseModel):
    id: str
    name: str
    is_primary: bool = False
    read_only: bool = False


class CalendarOptionsResponse(BaseModel):
    provider: str
    connection_id: str | None = None
    calendars: list[CalendarOption] = Field(default_factory=list)


class CalendarSettingsResponse(BaseModel):
    settings: CalendarSettings
    connections: list[CalendarConnectionOption]


class DailyAgendaItem(BaseModel):
    id: str
    source: Literal["task", "booking"]
    title: str
    starts_at: str
    ends_at: str | None = None
    status: str | None = None
    priority: int | None = None
    task_id: str | None = None
    workspace_id: str | None = None
    booking_id: str | None = None
    booking_link_id: str | None = None
    booking_link_slug: str | None = None
    guest_name: str | None = None
    guest_email: str | None = None


class DailyAgendaResponse(BaseModel):
    date: str
    timezone: str
    items: list[DailyAgendaItem]


class ExternalCalendarEvent(BaseModel):
    id: str
    provider: str
    calendar_id: str
    calendar_name: str | None = None
    external_event_id: str
    title: str
    starts_at: str
    ends_at: str | None = None
    timezone: str | None = None
    all_day: bool = False
    status: str | None = None
    location: str | None = None
    description: str | None = None
    organizer_email: str | None = None
    attendee_count: int | None = None
    calendar_event_url: str | None = None
    meeting_url: str | None = None


class ExternalCalendarEventsResponse(BaseModel):
    provider: str
    connection_id: str | None = None
    timezone: str
    range_start: str
    range_end: str
    synced_at: str
    events: list[ExternalCalendarEvent]


class AvailableSlot(BaseModel):
    starts_at: str
    ends_at: str
    label: str


class PublicBookingLinkResponse(BaseModel):
    owner_id: str
    slug: str
    name: str
    description: str | None = None
    duration_minutes: int
    location_type: str
    location_detail: str | None = None
    owner_name: str | None = None
    timezone: str
    working_hours: list[WorkingHourWindow]
    availability_range_start: str
    availability_range_end: str
    available_slots: list[AvailableSlot] = Field(default_factory=list)


class PublicBookingRequest(BaseModel):
    starts_at: str
    guest_name: str = Field(min_length=1, max_length=160)
    guest_email: str = Field(min_length=3, max_length=320)
    note: str | None = Field(default=None, max_length=2000)
    timezone: str | None = Field(default=None, max_length=100)


class BookingConfirmationResponse(BaseModel):
    id: str
    status: str
    booking_link_slug: str
    guest_name: str
    guest_email: str
    starts_at: str
    ends_at: str
    timezone: str
    calendar_event_created: bool
    host_email: str | None = None
    calendar_event_url: str | None = None
    meeting_url: str | None = None
    email_sent: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_working_hours() -> list[dict[str, Any]]:
    return [
        {"day_of_week": day, "enabled": day < 5, "start": "09:00", "end": "17:00"}
        for day in range(7)
    ]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or f"booking-{generate_ulid().lower()[-8:]}"


def _unique_slug(raw: str | None, links: list[BookingLink], *, excluding_id: str | None = None) -> str:
    base = _slugify(raw or "booking")
    used = {link.slug for link in links if link.id != excluding_id}
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _parse_time_hhmm(value: str, fallback: time) -> time:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return time(hour=hour, minute=minute)
    except Exception:
        return fallback


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _task_schedule_start(task: Task, settings: CalendarSettings) -> datetime | None:
    details = task.details or {}
    scheduled = _parse_datetime(details.get("scheduled_at")) if settings.track_scheduled_tasks else None
    if scheduled:
        return scheduled
    return task.deadline if settings.track_task_deadlines and task.deadline else None


def _booking_url(request: Request, owner: User, slug: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/book/u/{owner.id}/{slug}"


def _with_booking_urls(settings: CalendarSettings, owner: User, request: Request) -> CalendarSettings:
    copy = settings.model_copy(deep=True)
    copy.booking_links = [
        link.model_copy(update={"url": _booking_url(request, owner, link.slug)})
        for link in copy.booking_links
    ]
    return copy


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _timezone_name(name: str | None, fallback: str) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        return fallback
    try:
        ZoneInfo(candidate)
        return candidate
    except ZoneInfoNotFoundError:
        return fallback


def _raw_calendar_settings(user: User) -> dict[str, Any]:
    prefs = user.preferences or {}
    raw = prefs.get(_PREF_KEY)
    return raw if isinstance(raw, dict) else {}


def _normalize_settings(user: User) -> CalendarSettings:
    raw = dict(_raw_calendar_settings(user))
    raw.setdefault("timezone", user.timezone or "UTC")
    raw.setdefault("default_calendar_id", "primary")
    raw.setdefault("conflict_calendar_ids", ["primary"])
    raw.setdefault("conflict_sources", [])
    raw.setdefault("visible_calendar_ids", raw.get("conflict_calendar_ids") or ["primary"])
    raw.setdefault("working_hours", _default_working_hours())
    raw.setdefault("booking_defaults", {})
    raw.setdefault("booking_links", [])
    raw.setdefault("bookings", [])

    if raw.get("provider") not in _SUPPORTED_PROVIDERS:
        raw["provider"] = ""
    if not isinstance(raw.get("conflict_calendar_ids"), list):
        raw["conflict_calendar_ids"] = ["primary"]
    raw_sources: list[dict[str, Any]] = []
    for item in raw.get("conflict_sources") or []:
        try:
            raw_sources.append(CalendarConflictSource.model_validate(item).model_dump())
        except Exception:
            logger.debug("Skipping invalid calendar conflict source", exc_info=True)
    raw["conflict_sources"] = raw_sources
    if not isinstance(raw.get("visible_calendar_ids"), list):
        raw["visible_calendar_ids"] = raw.get("conflict_calendar_ids") or ["primary"]

    links: list[dict[str, Any]] = []
    defaults = BookingDefaults.model_validate(raw.get("booking_defaults") or {})
    for idx, item in enumerate(raw.get("booking_links") or []):
        if not isinstance(item, dict):
            continue
        merged = {
            "id": item.get("id") or generate_ulid(),
            "slug": item.get("slug") or _slugify(item.get("name") or f"booking-{idx + 1}"),
            "name": item.get("name") or "Booking link",
            "duration_minutes": item.get("duration_minutes") or defaults.duration_minutes,
            "buffer_before_minutes": item.get("buffer_before_minutes") if item.get("buffer_before_minutes") is not None else defaults.buffer_before_minutes,
            "buffer_after_minutes": item.get("buffer_after_minutes") if item.get("buffer_after_minutes") is not None else defaults.buffer_after_minutes,
            "min_notice_minutes": item.get("min_notice_minutes") if item.get("min_notice_minutes") is not None else defaults.min_notice_minutes,
            "rolling_window_days": item.get("rolling_window_days") if item.get("rolling_window_days") is not None else defaults.rolling_window_days,
            **item,
        }
        links.append(BookingLink.model_validate(merged).model_dump())
    raw["booking_links"] = links

    bookings: list[dict[str, Any]] = []
    for item in raw.get("bookings") or []:
        if not isinstance(item, dict):
            continue
        try:
            bookings.append(BookingRecord.model_validate(item).model_dump())
        except Exception:
            logger.debug("Skipping invalid booking record", exc_info=True)
    raw["bookings"] = bookings
    return CalendarSettings.model_validate(raw)


async def _save_settings(db: AsyncSession, user: User, settings: CalendarSettings) -> CalendarSettings:
    await update_user_preferences(db, user.id, {_PREF_KEY: settings.model_dump()})
    return settings


def _first_nonempty(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _first_email(*values: object) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text and _EMAIL_RE.match(text):
            return text
    return None


def _calendar_account_label(provider: str, *values: object) -> str:
    email = _first_email(*values)
    if email:
        return email
    display = _first_nonempty(*values)
    if display:
        return display
    if provider == "google_calendar":
        return "Google Calendar account"
    if provider == "ms_calendar":
        return "Outlook Calendar account"
    return "Calendar account"


async def _calendar_email_from_token(provider: str, token: str | None) -> str | None:
    if not token:
        return None
    try:
        if provider == "google_calendar":
            from packages.core.ai.mcp import google_calendar

            calendars = await google_calendar.list_calendars_data(token)
            primary = next((item for item in calendars if item.get("primary")), None)
            return _first_email(
                (primary or {}).get("id"),
                *((item or {}).get("id") for item in calendars),
            )
        if provider == "ms_calendar":
            from packages.core.ai.mcp import outlook

            result = await outlook.call_tool("get_profile", {}, token)
            if result.get("isError"):
                return None
            data = _json_result(result)
            return _first_email(data.get("mail"), data.get("userPrincipalName"))
    except Exception:
        logger.debug("Calendar account email lookup failed", exc_info=True)
    return None


@dataclass(frozen=True, slots=True)
class _CalendarConnectionSnapshot:
    """Authorized account catalog plus source rows for calendar operations."""

    registry: RuntimeIntegrationRegistry
    oauth_rows: dict[str, OAuthAccount]
    integration_rows: dict[str, Integration]

    def accounts_for(self, provider: str) -> tuple[RuntimeIntegrationAccount, ...]:
        return self.registry.accounts_for(provider)

    def select(
        self,
        provider: str,
        account_id: str | None = None,
    ) -> RuntimeIntegrationAccount | None:
        accounts = self.accounts_for(provider)
        normalized = str(account_id or "").strip()
        if normalized:
            exact = next(
                (account for account in accounts if account.id == normalized),
                None,
            )
            if exact is not None:
                return exact
            for account in accounts:
                if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT:
                    row = self.oauth_rows.get(account.id)
                    if row is not None and row.provider_user_id == normalized:
                        return account
                    continue
                row = self.integration_rows.get(account.id)
                config = (
                    row.config
                    if row is not None and isinstance(row.config, dict)
                    else {}
                )
                nango = (
                    config.get("nango")
                    if isinstance(config.get("nango"), dict)
                    else {}
                )
                if str(nango.get("connection_id") or "").strip() == normalized:
                    return account
            return None

        binding = self.registry.integration(provider)
        if binding is None or binding.requires_explicit_account:
            return None
        return accounts[0] if accounts else None


class _CalendarConnectionSnapshotFactory:
    """Load callable accounts once, then hydrate only their persistence rows."""

    @staticmethod
    async def create(
        db: AsyncSession,
        user: User,
        *,
        providers: set[str] | tuple[str, ...] | list[str],
    ) -> _CalendarConnectionSnapshot:
        registry = await load_runtime_integration_registry(
            db,
            user_id=user.id,
            entity_id=user.entity_id,
            provider_keys=providers,
        )
        accounts = [
            account
            for provider in providers
            for account in registry.accounts_for(provider)
        ]
        oauth_ids = {
            account.id
            for account in accounts
            if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT
        }
        integration_ids = {
            account.id
            for account in accounts
            if account.kind is IntegrationAccountKind.INTEGRATION
        }
        oauth_rows = (
            list((await db.execute(
                select(OAuthAccount).where(OAuthAccount.id.in_(oauth_ids))
            )).scalars().all())
            if oauth_ids
            else []
        )
        integration_rows = (
            list((await db.execute(
                select(Integration).where(
                    Integration.id.in_(integration_ids),
                    Integration.entity_id == user.entity_id,
                    Integration.status == "active",
                )
            )).scalars().all())
            if integration_ids
            else []
        )
        return _CalendarConnectionSnapshot(
            registry=registry,
            oauth_rows={row.id: row for row in oauth_rows},
            integration_rows={row.id: row for row in integration_rows},
        )


async def _connection_options(
    db: AsyncSession,
    user: User,
    *,
    snapshot: _CalendarConnectionSnapshot | None = None,
) -> list[CalendarConnectionOption]:
    providers = [provider.value for provider in CalendarProvider]
    if snapshot is None:
        snapshot = await _CalendarConnectionSnapshotFactory.create(
            db,
            user,
            providers=providers,
        )
    options: list[CalendarConnectionOption] = []
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens

    for provider in providers:
        for account in snapshot.accounts_for(provider):
            if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT:
                row = snapshot.oauth_rows.get(account.id)
                if row is None:
                    continue
                profile = row.profile if isinstance(row.profile, dict) else {}
                email = _first_email(profile.get("email"), row.provider_user_id)
                if not email:
                    creds = lease_oauth_account_tokens(
                        row,
                        requester_id=user.id,
                        reason="oauth.calendar_connection_label",
                        requester_kind="user",
                    )
                    email = await _calendar_email_from_token(
                        account.provider,
                        creds.get("access_token"),
                    )
                display = _calendar_account_label(
                    account.provider,
                    email,
                    profile.get("display_name"),
                    profile.get("name"),
                    row.provider_user_id,
                )
                provider_user_id = row.provider_user_id
                expires_at = (
                    row.token_expires_at.isoformat()
                    if row.token_expires_at
                    else None
                )
            else:
                row = snapshot.integration_rows.get(account.id)
                if row is None:
                    continue
                cfg = row.config if isinstance(row.config, dict) else {}
                profile = (
                    cfg.get("profile")
                    if isinstance(cfg.get("profile"), dict)
                    else {}
                )
                email = _first_email(
                    profile.get("email"),
                    cfg.get("email"),
                    cfg.get("from_email"),
                    cfg.get("from_address"),
                )
                if not email:
                    email = await _calendar_email_from_token(
                        account.provider,
                        await _integration_token(db, user, row, account.provider),
                    )
                display = _calendar_account_label(
                    account.provider,
                    email,
                    profile.get("display_name"),
                    profile.get("name"),
                    cfg.get("display_name"),
                    cfg.get("name"),
                )
                # Never expose an internal Nango connection pointer to the client.
                provider_user_id = row.id
                expires_at = None
            options.append(CalendarConnectionOption(
                id=account.id,
                provider=account.provider,
                display_name=display,
                provider_user_id=provider_user_id,
                is_default=account.is_default,
                expires_at=expires_at,
            ))
    return options


def _canonical_calendar_settings_account_ids(
    settings: CalendarSettings,
    snapshot: _CalendarConnectionSnapshot,
) -> CalendarSettings:
    """Project legacy account aliases to authorized row IDs for API clients."""
    canonical = settings.model_copy(deep=True)
    if canonical.provider and canonical.connection_id:
        selected = snapshot.select(
            canonical.provider,
            canonical.connection_id,
        )
        if selected is not None:
            canonical.connection_id = selected.id

    canonical_sources: list[CalendarConflictSource] = []
    for source in canonical.conflict_sources:
        selected = (
            snapshot.select(source.provider, source.connection_id)
            if source.connection_id
            else None
        )
        canonical_sources.append(
            source.model_copy(update={"connection_id": selected.id})
            if selected is not None
            else source
        )
    canonical.conflict_sources = canonical_sources
    return canonical


async def _canonical_calendar_connection_id(
    db: AsyncSession,
    user: User,
    provider: str,
    connection_id: str | None,
    *,
    require_connected: bool = False,
    snapshot: _CalendarConnectionSnapshot | None = None,
) -> str | None:
    """Resolve a stored row ID or legacy provider account ID to one row ID."""
    normalized = str(connection_id or "").strip()
    if not normalized or not provider or provider not in _SUPPORTED_PROVIDERS:
        return normalized or None

    if snapshot is None:
        snapshot = await _CalendarConnectionSnapshotFactory.create(
            db,
            user,
            providers=[provider],
        )
    selected = snapshot.select(provider, normalized)
    if selected is not None:
        return selected.id
    if require_connected:
        raise HTTPException(422, "Calendar account is not connected")
    return normalized


async def _canonical_conflict_sources(
    db: AsyncSession,
    user: User,
    sources: list[CalendarConflictSource],
) -> list[CalendarConflictSource]:
    if not sources:
        return []
    snapshot = await _CalendarConnectionSnapshotFactory.create(
        db,
        user,
        providers={source.provider.value for source in sources},
    )
    merged: dict[tuple[str, str | None], list[str]] = {}
    for source in sources:
        connection_id = await _canonical_calendar_connection_id(
            db,
            user,
            source.provider,
            source.connection_id,
            require_connected=True,
            snapshot=snapshot,
        )
        key = (source.provider, connection_id)
        calendars = merged.setdefault(key, [])
        for calendar_id in source.calendar_ids:
            if calendar_id not in calendars:
                calendars.append(calendar_id)
    return [
        CalendarConflictSource(
            provider=provider,
            connection_id=connection_id,
            calendar_ids=calendar_ids,
        )
        for (provider, connection_id), calendar_ids in merged.items()
    ]


def _parse_public_datetime(value: str, tz: ZoneInfo) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, "Invalid start time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _booking_times(record: BookingRecord, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    start = _parse_datetime(record.starts_at)
    end = _parse_datetime(record.ends_at)
    if not start or not end:
        return None
    return start.astimezone(tz), end.astimezone(tz)


def _time_ranges_overlap(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
) -> bool:
    return first_start < second_end and second_start < first_end


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_external_datetime(value: Any, fallback_tz: ZoneInfo) -> datetime | None:
    raw = value
    tz = fallback_tz
    if isinstance(value, dict):
        raw = value.get("dateTime") or value.get("date") or value.get("value")
        tz_name = value.get("timeZone") or value.get("timezone")
        if tz_name:
            tz = _timezone(str(tz_name))
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=tz)
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _conflict_calendar_ids(settings: CalendarSettings, link: BookingLink) -> list[str]:
    raw = settings.conflict_calendar_ids or [
        link.calendar_id or settings.default_calendar_id or "primary"
    ]
    calendars: list[str] = []
    seen: set[str] = set()
    for item in raw:
        calendar_id = str(item or "").strip()
        if not calendar_id:
            continue
        if calendar_id in seen:
            continue
        seen.add(calendar_id)
        calendars.append(calendar_id)
    if not calendars:
        calendars.append("primary")
    return calendars


def _booking_availability_signature(
    settings: CalendarSettings,
    link: BookingLink,
) -> str:
    return json.dumps(
        {
            "provider": settings.provider,
            "connection_id": settings.connection_id,
            "default_calendar_id": settings.default_calendar_id,
            "conflict_calendar_ids": settings.conflict_calendar_ids,
            "conflict_sources": [
                source.model_dump()
                for source in settings.conflict_sources
            ],
            "timezone": settings.timezone,
            "working_hours": [
                window.model_dump()
                for window in settings.working_hours
            ],
            "link": link.model_dump(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _link_working_hours(
    settings: CalendarSettings,
    link: BookingLink,
) -> list[WorkingHourWindow]:
    if link.availability_mode == "custom":
        return link.working_hours
    return settings.working_hours


def _link_date_is_unavailable(link: BookingLink, target: date) -> bool:
    return target.isoformat() in link.unavailable_dates


def _link_time_is_unavailable(
    link: BookingLink,
    starts_at: datetime,
    ends_at: datetime,
    tz: ZoneInfo,
) -> bool:
    target = starts_at.date().isoformat()
    for exclusion in link.unavailable_times:
        if exclusion.date != target:
            continue
        excluded_start = datetime.combine(
            starts_at.date(),
            time.fromisoformat(exclusion.start),
            tzinfo=tz,
        )
        excluded_end = datetime.combine(
            starts_at.date(),
            time.fromisoformat(exclusion.end),
            tzinfo=tz,
        )
        if _time_ranges_overlap(starts_at, ends_at, excluded_start, excluded_end):
            return True
    return False


def _slot_label(starts_at: datetime, ends_at: datetime) -> str:
    return f"{starts_at.strftime('%-I:%M %p')} - {ends_at.strftime('%-I:%M %p')}"


def _booking_slot_step(link: BookingLink) -> timedelta:
    return timedelta(minutes=15 if link.duration_minutes <= 20 else 30)


def _next_booking_slot(
    candidate: datetime,
    work_start: datetime,
    step: timedelta,
) -> datetime:
    if candidate <= work_start:
        return work_start
    steps = (candidate - work_start) // step
    aligned = work_start + (step * steps)
    return aligned if aligned >= candidate else aligned + step


def _validate_booking_slot(
    settings: CalendarSettings,
    link: BookingLink,
    starts_at: datetime,
    *,
    external_busy_ranges: list[tuple[datetime, datetime]] | None = None,
) -> datetime:
    tz = _timezone(settings.timezone)
    starts_local = starts_at.astimezone(tz)
    ends_local = starts_local + timedelta(minutes=link.duration_minutes)
    now_local = datetime.now(tz)
    min_start = now_local + timedelta(minutes=link.min_notice_minutes)
    if starts_local < min_start:
        raise HTTPException(400, "This time is no longer available")
    if starts_local.date() > now_local.date() + timedelta(days=link.rolling_window_days):
        raise HTTPException(400, "This time is outside the booking window")

    if _link_date_is_unavailable(link, starts_local.date()):
        raise HTTPException(400, "This date is not available")
    if _link_time_is_unavailable(link, starts_local, ends_local, tz):
        raise HTTPException(400, "This time is not available")

    window = next(
        (
            row for row in _link_working_hours(settings, link)
            if row.day_of_week == starts_local.weekday() and row.enabled
        ),
        None,
    )
    if not window:
        raise HTTPException(400, "This day is not available")

    work_start = datetime.combine(
        starts_local.date(),
        _parse_time_hhmm(window.start, time(9, 0)),
        tzinfo=tz,
    )
    work_end = datetime.combine(
        starts_local.date(),
        _parse_time_hhmm(window.end, time(17, 0)),
        tzinfo=tz,
    )
    if starts_local < work_start or ends_local > work_end:
        raise HTTPException(400, "This time is outside working hours")
    if _next_booking_slot(
        starts_local,
        work_start,
        _booking_slot_step(link),
    ) != starts_local:
        raise HTTPException(400, "This time must match an available slot")

    candidate_start = starts_local - timedelta(minutes=link.buffer_before_minutes)
    candidate_end = ends_local + timedelta(minutes=link.buffer_after_minutes)
    for booking in settings.bookings:
        if booking.status != "confirmed":
            continue
        times = _booking_times(booking, tz)
        if not times:
            continue
        booked_start, booked_end = times
        booked_link = None
        if (
            booking.buffer_before_minutes is None
            or booking.buffer_after_minutes is None
        ):
            booked_link = next(
                (
                    item
                    for item in settings.booking_links
                    if item.id == booking.booking_link_id
                ),
                None,
            )
        booked_buffer_before = booking.buffer_before_minutes
        if booked_buffer_before is None:
            booked_buffer_before = booked_link.buffer_before_minutes if booked_link else 0
        booked_buffer_after = booking.buffer_after_minutes
        if booked_buffer_after is None:
            booked_buffer_after = booked_link.buffer_after_minutes if booked_link else 0
        booked_start -= timedelta(minutes=booked_buffer_before)
        booked_end += timedelta(minutes=booked_buffer_after)
        if _time_ranges_overlap(candidate_start, candidate_end, booked_start, booked_end):
            raise HTTPException(409, "This time was just booked")

    for busy_start, busy_end in external_busy_ranges or []:
        busy_start_local = busy_start.astimezone(tz)
        busy_end_local = busy_end.astimezone(tz)
        if _time_ranges_overlap(candidate_start, candidate_end, busy_start_local, busy_end_local):
            raise HTTPException(409, "This time is unavailable")

    return ends_local


async def _available_slots(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> list[AvailableSlot]:
    tz = _timezone(settings.timezone)
    now_local = datetime.now(tz)
    min_start = now_local + timedelta(minutes=link.min_notice_minutes)
    slots: list[AvailableSlot] = []
    days = min(max(link.rolling_window_days, 1), 365)
    booking_range_start = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    booking_range_end = booking_range_start + timedelta(days=days + 1)
    requested_start = (range_start or booking_range_start).astimezone(tz)
    requested_end = (range_end or (booking_range_start + timedelta(days=31))).astimezone(tz)
    effective_start = max(requested_start, booking_range_start)
    effective_end = min(requested_end, booking_range_end)
    if effective_end <= effective_start:
        return []
    effective_start_utc = effective_start.astimezone(timezone.utc)
    effective_end_utc = effective_end.astimezone(timezone.utc)

    busy_range_start = effective_start - timedelta(minutes=link.buffer_before_minutes)
    busy_range_end = effective_end + timedelta(
        minutes=link.duration_minutes + link.buffer_after_minutes,
    )
    external_busy_ranges = await _cached_external_busy_ranges(
        db,
        owner,
        settings,
        link,
        busy_range_start,
        busy_range_end,
    )

    first_day = effective_start.date()
    last_day = effective_end.date()
    for offset in range((last_day - first_day).days + 1):
        target = first_day + timedelta(days=offset)
        if _link_date_is_unavailable(link, target):
            continue
        window = next(
            (
                row for row in _link_working_hours(settings, link)
                if row.day_of_week == target.weekday() and row.enabled
            ),
            None,
        )
        if not window:
            continue
        work_start = datetime.combine(
            target,
            _parse_time_hhmm(window.start, time(9, 0)),
            tzinfo=tz,
        )
        cursor = work_start
        work_end = datetime.combine(target, _parse_time_hhmm(window.end, time(17, 0)), tzinfo=tz)
        step = _booking_slot_step(link)
        if cursor < min_start:
            cursor = _next_booking_slot(min_start, work_start, step)
        while cursor + timedelta(minutes=link.duration_minutes) <= work_end:
            cursor_utc = cursor.astimezone(timezone.utc)
            if cursor_utc < effective_start_utc:
                cursor += step
                continue
            if cursor_utc >= effective_end_utc:
                break
            try:
                ends_at = _validate_booking_slot(
                    settings,
                    link,
                    cursor,
                    external_busy_ranges=external_busy_ranges,
                )
            except HTTPException:
                cursor += step
                continue
            slots.append(AvailableSlot(
                starts_at=cursor.isoformat(),
                ends_at=ends_at.isoformat(),
                label=_slot_label(cursor, ends_at),
            ))
            cursor += step
    return slots


def _booking_availability_window(
    settings: CalendarSettings,
    link: BookingLink,
) -> tuple[datetime, datetime]:
    tz = _timezone(settings.timezone)
    today = datetime.now(tz).date()
    starts_at = datetime.combine(today, time.min, tzinfo=tz)
    days = min(max(link.rolling_window_days, 1), 365)
    return starts_at, starts_at + timedelta(days=days + 1)


def _public_availability_range(
    settings: CalendarSettings,
    link: BookingLink,
    month: str | None,
    viewer_timezone: str | None,
) -> tuple[datetime, datetime]:
    if not month:
        return _booking_availability_window(settings, link)

    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(400, "month must use YYYY-MM") from exc
    if month_start.isoformat()[:7] != month:
        raise HTTPException(400, "month must use YYYY-MM")
    # Converting a minimum/maximum-year local midnight across time zones can
    # overflow before the requested range is clamped to the booking window.
    if month_start.year in {date.min.year, date.max.year}:
        raise HTTPException(400, "month is outside the supported range")
    viewer_tz = _timezone(_timezone_name(viewer_timezone, settings.timezone))
    starts_at = datetime.combine(month_start, time.min, tzinfo=viewer_tz)
    try:
        if month_start.month == 12:
            next_month = date(month_start.year + 1, 1, 1)
        else:
            next_month = date(month_start.year, month_start.month + 1, 1)
    except ValueError as exc:
        raise HTTPException(400, "month must use YYYY-MM") from exc
    ends_at = datetime.combine(next_month, time.min, tzinfo=viewer_tz)
    return starts_at, ends_at


async def _find_public_booking(
    db: AsyncSession,
    slug: str,
    owner_id: str | None = None,
) -> tuple[User, CalendarSettings, BookingLink]:
    query = select(User).where(User.deleted_at.is_(None), User.status == "active")
    if owner_id:
        query = query.where(User.id == owner_id)
    rows = (await db.execute(query)).scalars().all()
    matches: list[tuple[User, CalendarSettings, BookingLink]] = []
    for owner in rows:
        settings = _normalize_settings(owner)
        for link in settings.booking_links:
            if link.slug == slug and link.enabled:
                matches.append((owner, settings, link))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            409,
            "Booking link is ambiguous. Ask the host for the current link",
        )
    raise HTTPException(404, "Booking link not found")


async def _lock_public_booking(
    db: AsyncSession,
    owner_id: str,
    slug: str,
) -> tuple[User, CalendarSettings, BookingLink]:
    """Serialize bookings for one host and re-read their latest settings."""
    owner = await lock_calendar_settings_user(db, owner_id)
    if not owner or owner.status != "active":
        raise HTTPException(404, "Booking link not found")
    settings = _normalize_settings(owner)
    link = next(
        (
            item
            for item in settings.booking_links
            if item.slug == slug and item.enabled
        ),
        None,
    )
    if not link:
        raise HTTPException(404, "Booking link not found")
    return owner, settings, link


@asynccontextmanager
async def _serialize_public_booking(owner_id: str) -> AsyncIterator[None]:
    """Serialize one host's booking requests without holding a DB connection."""
    local_key = (id(asyncio.get_running_loop()), owner_id)
    local_lock = _PUBLIC_BOOKING_LOCKS.get(local_key)
    if local_lock is None:
        local_lock = asyncio.Lock()
        _PUBLIC_BOOKING_LOCKS[local_key] = local_lock

    async with local_lock:
        lease_key = f"calendar-public-booking:{owner_id}"
        lease_token = secrets.token_urlsafe(18)
        lease_acquired = await cache.acquire_lease(
            lease_key,
            lease_token,
            ttl=_PUBLIC_BOOKING_LEASE_TTL_SECONDS,
        )
        if lease_acquired is False:
            deadline = monotonic() + _PUBLIC_BOOKING_LEASE_WAIT_SECONDS
            while lease_acquired is False and monotonic() < deadline:
                await asyncio.sleep(_PUBLIC_AVAILABILITY_LEASE_POLL_SECONDS)
                lease_acquired = await cache.acquire_lease(
                    lease_key,
                    lease_token,
                    ttl=_PUBLIC_BOOKING_LEASE_TTL_SECONDS,
                )
        if lease_acquired is False:
            raise HTTPException(503, _BOOKING_BUSY_ERROR)

        lease_heartbeat: asyncio.Task | None = None
        if lease_acquired is True:
            async def keep_lease_alive() -> None:
                interval = max(_PUBLIC_BOOKING_LEASE_TTL_SECONDS / 3, 1)
                while True:
                    await asyncio.sleep(interval)
                    extended = await cache.extend_lease(
                        lease_key,
                        lease_token,
                        ttl=_PUBLIC_BOOKING_LEASE_TTL_SECONDS,
                    )
                    if extended is not True:
                        return

            lease_heartbeat = asyncio.create_task(keep_lease_alive())

        try:
            yield
        finally:
            if lease_heartbeat is not None:
                lease_heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await lease_heartbeat
            if lease_acquired is True:
                await cache.release_lease(lease_key, lease_token)


@dataclass(frozen=True)
class _NangoTokenRequest:
    secret: str
    provider_config_key: str
    connection_id: str


_CalendarCredential = str | _NangoTokenRequest | None


async def _materialize_calendar_credential(
    credential: _CalendarCredential,
) -> str | None:
    if not isinstance(credential, _NangoTokenRequest):
        return credential
    from packages.core.services.nango_bridge import fetch_nango_access_token

    return await fetch_nango_access_token(
        secret=credential.secret,
        provider_config_key=credential.provider_config_key,
        connection_id=credential.connection_id,
    )


async def _integration_credential(
    db: AsyncSession,
    owner: User,
    row: Integration,
    provider: str,
) -> _CalendarCredential:
    nango_meta = (row.config or {}).get("nango") or {}
    connection_id = str(nango_meta.get("connection_id") or "").strip()
    if connection_id:
        provider_config_key = str(
            nango_meta.get("provider_config_key") or provider
        ).strip()
        if not nango_connection_matches_runtime_scope(
            connection_id=connection_id,
            entity_id=row.entity_id,
            owner_user_id=row.owner_user_id,
            provider=provider,
            provider_config_key=provider_config_key,
        ):
            logger.warning(
                "Rejected out-of-scope Nango calendar connection %s",
                row.id,
            )
            return None
        from packages.core.ai.mcp.nango import get_nango_secret

        secret = await get_nango_secret(db, owner.entity_id)
        if not secret:
            return None
        return _NangoTokenRequest(
            secret=secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
        )

    try:
        from packages.core.credentials import Requester, get_credential_service
        creds = get_credential_service().lease_integration(
            row,
            requester=Requester(kind="system", id=owner.entity_id),
            reason="calendar_settings.public_booking",
        )
    except Exception:
        logger.debug("Calendar booking credential lease failed", exc_info=True)
        creds = row.credentials or {}
    return (creds or {}).get("access_token") or (creds or {}).get("api_key")


async def _integration_token(
    db: AsyncSession,
    owner: User,
    row: Integration,
    provider: str,
) -> str | None:
    credential = await _integration_credential(db, owner, row, provider)
    return await _materialize_calendar_credential(credential)


async def _booking_calendar_account_id(
    db: AsyncSession,
    owner: User,
    provider: str,
    account_id: str | None,
) -> str | None:
    """Freeze the exact connected account selected for a booking side effect."""
    if provider not in {"google_calendar", "ms_calendar"}:
        return None
    snapshot = await _CalendarConnectionSnapshotFactory.create(
        db,
        owner,
        providers=[provider],
    )
    selected = snapshot.select(provider, account_id)
    return selected.id if selected is not None else None


async def _resolve_calendar_credential(
    db: AsyncSession,
    owner: User,
    provider: str,
    account_id: str | None,
    *,
    snapshot: _CalendarConnectionSnapshot | None = None,
) -> _CalendarCredential:
    if provider not in {"google_calendar", "ms_calendar"}:
        return None
    if snapshot is None:
        snapshot = await _CalendarConnectionSnapshotFactory.create(
            db,
            owner,
            providers=[provider],
        )
    selected = snapshot.select(provider, account_id)
    if selected is None:
        return None
    if selected.kind is IntegrationAccountKind.OAUTH_ACCOUNT:
        row = snapshot.oauth_rows.get(selected.id)
        if row is None:
            return None
        from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
        return lease_oauth_account_tokens(
            row,
            requester_id=owner.id,
            reason="oauth.calendar_action",
            requester_kind="user",
        ).get("access_token")
    row = snapshot.integration_rows.get(selected.id)
    return (
        await _integration_credential(db, owner, row, provider)
        if row is not None
        else None
    )


async def _resolve_calendar_token(
    db: AsyncSession,
    owner: User,
    provider: str,
    account_id: str | None,
) -> str | None:
    credential = await _resolve_calendar_credential(
        db,
        owner,
        provider,
        account_id,
    )
    return await _materialize_calendar_credential(credential)


async def _calendar_options(
    db: AsyncSession,
    owner: User,
    provider: str,
    connection_id: str | None,
) -> list[CalendarOption]:
    token = await _resolve_calendar_token(db, owner, provider, connection_id)
    if not token:
        return []

    try:
        if provider == "google_calendar":
            from packages.core.ai.mcp import google_calendar

            items = await google_calendar.list_calendars_data(token)
        else:
            from packages.core.ai.mcp import ms_calendar

            items = await ms_calendar.list_calendars_data(token)
    except Exception as exc:
        logger.warning("Calendar list lookup failed", exc_info=True)
        raise HTTPException(503, _CALENDAR_LIST_ERROR) from exc

    options: list[CalendarOption] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        is_primary = bool(item.get("primary") or item.get("isDefaultCalendar"))
        raw_id = str(item.get("id") or "").strip()
        calendar_id = "primary" if is_primary else raw_id
        if not calendar_id or calendar_id in seen:
            continue
        seen.add(calendar_id)
        if provider == "google_calendar":
            name = str(item.get("summaryOverride") or item.get("summary") or raw_id)
            read_only = str(item.get("accessRole") or "owner") not in {"owner", "writer"}
        else:
            name = str(item.get("name") or raw_id)
            read_only = item.get("canEdit") is False
        options.append(CalendarOption(
            id=calendar_id,
            name=name,
            is_primary=is_primary,
            read_only=read_only,
        ))
    return sorted(options, key=lambda item: (not item.is_primary, item.name.lower()))


def _calendar_description(link: BookingLink, booking: BookingRecord, owner: User) -> str:
    lines = [
        "Booked via Manor AI.",
        f"Guest: {booking.guest_name} <{booking.guest_email}>",
        f"Host: {owner.display_name or owner.email}",
    ]
    if booking.note:
        lines.extend(["", "Guest note:", booking.note])
    if link.description:
        lines.extend(["", link.description])
    return "\n".join(lines)


def _calendar_location(link: BookingLink) -> str | None:
    if link.location_detail:
        return link.location_detail
    if link.location_type == "phone":
        return "Phone call"
    if link.location_type == "in_person":
        return "In person"
    if link.location_type == "video":
        return "Video meeting"
    return None


def _calendar_event_intent(
    settings: CalendarSettings,
    link: BookingLink,
    booking: BookingRecord,
    owner: User,
) -> BookingCalendarEventIntent | None:
    provider = booking.calendar_provider or settings.provider
    if (
        provider not in {"google_calendar", "ms_calendar"}
        or not booking.calendar_account_id
    ):
        return None
    return BookingCalendarEventIntent(
        provider=provider,
        account_id=booking.calendar_account_id,
        calendar_id=link.calendar_id or settings.default_calendar_id or "primary",
        summary=f"{link.name} with {booking.guest_name}",
        description=_calendar_description(link, booking, owner),
        location=_calendar_location(link),
        create_online_meeting=(
            link.location_type == "video" and not link.location_detail
        ),
        timezone=booking.timezone or settings.timezone,
    )


def _result_text(result: dict[str, Any]) -> str:
    content = result.get("content") or []
    if not content:
        return ""
    return str((content[0] or {}).get("text") or "")


def _json_result(result: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(_result_text(result))
    except Exception:
        return {}


def _calendar_ids_for_external_events(settings: CalendarSettings) -> list[str]:
    ids = [
        str(item or "").strip()
        for item in (
            settings.visible_calendar_ids
            or settings.conflict_calendar_ids
            or [settings.default_calendar_id]
        )
    ]
    ids = [item for item in ids if item]
    if not ids:
        ids = [settings.default_calendar_id or "primary"]
    seen: set[str] = set()
    out: list[str] = []
    for item in ids:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _all_day_date(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _google_meeting_url(item: dict[str, Any]) -> str | None:
    if item.get("hangoutLink"):
        return str(item.get("hangoutLink"))
    conference = item.get("conferenceData") if isinstance(item.get("conferenceData"), dict) else {}
    for entry in conference.get("entryPoints") or []:
        if isinstance(entry, dict) and entry.get("uri"):
            return str(entry["uri"])
    return None


def _normalize_google_event(
    item: dict[str, Any],
    *,
    calendar_id: str,
    calendar_name: str | None,
    settings: CalendarSettings,
) -> ExternalCalendarEvent | None:
    event_id = str(item.get("id") or "").strip()
    if not event_id or item.get("status") == "cancelled":
        return None
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    all_day = bool(start.get("date") and not start.get("dateTime"))
    starts_at = str(start.get("dateTime") or _all_day_date(start.get("date")) or "")
    if not starts_at:
        return None
    ends_at = str(end.get("dateTime") or _all_day_date(end.get("date")) or "") or None
    attendees = item.get("attendees") if isinstance(item.get("attendees"), list) else []
    organizer = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
    title = str(item.get("summary") or ("Busy" if item.get("visibility") == "private" else "Untitled event"))
    return ExternalCalendarEvent(
        id=f"external:google_calendar:{calendar_id}:{event_id}",
        provider="google_calendar",
        calendar_id=calendar_id,
        calendar_name=calendar_name,
        external_event_id=event_id,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=str(start.get("timeZone") or settings.timezone),
        all_day=all_day,
        status=str(item.get("status") or "confirmed"),
        location=item.get("location"),
        description=item.get("description"),
        organizer_email=organizer.get("email"),
        attendee_count=len(attendees),
        calendar_event_url=item.get("htmlLink"),
        meeting_url=_google_meeting_url(item),
    )


def _ms_location(item: dict[str, Any]) -> str | None:
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    return location.get("displayName") or None


def _normalize_ms_event(
    item: dict[str, Any],
    *,
    calendar_id: str,
    calendar_name: str | None,
    settings: CalendarSettings,
) -> ExternalCalendarEvent | None:
    event_id = str(item.get("id") or "").strip()
    if not event_id or item.get("isCancelled"):
        return None
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    fallback_tz = _timezone(settings.timezone)
    parsed_start = _parse_external_datetime(start, fallback_tz)
    if not parsed_start:
        return None
    parsed_end = _parse_external_datetime(end, fallback_tz)
    starts_at = parsed_start.isoformat()
    ends_at = parsed_end.isoformat() if parsed_end else None
    online = item.get("onlineMeeting") if isinstance(item.get("onlineMeeting"), dict) else {}
    attendees = item.get("attendees") if isinstance(item.get("attendees"), list) else []
    organizer = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
    email_addr = organizer.get("emailAddress") if isinstance(organizer.get("emailAddress"), dict) else {}
    return ExternalCalendarEvent(
        id=f"external:ms_calendar:{calendar_id}:{event_id}",
        provider="ms_calendar",
        calendar_id=calendar_id,
        calendar_name=calendar_name,
        external_event_id=event_id,
        title=str(item.get("subject") or "Untitled event"),
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=str(start.get("timeZone") or settings.timezone),
        all_day=bool(item.get("isAllDay", False)),
        status=str(item.get("showAs") or "busy"),
        location=_ms_location(item),
        description=item.get("bodyPreview"),
        organizer_email=email_addr.get("address"),
        attendee_count=len(attendees),
        calendar_event_url=item.get("webLink"),
        meeting_url=online.get("joinUrl") or item.get("onlineMeetingUrl"),
    )


def _external_event_sort_key(item: ExternalCalendarEvent) -> str:
    return item.starts_at or ""


_CalendarBusyLookup = Callable[
    [str, list[str], str, datetime, datetime],
    Awaitable[list[tuple[datetime, datetime]]],
]
_CalendarVisibleEventLookup = Callable[
    [str, str, dict[str, str]],
    Awaitable[tuple[list[dict[str, Any]], str | None]],
]
_CalendarEventNormalizer = Callable[..., ExternalCalendarEvent | None]


async def _google_calendar_busy_ranges(
    token: str,
    calendars: list[str],
    timezone_name: str,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    from packages.core.ai.mcp import google_calendar

    data = await google_calendar.query_freebusy_data(
        token,
        {
            "calendars": calendars,
            "time_min": _iso_utc(range_start),
            "time_max": _iso_utc(range_end),
            "timezone": timezone_name,
        },
    )
    calendar_data = data.get("calendars")
    if not isinstance(calendar_data, dict):
        raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)

    tz = _timezone(timezone_name)
    ranges: list[tuple[datetime, datetime]] = []
    for calendar_id in calendars:
        calendar = calendar_data.get(calendar_id)
        if not isinstance(calendar, dict) or calendar.get("errors"):
            raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
        busy = calendar.get("busy")
        if not isinstance(busy, list):
            raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
        for item in busy:
            if not isinstance(item, dict):
                raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
            start = _parse_external_datetime(item.get("start"), tz)
            end = _parse_external_datetime(item.get("end"), tz)
            if not start or not end or start >= end:
                raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
            ranges.append((start, end))
    return ranges


async def _microsoft_calendar_busy_ranges(
    token: str,
    calendars: list[str],
    timezone_name: str,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    from packages.core.ai.mcp import ms_calendar

    range_args = {
        "time_min": _iso_utc(range_start),
        "time_max": _iso_utc(range_end),
        "top": 500,
        "select": "id,start,end,showAs,isCancelled",
    }
    tz = _timezone(timezone_name)
    ranges: list[tuple[datetime, datetime]] = []
    for calendar_id in calendars:
        arguments = dict(range_args)
        if calendar_id != "primary":
            arguments["calendar_id"] = calendar_id
        items = await ms_calendar.list_events_data(token, arguments)
        for item in items:
            if item.get("isCancelled"):
                continue
            status = str(item.get("showAs") or "busy").lower()
            if status not in {"busy", "tentative", "oof", "workingelsewhere"}:
                continue
            start = _parse_external_datetime(item.get("start"), tz)
            end = _parse_external_datetime(item.get("end"), tz)
            if not start or not end or start >= end:
                raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
            ranges.append((start, end))
    return ranges


async def _google_visible_events(
    token: str,
    calendar_id: str,
    range_args: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    from packages.core.ai.mcp import google_calendar

    data = await google_calendar.list_events_data(token, {
        **range_args,
        "calendar_id": calendar_id,
        "max_results": 250,
    })
    return data["items"], str(data.get("summary") or calendar_id)


async def _microsoft_visible_events(
    token: str,
    calendar_id: str,
    range_args: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    from packages.core.ai.mcp import ms_calendar

    arguments = {
        **range_args,
        "top": 500,
        "select": (
            "id,subject,start,end,webLink,onlineMeeting,onlineMeetingUrl,"
            "location,bodyPreview,attendees,organizer,showAs,isCancelled,isAllDay"
        ),
    }
    if calendar_id != "primary":
        arguments["calendar_id"] = calendar_id
    items = await ms_calendar.list_events_data(token, arguments)
    return items, calendar_id if calendar_id != "primary" else "Primary calendar"


@dataclass(frozen=True)
class _CalendarProviderAdapter:
    busy_lookup: _CalendarBusyLookup
    visible_event_lookup: _CalendarVisibleEventLookup
    normalize_event: _CalendarEventNormalizer


_CALENDAR_PROVIDER_ADAPTERS: dict[CalendarProvider, _CalendarProviderAdapter] = {
    CalendarProvider.GOOGLE: _CalendarProviderAdapter(
        busy_lookup=_google_calendar_busy_ranges,
        visible_event_lookup=_google_visible_events,
        normalize_event=_normalize_google_event,
    ),
    CalendarProvider.MICROSOFT: _CalendarProviderAdapter(
        busy_lookup=_microsoft_calendar_busy_ranges,
        visible_event_lookup=_microsoft_visible_events,
        normalize_event=_normalize_ms_event,
    ),
}


def _calendar_provider_adapter(
    provider: CalendarProvider,
) -> _CalendarProviderAdapter:
    return _CALENDAR_PROVIDER_ADAPTERS[provider]


async def _external_busy_ranges(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    sources = settings.conflict_sources
    if not sources and settings.provider in {"google_calendar", "ms_calendar"}:
        sources = [
            CalendarConflictSource(
                provider=settings.provider,
                connection_id=settings.connection_id,
                calendar_ids=_conflict_calendar_ids(settings, link),
            )
        ]
    if not sources:
        return []

    busy_ranges: list[tuple[datetime, datetime]] = []
    resolved_sources: list[tuple[CalendarConflictSource, _CalendarCredential]] = []

    try:
        snapshot = await _CalendarConnectionSnapshotFactory.create(
            db,
            owner,
            providers={source.provider.value for source in sources},
        )
        for source in sources:
            provider = source.provider
            credential = await _resolve_calendar_credential(
                db,
                owner,
                provider,
                source.connection_id,
                snapshot=snapshot,
            )
            if not credential:
                logger.warning("Calendar availability lookup has no token for %s", provider)
                raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
            resolved_sources.append((source, credential))

        # Credential planning may use the request session. Materializing a
        # Nango token and querying providers are remote I/O, so release the
        # transaction and pooled connection before either step.
        await db.rollback()

        for source, credential in resolved_sources:
            token = await _materialize_calendar_credential(credential)
            if not token:
                logger.warning(
                    "Calendar availability lookup has no token for %s",
                    source.provider,
                )
                raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)
            adapter = _calendar_provider_adapter(CalendarProvider(source.provider))
            busy_ranges.extend(await adapter.busy_lookup(
                token,
                source.calendar_ids,
                settings.timezone,
                range_start,
                range_end,
            ))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Calendar free/busy lookup failed", exc_info=True)
        raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR) from exc

    return busy_ranges


def _availability_cache_key(
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    range_start: datetime,
    range_end: datetime,
) -> str:
    payload = "|".join((
        owner.id,
        _booking_availability_signature(settings, link),
        range_start.isoformat(),
        range_end.isoformat(),
    ))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"calendar-public-availability:{owner.id}:{digest}"


def _deserialize_busy_ranges(value: Any) -> list[tuple[datetime, datetime]] | None:
    if not isinstance(value, list):
        return None
    ranges: list[tuple[datetime, datetime]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            return None
        start = _parse_datetime(item[0])
        end = _parse_datetime(item[1])
        if not start or not end or start >= end:
            return None
        ranges.append((start, end))
    return ranges


def _store_local_availability(
    cache_key: str,
    value: list[list[str]],
    ttl: int,
) -> None:
    now = monotonic()
    for key, cached in list(_PUBLIC_AVAILABILITY_LOCAL_CACHE.items()):
        if cached[0] <= now:
            _PUBLIC_AVAILABILITY_LOCAL_CACHE.pop(key, None)
    while len(_PUBLIC_AVAILABILITY_LOCAL_CACHE) >= _PUBLIC_AVAILABILITY_LOCAL_CACHE_MAX:
        oldest_key = next(iter(_PUBLIC_AVAILABILITY_LOCAL_CACHE))
        _PUBLIC_AVAILABILITY_LOCAL_CACHE.pop(oldest_key, None)
    _PUBLIC_AVAILABILITY_LOCAL_CACHE[cache_key] = (now + ttl, value)


async def _cached_external_busy_ranges(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    ttl = _PUBLIC_AVAILABILITY_CACHE_TTL_SECONDS
    if ttl <= 0:
        return await _external_busy_ranges(
            db, owner, settings, link, range_start, range_end,
        )

    cache_key = _availability_cache_key(
        owner, settings, link, range_start, range_end,
    )

    async def cached_value() -> list[tuple[datetime, datetime]] | None:
        local = _PUBLIC_AVAILABILITY_LOCAL_CACHE.get(cache_key)
        if local and local[0] > monotonic():
            return _deserialize_busy_ranges(local[1])
        _PUBLIC_AVAILABILITY_LOCAL_CACHE.pop(cache_key, None)
        shared = await cache.get(cache_key)
        parsed = _deserialize_busy_ranges(shared)
        if parsed is not None:
            serialized = [[start.isoformat(), end.isoformat()] for start, end in parsed]
            _store_local_availability(cache_key, serialized, ttl)
        return parsed

    existing = await cached_value()
    if existing is not None:
        return existing

    lock_key = (id(asyncio.get_running_loop()), cache_key)
    lock = _PUBLIC_AVAILABILITY_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _PUBLIC_AVAILABILITY_LOCKS[lock_key] = lock
    async with lock:
        existing = await cached_value()
        if existing is not None:
            return existing

        lease_key = f"{cache_key}:refresh-lease"
        lease_token = secrets.token_urlsafe(18)
        lease_acquired = await cache.acquire_lease(
            lease_key,
            lease_token,
            ttl=_PUBLIC_AVAILABILITY_LEASE_TTL_SECONDS,
        )
        if lease_acquired is False:
            deadline = monotonic() + _PUBLIC_AVAILABILITY_LEASE_WAIT_SECONDS
            while lease_acquired is False and monotonic() < deadline:
                await asyncio.sleep(_PUBLIC_AVAILABILITY_LEASE_POLL_SECONDS)
                existing = await cached_value()
                if existing is not None:
                    return existing
                lease_acquired = await cache.acquire_lease(
                    lease_key,
                    lease_token,
                    ttl=_PUBLIC_AVAILABILITY_LEASE_TTL_SECONDS,
                )

        if lease_acquired is False:
            raise HTTPException(503, _CALENDAR_AVAILABILITY_ERROR)

        lease_heartbeat: asyncio.Task | None = None
        if lease_acquired is True:
            async def keep_lease_alive() -> None:
                interval = max(_PUBLIC_AVAILABILITY_LEASE_TTL_SECONDS / 3, 1)
                while True:
                    await asyncio.sleep(interval)
                    extended = await cache.extend_lease(
                        lease_key,
                        lease_token,
                        ttl=_PUBLIC_AVAILABILITY_LEASE_TTL_SECONDS,
                    )
                    if extended is not True:
                        return

            lease_heartbeat = asyncio.create_task(keep_lease_alive())

        try:
            if lease_acquired is True:
                existing = await cached_value()
                if existing is not None:
                    return existing
            ranges = await _external_busy_ranges(
                db, owner, settings, link, range_start, range_end,
            )
            if lease_acquired is True:
                still_held = await cache.extend_lease(
                    lease_key,
                    lease_token,
                    ttl=_PUBLIC_AVAILABILITY_LEASE_TTL_SECONDS,
                )
                if still_held is not True:
                    return ranges
            serialized = [[start.isoformat(), end.isoformat()] for start, end in ranges]
            _store_local_availability(cache_key, serialized, ttl)
            await cache.set(cache_key, serialized, ttl=ttl)
            return ranges
        finally:
            if lease_heartbeat is not None:
                lease_heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await lease_heartbeat
            if lease_acquired is True:
                await cache.release_lease(lease_key, lease_token)


def _booking_email_detail_table(rows: list[tuple[str, str, bool]]) -> str:
    body = []
    for label, value, is_link in rows:
        safe_label = escape(label)
        safe_value = escape(value or "")
        if is_link and value:
            safe_href = escape(value, quote=True)
            safe_value = (
                f"<a href='{safe_href}' style='color:#0d9488;text-decoration:none;"
                f"word-break:break-all;font-weight:600;'>{safe_value}</a>"
            )
        body.append(
            "<tr>"
            f"<td style='padding:10px 0;color:#64748b;font-size:13px;width:92px;vertical-align:top;'>{safe_label}</td>"
            f"<td style='padding:10px 0;color:#0f172a;font-size:14px;font-weight:600;vertical-align:top;'>{safe_value}</td>"
            "</tr>"
        )
    return (
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
        "style='margin:20px 0;border-collapse:collapse;background:#f8fafc;"
        "border:1px solid #e2e8f0;border-radius:14px;padding:4px 18px;display:block;'>"
        f"{''.join(body)}"
        "</table>"
    )


def _booking_email_note_block(label: str, note: str | None) -> str:
    if not note:
        return ""
    safe_note = escape(note).replace("\n", "<br />")
    return (
        "<div style='margin-top:18px;padding:14px 16px;background:#f8fafc;"
        "border:1px solid #e2e8f0;border-radius:12px;'>"
        f"<div style='color:#64748b;font-size:12px;font-weight:700;text-transform:uppercase;"
        f"letter-spacing:0.04em;margin-bottom:6px;'>{escape(label)}</div>"
        f"<div style='color:#334155;font-size:14px;line-height:22px;'>{safe_note}</div>"
        "</div>"
    )


def _booking_display_when(booking: BookingRecord, tz_name: str | None = None) -> str:
    tz = _timezone(tz_name or booking.timezone)
    start = _parse_datetime(booking.starts_at)
    end = _parse_datetime(booking.ends_at)
    if not start or not end:
        return "Time to be confirmed"
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)
    return (
        f"{start_local.strftime('%A, %B %-d, %Y')} "
        f"{start_local.strftime('%-I:%M %p')} - {end_local.strftime('%-I:%M %p')} "
        f"{tz_name or booking.timezone}"
    )


async def _send_booking_notification(
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    booking: BookingRecord,
) -> None:
    when = _booking_display_when(booking, settings.timezone)
    title = f"New booking: {link.name}"
    body = f"{booking.guest_name} booked {when}."
    meta = {
        "kind": "booking_confirmed",
        "booking_id": booking.id,
        "booking_link_id": booking.booking_link_id,
        "booking_link_slug": booking.booking_link_slug,
        "booking_link_name": link.name,
        "guest_name": booking.guest_name,
        "guest_email": booking.guest_email,
        "starts_at": booking.starts_at,
        "ends_at": booking.ends_at,
        "timezone": booking.timezone,
        "calendar_event_url": booking.calendar_event_url,
        "meeting_url": booking.meeting_url,
    }
    await notify(
        entity_id=owner.entity_id,
        user_id=owner.id,
        type="booking_confirmed",
        title=title,
        body=body,
        link="/tasks?view=calendar",
        meta=meta,
        severity="info",
        idempotency_key=f"booking-confirmed:{booking.id}",
    )


def _calendar_event_host_email(
    provider: CalendarProvider,
    event: dict[str, Any],
) -> str | None:
    organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
    if provider == "google_calendar":
        return _first_email(organizer.get("email"))
    email_address = (
        organizer.get("emailAddress")
        if isinstance(organizer.get("emailAddress"), dict)
        else {}
    )
    return _first_email(email_address.get("address"))


def _google_booking_event_id(booking_id: str) -> str:
    return hashlib.sha256(f"manor-booking:{booking_id}".encode("utf-8")).hexdigest()


def _apply_calendar_event_metadata(
    booking: BookingRecord,
    event: dict[str, Any],
) -> bool:
    if not (event.get("calendar_event_id") or event.get("calendar_event_url")):
        return False
    booking.calendar_event_created = True
    booking.calendar_event_id = event.get("calendar_event_id")
    booking.calendar_event_url = event.get("calendar_event_url")
    booking.meeting_url = event.get("meeting_url")
    booking.host_email = event.get("host_email")
    booking.calendar_metadata_sync_pending = False
    return True


async def _create_external_calendar_event(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink | None,
    booking: BookingRecord,
) -> dict[str, Any]:
    intent = booking.calendar_event_intent
    if intent is None and link is not None:
        intent = _calendar_event_intent(settings, link, booking, owner)
    if intent is None:
        return {}
    provider = intent.provider
    credential = await _resolve_calendar_credential(
        db,
        owner,
        provider,
        intent.account_id,
    )
    if not credential:
        return {}

    # Credential selection may query the request session. Release its
    # transaction (and any reconciliation row lock) before Nango or calendar
    # provider network I/O.
    await db.rollback()
    token = await _materialize_calendar_credential(credential)
    if not token:
        return {}

    tz = _timezone(intent.timezone)
    starts_at = _parse_datetime(booking.starts_at)
    ends_at = _parse_datetime(booking.ends_at)
    if not starts_at or not ends_at:
        return {}
    starts_local = starts_at.astimezone(tz)
    ends_local = ends_at.astimezone(tz)

    if provider == "google_calendar":
        from packages.core.ai.mcp import google_calendar
        calendar_id = intent.calendar_id
        event_id = _google_booking_event_id(booking.id)
        arguments = {
            "summary": intent.summary,
            "start_time": starts_local.isoformat(),
            "end_time": ends_local.isoformat(),
            "description": intent.description,
            "location": intent.location,
            "attendees": [booking.guest_email],
            "calendar_id": calendar_id,
            "event_id": event_id,
            "create_meet_link": intent.create_online_meeting,
            "conference_request_id": booking.id,
        }
        try:
            data = await google_calendar.create_event_data(token, arguments)
        except Exception:
            try:
                data = await google_calendar.get_event_data(token, {
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                })
            except Exception:
                logger.warning(
                    "Google Calendar booking event creation and recovery failed",
                    exc_info=True,
                )
                return {}
        meeting_url = data.get("hangoutLink")
        conference = data.get("conferenceData") or {}
        for entry in conference.get("entryPoints") or []:
            if entry.get("uri"):
                meeting_url = entry["uri"]
                break
        return {
            "calendar_event_id": data.get("id"),
            "calendar_event_url": data.get("htmlLink"),
            "meeting_url": meeting_url,
            "host_email": _calendar_event_host_email(provider, data),
        }

    from packages.core.ai.mcp import ms_calendar
    try:
        data = await ms_calendar.create_event_data(token, {
            "subject": intent.summary,
            "start_time": starts_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_time": ends_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": intent.timezone,
            "body": escape(intent.description).replace("\n", "<br />"),
            "location": intent.location,
            "attendees": [booking.guest_email],
            "transaction_id": booking.id,
            "calendar_id": None if intent.calendar_id == "primary" else intent.calendar_id,
            "is_online_meeting": intent.create_online_meeting,
        })
    except Exception:
        logger.warning("MS Calendar booking event failed", exc_info=True)
        return {}
    online = data.get("onlineMeeting") or {}
    return {
        "calendar_event_id": data.get("id"),
        "calendar_event_url": data.get("webLink"),
        "meeting_url": online.get("joinUrl"),
        "host_email": _calendar_event_host_email(provider, data),
    }


async def _persist_confirmed_booking(
    db: AsyncSession,
    owner_id: str,
    booking: BookingRecord,
    *,
    attempts: int = 3,
) -> tuple[User | None, CalendarSettings | None]:
    for attempt in range(max(attempts, 1)):
        try:
            locked_owner = await lock_calendar_settings_user(db, owner_id)
            if not locked_owner:
                raise RuntimeError("Booking owner could not be loaded")
            locked_settings = _normalize_settings(locked_owner)
            booking_index = next(
                (
                    index
                    for index, stored_booking in enumerate(locked_settings.bookings)
                    if stored_booking.id == booking.id
                ),
                None,
            )
            if booking_index is None:
                raise RuntimeError("Confirmed booking could not be loaded")
            locked_settings.bookings[booking_index] = booking
            await _save_settings(db, locked_owner, locked_settings)
            await db.commit()
            return locked_owner, locked_settings
        except Exception:
            await db.rollback()
            if attempt + 1 >= max(attempts, 1):
                logger.warning(
                    "Confirmed booking metadata persistence failed for %s",
                    booking.id,
                    exc_info=True,
                )
                break
            await asyncio.sleep(0.05 * (attempt + 1))
    return None, None


async def reconcile_booking_metadata(
    db: AsyncSession,
    owner_id: str,
    booking_id: str,
) -> bool:
    owner = await lock_calendar_settings_user(db, owner_id)
    if not owner:
        return True
    settings = _normalize_settings(owner)
    booking = next(
        (item for item in settings.bookings if item.id == booking_id),
        None,
    )
    if not booking or not booking.calendar_metadata_sync_pending:
        await db.rollback()
        return True
    link = next(
        (
            item
            for item in settings.booking_links
            if item.id == booking.booking_link_id
            or item.slug == booking.booking_link_slug
        ),
        None,
    )
    if not link and booking.calendar_event_intent is None:
        await db.rollback()
        raise RuntimeError("Booking link could not be loaded for metadata reconciliation")

    event = await _create_external_calendar_event(db, owner, settings, link, booking)
    if not (event.get("calendar_event_id") or event.get("calendar_event_url")):
        await db.rollback()
        raise RuntimeError("Calendar event metadata is still unavailable")

    # The provider call ran without a transaction. Re-lock the latest settings
    # so concurrent calendar edits are preserved when metadata is merged.
    locked_owner = await lock_calendar_settings_user(db, owner_id)
    if not locked_owner:
        await db.rollback()
        return True
    locked_settings = _normalize_settings(locked_owner)
    booking_index = next(
        (
            index
            for index, stored_booking in enumerate(locked_settings.bookings)
            if stored_booking.id == booking.id
        ),
        None,
    )
    if booking_index is None:
        await db.rollback()
        return True
    locked_booking = locked_settings.bookings[booking_index]
    if not locked_booking.calendar_metadata_sync_pending:
        await db.rollback()
        return True
    _apply_calendar_event_metadata(locked_booking, event)
    locked_settings.bookings[booking_index] = locked_booking
    await _save_settings(db, locked_owner, locked_settings)
    await db.commit()
    return True


def _schedule_booking_metadata_reconciliation(owner_id: str, booking_id: str) -> None:
    try:
        from packages.core.tasks.maintenance_tasks import (
            reconcile_booking_metadata_task,
        )

        reconcile_booking_metadata_task.delay(owner_id, booking_id)
    except Exception:
        logger.warning(
            "Could not schedule booking metadata reconciliation for %s",
            booking_id,
            exc_info=True,
        )


async def _send_booking_confirmation_emails(
    owner_email: str,
    owner_name: str,
    settings: CalendarSettings,
    link: BookingLink,
    booking: BookingRecord,
) -> bool:
    from packages.core.services.email_service import send_common_email

    guest_when = _booking_display_when(
        booking,
        booking.guest_timezone or booking.timezone,
    )
    owner_when = _booking_display_when(booking, settings.timezone)
    location = booking.meeting_url or link.location_detail or _calendar_location(link) or "To be shared"
    subject = f"Booking confirmed: {link.name}"

    meeting_cta = ""
    if booking.meeting_url:
        safe_url = escape(booking.meeting_url, quote=True)
        meeting_cta = (
            "<p style='margin-top:22px;'>"
            f"<a href='{safe_url}' style='background:#0d9488;color:#ffffff;text-decoration:none;"
            "padding:11px 18px;border-radius:10px;display:inline-block;font-weight:700;'>"
            "Join meeting</a></p>"
        )

    guest_html = (
        f"<p>Hi {escape(booking.guest_name)},</p>"
        "<p>Your booking is confirmed.</p>"
        + _booking_email_detail_table([
            ("Meeting", link.name, False),
            ("When", guest_when, False),
            ("Location", location, bool(booking.meeting_url)),
            ("Host", owner_name, False),
        ])
        + meeting_cta
        + _booking_email_note_block("Your note", booking.note)
    )
    owner_html = (
        "<p>New booking confirmed.</p>"
        + _booking_email_detail_table([
            ("Meeting", link.name, False),
            ("When", owner_when, False),
            ("Guest", f"{booking.guest_name} <{booking.guest_email}>", False),
            ("Location", location, bool(booking.meeting_url)),
        ])
        + _booking_email_note_block("Guest note", booking.note)
    )
    guest_ok = await send_common_email(booking.guest_email, subject, guest_html)
    owner_ok = await send_common_email(owner_email, subject, owner_html)
    return bool(guest_ok and owner_ok)


@router.get("", response_model=CalendarSettingsResponse)
async def get_calendar_settings(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's calendar preferences and calendar OAuth accounts."""
    snapshot = await _CalendarConnectionSnapshotFactory.create(
        db,
        user,
        providers=[provider.value for provider in CalendarProvider],
    )
    settings = _canonical_calendar_settings_account_ids(
        _normalize_settings(user),
        snapshot,
    )
    return CalendarSettingsResponse(
        settings=_with_booking_urls(settings, user, request),
        connections=await _connection_options(db, user, snapshot=snapshot),
    )


@router.get("/calendars", response_model=CalendarOptionsResponse)
async def list_calendar_options(
    provider: str | None = Query(None),
    connection_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List calendars in one connected account for booking and conflict settings."""
    settings = _normalize_settings(user)
    selected_provider = provider if provider is not None else settings.provider
    selected_connection_id = connection_id if connection_id is not None else settings.connection_id
    if selected_provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(400, "Unsupported calendar provider")
    if not selected_provider:
        return CalendarOptionsResponse(provider="", connection_id=selected_connection_id)
    return CalendarOptionsResponse(
        provider=selected_provider,
        connection_id=selected_connection_id,
        calendars=await _calendar_options(
            db,
            user,
            selected_provider,
            selected_connection_id,
        ),
    )


@router.put("", response_model=CalendarSettingsResponse)
async def update_calendar_settings(
    body: CalendarSettingsWrite,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Patch the current user's personal calendar settings."""
    user = await lock_calendar_settings_user(db, user.id)
    if not user:
        raise HTTPException(404, "User not found")
    settings = _normalize_settings(user)
    patch = body.model_dump(exclude_unset=True)
    if "provider" in patch and patch["provider"] not in _SUPPORTED_PROVIDERS:
        raise HTTPException(400, "Unsupported calendar provider")
    if "connection_id" in patch:
        patch["connection_id"] = str(patch["connection_id"] or "").strip() or None
    next_provider = patch.get("provider", settings.provider)
    provider_changed = next_provider != settings.provider
    saved_connection_id = await _canonical_calendar_connection_id(
        db,
        user,
        settings.provider,
        settings.connection_id,
    )
    connection_changed = False
    if "connection_id" in patch:
        patch["connection_id"] = await _canonical_calendar_connection_id(
            db,
            user,
            next_provider,
            patch["connection_id"] if next_provider else None,
            require_connected=bool(next_provider and patch["connection_id"]),
        )
        connection_changed = patch["connection_id"] != saved_connection_id
    elif provider_changed:
        patch["connection_id"] = None
    if "conflict_sources" in patch:
        conflict_sources = [
            CalendarConflictSource.model_validate(item)
            for item in (patch["conflict_sources"] or [])
        ]
        patch["conflict_sources"] = await _canonical_conflict_sources(
            db,
            user,
            conflict_sources,
        )
    account_changed = provider_changed or connection_changed
    if account_changed:
        next_default_calendar_id = patch.get("default_calendar_id") or "primary"
        patch.setdefault("default_calendar_id", next_default_calendar_id)
        patch.setdefault("conflict_calendar_ids", [next_default_calendar_id])
        patch.setdefault("visible_calendar_ids", [next_default_calendar_id])
    for key, value in patch.items():
        if key == "booking_defaults":
            value = BookingDefaults.model_validate(value or {})
        elif key == "working_hours":
            value = [WorkingHourWindow.model_validate(item) for item in (value or [])]
        setattr(settings, key, value)
    if not settings.default_calendar_id:
        settings.default_calendar_id = "primary"
    if not settings.conflict_calendar_ids:
        settings.conflict_calendar_ids = [settings.default_calendar_id]
    if not settings.visible_calendar_ids:
        settings.visible_calendar_ids = [settings.default_calendar_id]
    if account_changed:
        updated_at = _now_iso()
        settings.booking_links = [
            BookingLink.model_validate({
                **link.model_dump(),
                "calendar_id": settings.default_calendar_id,
                "updated_at": updated_at,
            })
            for link in settings.booking_links
        ]
    settings = await _save_settings(db, user, settings)
    await db.commit()
    return CalendarSettingsResponse(
        settings=_with_booking_urls(settings, user, request),
        connections=await _connection_options(db, user),
    )


@router.post("/booking-links", response_model=BookingLink)
async def create_booking_link(
    body: BookingLinkWrite,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await lock_calendar_settings_user(db, user.id)
    if not user:
        raise HTTPException(404, "User not found")
    settings = _normalize_settings(user)
    defaults = settings.booking_defaults
    now = _now_iso()
    link = BookingLink(
        id=generate_ulid(),
        slug=_unique_slug(body.slug or body.name, settings.booking_links),
        name=body.name.strip() or "Booking link",
        description=body.description,
        duration_minutes=body.duration_minutes or defaults.duration_minutes,
        location_type=body.location_type or "video",
        location_detail=body.location_detail,
        calendar_id=body.calendar_id or settings.default_calendar_id,
        enabled=True if body.enabled is None else body.enabled,
        color=body.color or _DEFAULT_COLOR,
        buffer_before_minutes=body.buffer_before_minutes if body.buffer_before_minutes is not None else defaults.buffer_before_minutes,
        buffer_after_minutes=body.buffer_after_minutes if body.buffer_after_minutes is not None else defaults.buffer_after_minutes,
        min_notice_minutes=body.min_notice_minutes if body.min_notice_minutes is not None else defaults.min_notice_minutes,
        rolling_window_days=body.rolling_window_days if body.rolling_window_days is not None else defaults.rolling_window_days,
        availability_mode=body.availability_mode or "default",
        working_hours=(
            body.working_hours
            if body.working_hours is not None
            else (settings.working_hours if body.availability_mode == "custom" else [])
        ),
        unavailable_dates=body.unavailable_dates or [],
        unavailable_times=body.unavailable_times or [],
        created_at=now,
        updated_at=now,
    )
    settings.booking_links.append(link)
    await _save_settings(db, user, settings)
    await db.commit()
    data = link.model_dump()
    data["url"] = _booking_url(request, user, link.slug)
    return data


@router.put("/booking-links/{link_id}", response_model=BookingLink)
async def update_booking_link(
    link_id: str,
    body: BookingLinkWrite,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await lock_calendar_settings_user(db, user.id)
    if not user:
        raise HTTPException(404, "User not found")
    settings = _normalize_settings(user)
    for idx, link in enumerate(settings.booking_links):
        if link.id != link_id:
            continue
        patch = body.model_dump(exclude_unset=True)
        for key in (
            "duration_minutes",
            "location_type",
            "enabled",
            "color",
            "buffer_before_minutes",
            "buffer_after_minutes",
            "min_notice_minutes",
            "rolling_window_days",
            "availability_mode",
        ):
            if patch.get(key) is None:
                patch.pop(key, None)
        if "slug" in patch:
            patch["slug"] = _unique_slug(patch.get("slug") or patch.get("name") or link.name, settings.booking_links, excluding_id=link_id)
        if "name" in patch:
            patch["name"] = str(patch["name"]).strip() or link.name
        for key in ("working_hours", "unavailable_dates", "unavailable_times"):
            if key in patch and patch[key] is None:
                patch[key] = []
        updated = BookingLink.model_validate({
            **link.model_dump(),
            **patch,
            "updated_at": _now_iso(),
        })
        settings.booking_links[idx] = updated
        await _save_settings(db, user, settings)
        await db.commit()
        data = updated.model_dump()
        data["url"] = _booking_url(request, user, updated.slug)
        return data
    raise HTTPException(404, "Booking link not found")


@router.delete("/booking-links/{link_id}", status_code=204)
async def delete_booking_link(
    link_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await lock_calendar_settings_user(db, user.id)
    if not user:
        raise HTTPException(404, "User not found")
    settings = _normalize_settings(user)
    next_links = [link for link in settings.booking_links if link.id != link_id]
    if len(next_links) == len(settings.booking_links):
        raise HTTPException(404, "Booking link not found")
    settings.booking_links = next_links
    await _save_settings(db, user, settings)
    await db.commit()


@router.get("/events", response_model=ExternalCalendarEventsResponse)
async def get_external_calendar_events(
    start: date = Query(..., description="Visible range start date, inclusive."),
    end: date = Query(..., description="Visible range end date, exclusive."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return external calendar events for the user's visible Task calendar range."""
    settings = _normalize_settings(user)
    provider = settings.provider
    tz = _timezone(settings.timezone)
    local_start = datetime.combine(start, time.min, tzinfo=tz)
    local_end = datetime.combine(end, time.min, tzinfo=tz)
    if local_end <= local_start:
        raise HTTPException(400, "end must be after start")
    if local_end - local_start > timedelta(days=370):
        raise HTTPException(400, "Calendar event range is too large")

    response_base = {
        "provider": provider or "",
        "connection_id": settings.connection_id,
        "timezone": settings.timezone,
        "range_start": local_start.isoformat(),
        "range_end": local_end.isoformat(),
        "synced_at": _now_iso(),
    }
    if provider not in {"google_calendar", "ms_calendar"}:
        return ExternalCalendarEventsResponse(**response_base, events=[])

    credential = await _resolve_calendar_credential(
        db,
        user,
        provider,
        settings.connection_id,
    )
    if not credential:
        raise HTTPException(503, _CALENDAR_SYNC_ERROR)

    # Credential selection may query the request session. Release its
    # transaction before Nango and calendar-provider network calls so polling
    # the calendar cannot pin a pooled database connection for their duration.
    await db.rollback()
    token = await _materialize_calendar_credential(credential)
    if not token:
        raise HTTPException(503, _CALENDAR_SYNC_ERROR)

    booking_event_ids = {
        str(booking.calendar_event_id)
        for booking in settings.bookings
        if booking.status == "confirmed" and booking.calendar_event_id
    }
    events: list[ExternalCalendarEvent] = []
    range_args = {
        "time_min": local_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "time_max": local_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    adapter = _calendar_provider_adapter(CalendarProvider(provider))
    for calendar_id in _calendar_ids_for_external_events(settings):
        try:
            items, calendar_name = await adapter.visible_event_lookup(
                token,
                calendar_id,
                range_args,
            )
        except Exception as exc:
            logger.warning(
                "%s visible event sync failed for %s",
                provider,
                calendar_id,
                exc_info=True,
            )
            raise HTTPException(503, _CALENDAR_SYNC_ERROR) from exc
        for item in items:
            if str(item.get("id") or "") in booking_event_ids:
                continue
            event = adapter.normalize_event(
                item,
                calendar_id=calendar_id,
                calendar_name=calendar_name,
                settings=settings,
            )
            if event:
                events.append(event)

    events.sort(key=_external_event_sort_key)
    return ExternalCalendarEventsResponse(**response_base, events=events)


async def _public_booking_link_response(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    month: str | None,
    viewer_timezone: str | None,
) -> PublicBookingLinkResponse:
    range_start, range_end = _public_availability_range(
        settings,
        link,
        month,
        viewer_timezone,
    )
    window_start, window_end = _booking_availability_window(settings, link)
    return PublicBookingLinkResponse(
        owner_id=owner.id,
        slug=link.slug,
        name=link.name,
        description=link.description,
        duration_minutes=link.duration_minutes,
        location_type=link.location_type,
        location_detail=link.location_detail,
        owner_name=owner.display_name or owner.email,
        timezone=settings.timezone,
        working_hours=_link_working_hours(settings, link),
        availability_range_start=window_start.isoformat(),
        availability_range_end=window_end.isoformat(),
        available_slots=await _available_slots(
            db,
            owner,
            settings,
            link,
            range_start=range_start,
            range_end=range_end,
        ),
    )


async def _limit_public_availability(
    request: Request,
    owner_scope: str,
) -> None:
    limit = await _PUBLIC_AVAILABILITY_LIMITER.check(
        f"calendar-public-availability:{owner_scope}:{client_ip(request)}",
        _PUBLIC_AVAILABILITY_RATE_LIMIT,
        _PUBLIC_AVAILABILITY_RATE_WINDOW_SECONDS,
    )
    if not limit.allowed:
        raise HTTPException(
            429,
            "Too many availability requests. Please try again shortly",
            headers={
                "Retry-After": str(
                    limit.retry_after or _PUBLIC_AVAILABILITY_RATE_WINDOW_SECONDS
                )
            },
        )


async def _limit_public_lookup(request: Request) -> None:
    limit = await _PUBLIC_LOOKUP_LIMITER.check(
        f"calendar-public-lookup:{client_ip(request)}",
        _PUBLIC_LOOKUP_RATE_LIMIT,
        _PUBLIC_LOOKUP_RATE_WINDOW_SECONDS,
    )
    if not limit.allowed:
        raise HTTPException(
            429,
            "Too many public link requests. Please try again shortly",
            headers={
                "Retry-After": str(
                    limit.retry_after or _PUBLIC_LOOKUP_RATE_WINDOW_SECONDS
                )
            },
        )


async def _limit_legacy_public_availability(
    request: Request,
    owner_scope: str,
    slug: str,
) -> None:
    limit = await _PUBLIC_LEGACY_AVAILABILITY_LIMITER.check(
        f"calendar-legacy-availability:{owner_scope}:{slug}:{client_ip(request)}",
        _PUBLIC_LEGACY_AVAILABILITY_RATE_LIMIT,
        _PUBLIC_LEGACY_AVAILABILITY_RATE_WINDOW_SECONDS,
    )
    if not limit.allowed:
        raise HTTPException(
            429,
            "Too many full availability requests. Use month=YYYY-MM",
            headers={"Retry-After": str(limit.retry_after or _PUBLIC_LEGACY_AVAILABILITY_RATE_WINDOW_SECONDS)},
        )


async def _limit_public_booking(
    request: Request,
    owner_id: str,
    guest_email: str,
) -> None:
    ip = client_ip(request)
    checks = (
        (
            f"calendar-public-booking-ip:{owner_id}:{ip}",
            _PUBLIC_BOOKING_IP_RATE_LIMIT,
            _PUBLIC_BOOKING_RATE_WINDOW_SECONDS,
        ),
        (
            f"calendar-public-booking-email:{owner_id}:{guest_email}",
            _PUBLIC_BOOKING_EMAIL_RATE_LIMIT,
            _PUBLIC_BOOKING_EMAIL_RATE_WINDOW_SECONDS,
        ),
        (
            f"calendar-public-booking-owner:{owner_id}",
            _PUBLIC_BOOKING_OWNER_RATE_LIMIT,
            _PUBLIC_BOOKING_RATE_WINDOW_SECONDS,
        ),
    )
    for key, max_requests, window_seconds in checks:
        limit = await _PUBLIC_BOOKING_LIMITER.check(
            key,
            max_requests,
            window_seconds,
        )
        if not limit.allowed:
            raise HTTPException(
                429,
                "Too many booking attempts. Please try again later",
                headers={
                    "Retry-After": str(limit.retry_after or window_seconds)
                },
            )


@router.get("/public/booking-links/{slug}", response_model=PublicBookingLinkResponse)
async def get_public_booking_link(
    slug: str,
    request: Request,
    month: str | None = Query(None, max_length=7),
    viewer_timezone: str | None = Query(None, alias="timezone", max_length=100),
    db: AsyncSession = Depends(get_db),
):
    """Legacy public metadata lookup by slug only. Prefer the owner-scoped route."""
    await _limit_public_lookup(request)
    owner, settings, link = await _find_public_booking(db, slug)
    await _limit_public_availability(request, owner.id)
    if month is None:
        await _limit_legacy_public_availability(request, owner.id, slug)
    return await _public_booking_link_response(
        db,
        owner,
        settings,
        link,
        month,
        viewer_timezone,
    )


@router.get("/public/booking-links/u/{owner_id}/{slug}", response_model=PublicBookingLinkResponse)
async def get_public_booking_link_for_owner(
    owner_id: str,
    slug: str,
    request: Request,
    month: str | None = Query(None, max_length=7),
    viewer_timezone: str | None = Query(None, alias="timezone", max_length=100),
    db: AsyncSession = Depends(get_db),
):
    """Public metadata for an owner-scoped booking link."""
    await _limit_public_lookup(request)
    owner, settings, link = await _find_public_booking(db, slug, owner_id=owner_id)
    await _limit_public_availability(request, owner.id)
    if month is None:
        await _limit_legacy_public_availability(request, owner.id, slug)
    return await _public_booking_link_response(
        db,
        owner,
        settings,
        link,
        month,
        viewer_timezone,
    )


@router.post("/public/booking-links/{slug}/book", response_model=BookingConfirmationResponse)
async def book_public_booking_link(
    slug: str,
    body: PublicBookingRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Legacy booking endpoint by slug only. Prefer the owner-scoped route."""
    await _limit_public_lookup(request)
    owner, settings, link = await _find_public_booking(db, slug)
    return await _book_public_link(owner, settings, link, body, request, db)


@router.post("/public/booking-links/u/{owner_id}/{slug}/book", response_model=BookingConfirmationResponse)
async def book_public_booking_link_for_owner(
    owner_id: str,
    slug: str,
    body: PublicBookingRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _limit_public_lookup(request)
    owner, settings, link = await _find_public_booking(db, slug, owner_id=owner_id)
    return await _book_public_link(owner, settings, link, body, request, db)


async def _book_public_link(
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    body: PublicBookingRequest,
    request: Request,
    db: AsyncSession,
) -> BookingConfirmationResponse:
    owner_id = owner.id
    booking_slug = link.slug
    guest_name = body.guest_name.strip()
    guest_email = body.guest_email.strip().lower()
    if not guest_name:
        raise HTTPException(400, "Name is required")
    if not _EMAIL_RE.match(guest_email):
        raise HTTPException(400, "Valid email is required")
    await _limit_public_booking(request, owner_id, guest_email)

    # The route lookup opened a read transaction. Release it before waiting on
    # the per-host guard so queued public requests do not consume DB connections.
    await db.rollback()
    async with _serialize_public_booking(owner_id):
        try:
            owner, settings, link = await _find_public_booking(
                db,
                booking_slug,
                owner_id=owner_id,
            )
            availability_signature = _booking_availability_signature(settings, link)
            tz = _timezone(settings.timezone)
            starts_local = _parse_public_datetime(body.starts_at, tz)

            # Keep the immutable settings snapshot, but release the read-only
            # transaction before resolving tokens and calling the provider.
            await db.commit()
            external_busy_ranges = await _external_busy_ranges(
                db,
                owner,
                settings,
                link,
                starts_local - timedelta(minutes=link.buffer_before_minutes),
                starts_local + timedelta(
                    minutes=link.duration_minutes + link.buffer_after_minutes,
                ),
            )
            await db.rollback()

            owner, settings, link = await _lock_public_booking(db, owner_id, booking_slug)
            owner_email = owner.email
            owner_name = owner.display_name or owner.email
            if _booking_availability_signature(settings, link) != availability_signature:
                raise HTTPException(409, "Availability settings changed. Please choose a time again")
            ends_local = _validate_booking_slot(
                settings,
                link,
                starts_local,
                external_busy_ranges=external_busy_ranges,
            )
            now = _now_iso()
            calendar_account_id = await _booking_calendar_account_id(
                db,
                owner,
                settings.provider,
                settings.connection_id,
            )
            booking = BookingRecord(
                id=generate_ulid(),
                booking_link_id=link.id,
                booking_link_slug=link.slug,
                guest_name=guest_name,
                guest_email=guest_email,
                note=(body.note or "").strip() or None,
                starts_at=starts_local.astimezone(timezone.utc).isoformat(),
                ends_at=ends_local.astimezone(timezone.utc).isoformat(),
                timezone=settings.timezone,
                guest_timezone=_timezone_name(body.timezone, settings.timezone),
                buffer_before_minutes=link.buffer_before_minutes,
                buffer_after_minutes=link.buffer_after_minutes,
                calendar_provider=settings.provider or None,
                calendar_account_id=calendar_account_id,
                calendar_metadata_sync_pending=(
                    settings.provider in {"google_calendar", "ms_calendar"}
                    and calendar_account_id is not None
                ),
                created_at=now,
            )
            booking.calendar_event_intent = _calendar_event_intent(
                settings,
                link,
                booking,
                owner,
            )

            settings.bookings.append(booking)
            await _save_settings(db, owner, settings)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    try:
        event = await _create_external_calendar_event(db, owner, settings, link, booking)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("Calendar booking event creation failed", exc_info=True)
        event = {}
    _apply_calendar_event_metadata(booking, event)
    try:
        booking.email_sent = await _send_booking_confirmation_emails(
            owner_email,
            owner_name,
            settings,
            link,
            booking,
        )
    except Exception:
        logger.warning("Calendar booking confirmation email failed", exc_info=True)
        booking.email_sent = False

    notification_owner = owner
    notification_settings = settings
    persisted_owner, persisted_settings = await _persist_confirmed_booking(
        db,
        owner_id,
        booking,
    )
    if persisted_owner and persisted_settings:
        notification_owner = persisted_owner
        notification_settings = persisted_settings
    if (
        booking.calendar_provider in {"google_calendar", "ms_calendar"}
        and (
            booking.calendar_metadata_sync_pending
            or not persisted_owner
        )
    ):
        _schedule_booking_metadata_reconciliation(owner_id, booking.id)

    try:
        await _send_booking_notification(
            notification_owner,
            notification_settings,
            link,
            booking,
        )
    except Exception:
        logger.warning("Calendar booking notification failed", exc_info=True)
    return BookingConfirmationResponse(
        id=booking.id,
        status=booking.status,
        booking_link_slug=booking.booking_link_slug,
        guest_name=booking.guest_name,
        guest_email=booking.guest_email,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        timezone=booking.guest_timezone or booking.timezone,
        calendar_event_created=booking.calendar_event_created,
        host_email=booking.host_email,
        calendar_event_url=booking.calendar_event_url,
        meeting_url=booking.meeting_url,
        email_sent=booking.email_sent,
    )


@router.get("/day", response_model=DailyAgendaResponse)
async def get_daily_agenda(
    day: date | None = Query(None, description="Local date, YYYY-MM-DD. Defaults to today."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = _normalize_settings(user)
    tz = _timezone(settings.timezone)
    target = day or datetime.now(tz).date()

    local_start = datetime.combine(target, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    start_dt = local_start.astimezone(timezone.utc)
    end_dt = local_end.astimezone(timezone.utc)

    rows = (await db.execute(
        select(Task).where(Task.entity_id == user.entity_id)
    )).scalars().all()

    items: list[DailyAgendaItem] = []
    for task in rows:
        if isinstance(task.details, dict) and task.details.get("scheduled_job_id"):
            continue
        starts_at = _task_schedule_start(task, settings)
        if not starts_at:
            continue
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)
        starts_utc = starts_at.astimezone(timezone.utc)
        if not (start_dt <= starts_utc < end_dt):
            continue
        duration = None
        if isinstance(task.details, dict) and task.details.get("duration_minutes"):
            try:
                duration = int(task.details["duration_minutes"])
            except Exception:
                duration = None
        ends_at = starts_at + timedelta(minutes=duration) if duration else None
        items.append(DailyAgendaItem(
            id=f"task:{task.id}",
            source="task",
            title=task.title,
            starts_at=starts_at.isoformat(),
            ends_at=ends_at.isoformat() if ends_at else None,
            status=task.status,
            priority=task.priority,
            task_id=task.id,
            workspace_id=task.workspace_id,
        ))

    links_by_id = {link.id: link for link in settings.booking_links}
    for booking in settings.bookings:
        if booking.status != "confirmed":
            continue
        times = _booking_times(booking, tz)
        if not times:
            continue
        starts_local, ends_local = times
        starts_utc = starts_local.astimezone(timezone.utc)
        if not (start_dt <= starts_utc < end_dt):
            continue
        link = links_by_id.get(booking.booking_link_id)
        meeting_name = link.name if link else booking.booking_link_slug.replace("-", " ").title()
        items.append(DailyAgendaItem(
            id=f"booking:{booking.id}",
            source="booking",
            title=f"{booking.guest_name} · {meeting_name}",
            starts_at=starts_local.isoformat(),
            ends_at=ends_local.isoformat(),
            status=booking.status,
            booking_id=booking.id,
            booking_link_id=booking.booking_link_id,
            booking_link_slug=booking.booking_link_slug,
            guest_name=booking.guest_name,
            guest_email=booking.guest_email,
        ))
    items.sort(key=lambda item: item.starts_at)
    return DailyAgendaResponse(date=target.isoformat(), timezone=settings.timezone, items=items)
