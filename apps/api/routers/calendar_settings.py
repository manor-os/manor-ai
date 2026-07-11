"""Personal calendar settings, booking links, and daily agenda endpoints."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.document import Integration
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.user import OAuthAccount, User
from packages.core.services.notify import notify
from packages.core.services.provider_keys import provider_key_aliases
from packages.core.services.settings_service import update_user_preferences
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/calendar-settings", tags=["calendar-settings"])
logger = logging.getLogger(__name__)

_PREF_KEY = "calendar_settings"
_SUPPORTED_PROVIDERS = {"", "google_calendar", "ms_calendar"}
_DEFAULT_COLOR = "#4f7d75"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class WorkingHourWindow(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    enabled: bool = True
    start: str = "09:00"
    end: str = "17:00"


class BookingDefaults(BaseModel):
    duration_minutes: int = Field(default=30, ge=5, le=480)
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=10, ge=0, le=240)
    min_notice_minutes: int = Field(default=120, ge=0, le=43200)
    rolling_window_days: int = Field(default=30, ge=1, le=365)


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
    created_at: str | None = None
    updated_at: str | None = None
    url: str | None = None


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
    status: Literal["confirmed", "cancelled"] = "confirmed"
    calendar_provider: str | None = None
    calendar_account_id: str | None = None
    calendar_event_id: str | None = None
    calendar_event_url: str | None = None
    meeting_url: str | None = None
    calendar_event_created: bool = False
    email_sent: bool = False
    created_at: str | None = None


class CalendarSettings(BaseModel):
    provider: str = ""
    connection_id: str | None = None
    default_calendar_id: str = "primary"
    conflict_calendar_ids: list[str] = Field(default_factory=lambda: ["primary"])
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
    provider: str | None = None
    connection_id: str | None = None
    default_calendar_id: str | None = None
    conflict_calendar_ids: list[str] | None = None
    visible_calendar_ids: list[str] | None = None
    timezone: str | None = None
    working_hours: list[WorkingHourWindow] | None = None
    booking_defaults: BookingDefaults | None = None
    auto_create_events_from_tasks: bool | None = None
    track_task_deadlines: bool | None = None
    track_scheduled_tasks: bool | None = None


class CalendarConnectionOption(BaseModel):
    id: str
    provider: str
    display_name: str
    provider_user_id: str
    is_default: bool = False
    expires_at: str | None = None


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
    available_slots: list[AvailableSlot] = Field(default_factory=list)


class PublicBookingRequest(BaseModel):
    starts_at: str
    guest_name: str = Field(min_length=1, max_length=160)
    guest_email: str = Field(min_length=3, max_length=320)
    note: str | None = Field(default=None, max_length=2000)


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


def _raw_calendar_settings(user: User) -> dict[str, Any]:
    prefs = user.preferences or {}
    raw = prefs.get(_PREF_KEY)
    return raw if isinstance(raw, dict) else {}


def _normalize_settings(user: User) -> CalendarSettings:
    raw = dict(_raw_calendar_settings(user))
    raw.setdefault("timezone", user.timezone or "UTC")
    raw.setdefault("default_calendar_id", "primary")
    raw.setdefault("conflict_calendar_ids", ["primary"])
    raw.setdefault("visible_calendar_ids", raw.get("conflict_calendar_ids") or ["primary"])
    raw.setdefault("working_hours", _default_working_hours())
    raw.setdefault("booking_defaults", {})
    raw.setdefault("booking_links", [])
    raw.setdefault("bookings", [])

    if raw.get("provider") not in _SUPPORTED_PROVIDERS:
        raw["provider"] = ""
    if not isinstance(raw.get("conflict_calendar_ids"), list):
        raw["conflict_calendar_ids"] = ["primary"]
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
    user.preferences = {**(user.preferences or {}), _PREF_KEY: settings.model_dump()}
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

            result = await google_calendar.call_tool("list_calendars", {}, token)
            if result.get("isError"):
                return None
            data = _json_result(result)
            calendars = data.get("items") or []
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


async def _connection_options(db: AsyncSession, user: User) -> list[CalendarConnectionOption]:
    providers = sorted({
        *provider_key_aliases("google_calendar"),
        *provider_key_aliases("ms_calendar"),
    })
    rows = (await db.execute(
        select(OAuthAccount)
        .where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider.in_(providers),
        )
        .order_by(OAuthAccount.created_at.asc())
    )).scalars().all()
    options: list[CalendarConnectionOption] = []
    seen: set[str] = set()
    for row in rows:
        profile = row.profile or {}
        email = _first_email(profile.get("email"), row.provider_user_id)
        if not email:
            email = await _calendar_email_from_token(row.provider, row.access_token)
        display = _calendar_account_label(
            row.provider,
            email,
            profile.get("display_name"),
            profile.get("name"),
            row.provider_user_id,
        )
        seen.add(row.id)
        options.append(CalendarConnectionOption(
            id=row.id,
            provider=row.provider,
            display_name=display,
            provider_user_id=row.provider_user_id,
            is_default=bool(profile.get("is_default", False)),
            expires_at=row.token_expires_at.isoformat() if row.token_expires_at else None,
        ))
    integrations = (await db.execute(
        select(Integration)
        .where(
            Integration.entity_id == user.entity_id,
            Integration.provider.in_(providers),
            Integration.status == "active",
        )
        .order_by(Integration.created_at.asc())
    )).scalars().all()
    for row in integrations:
        if row.id in seen:
            continue
        cfg = row.config or {}
        profile = cfg.get("profile") if isinstance(cfg.get("profile"), dict) else {}
        nango = cfg.get("nango") if isinstance(cfg.get("nango"), dict) else {}
        provider_user_id = str(nango.get("connection_id") or row.id)
        email = _first_email(
            profile.get("email"),
            cfg.get("email"),
            cfg.get("from_email"),
            cfg.get("from_address"),
            provider_user_id,
        )
        if not email:
            email = await _calendar_email_from_token(
                row.provider,
                await _integration_token(db, user, row, row.provider),
            )
        display = _calendar_account_label(
            row.provider,
            email,
            profile.get("display_name"),
            profile.get("name"),
            cfg.get("display_name"),
            cfg.get("name"),
        )
        options.append(CalendarConnectionOption(
            id=row.id,
            provider=row.provider,
            display_name=display,
            provider_user_id=provider_user_id,
            is_default=bool(cfg.get("is_default", False)),
            expires_at=None,
        ))
    return options


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


def _conflict_calendar_ids(settings: CalendarSettings, link: BookingLink, owner: User) -> list[str]:
    raw = settings.conflict_calendar_ids or [
        link.calendar_id or settings.default_calendar_id or "primary"
    ]
    calendars: list[str] = []
    seen: set[str] = set()
    for item in raw:
        calendar_id = str(item or "").strip()
        if not calendar_id:
            continue
        if settings.provider == "ms_calendar" and calendar_id == "primary":
            calendar_id = owner.email
        if calendar_id in seen:
            continue
        seen.add(calendar_id)
        calendars.append(calendar_id)
    if not calendars:
        calendars.append(owner.email if settings.provider == "ms_calendar" else "primary")
    return calendars


def _slot_label(starts_at: datetime, ends_at: datetime) -> str:
    return f"{starts_at.strftime('%-I:%M %p')} - {ends_at.strftime('%-I:%M %p')}"


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

    window = next(
        (
            row for row in settings.working_hours
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

    candidate_start = starts_local - timedelta(minutes=link.buffer_before_minutes)
    candidate_end = ends_local + timedelta(minutes=link.buffer_after_minutes)
    for booking in settings.bookings:
        if booking.status != "confirmed":
            continue
        times = _booking_times(booking, tz)
        if not times:
            continue
        booked_start, booked_end = times
        booked_start -= timedelta(minutes=link.buffer_before_minutes)
        booked_end += timedelta(minutes=link.buffer_after_minutes)
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
    limit: int = 720,
) -> list[AvailableSlot]:
    tz = _timezone(settings.timezone)
    now_local = datetime.now(tz)
    min_start = now_local + timedelta(minutes=link.min_notice_minutes)
    slots: list[AvailableSlot] = []
    days = min(max(link.rolling_window_days, 1), 30)
    external_busy_ranges = await _external_busy_ranges(
        db,
        owner,
        settings,
        link,
        now_local,
        now_local + timedelta(days=days + 1),
    )

    for offset in range(days + 1):
        target = now_local.date() + timedelta(days=offset)
        window = next(
            (
                row for row in settings.working_hours
                if row.day_of_week == target.weekday() and row.enabled
            ),
            None,
        )
        if not window:
            continue
        cursor = datetime.combine(target, _parse_time_hhmm(window.start, time(9, 0)), tzinfo=tz)
        work_end = datetime.combine(target, _parse_time_hhmm(window.end, time(17, 0)), tzinfo=tz)
        if cursor < min_start:
            minute = 0 if min_start.minute == 0 else 30 if min_start.minute <= 30 else 60
            cursor = min_start.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
        step = timedelta(minutes=15 if link.duration_minutes <= 20 else 30)
        while cursor + timedelta(minutes=link.duration_minutes) <= work_end:
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
            if len(slots) >= limit:
                return slots
            cursor += step
    return slots


async def _find_public_booking(
    db: AsyncSession,
    slug: str,
    owner_id: str | None = None,
) -> tuple[User, CalendarSettings, BookingLink]:
    query = select(User).where(User.deleted_at.is_(None), User.status == "active")
    if owner_id:
        query = query.where(User.id == owner_id)
    rows = (await db.execute(query)).scalars().all()
    for owner in rows:
        settings = _normalize_settings(owner)
        for link in settings.booking_links:
            if link.slug == slug and link.enabled:
                return owner, settings, link
    raise HTTPException(404, "Booking link not found")


async def _integration_token(db: AsyncSession, owner: User, row: Integration, provider: str) -> str | None:
    nango_meta = (row.config or {}).get("nango") or {}
    connection_id = nango_meta.get("connection_id")
    if connection_id:
        from packages.core.ai.tools.mcp_builtin import _fetch_token_via_nango
        return await _fetch_token_via_nango(
            db,
            entity_id=owner.entity_id,
            provider_config_key=nango_meta.get("provider_config_key") or provider,
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


async def _resolve_calendar_token(
    db: AsyncSession,
    owner: User,
    provider: str,
    account_id: str | None,
) -> str | None:
    aliases = provider_key_aliases(provider)
    if account_id:
        oauth = (await db.execute(
            select(OAuthAccount).where(
                OAuthAccount.id == account_id,
                OAuthAccount.user_id == owner.id,
                OAuthAccount.provider.in_(aliases),
            )
        )).scalar_one_or_none()
        if oauth and oauth.access_token:
            return oauth.access_token
        integration = (await db.execute(
            select(Integration).where(
                Integration.id == account_id,
                Integration.entity_id == owner.entity_id,
                Integration.provider.in_(aliases),
                Integration.status == "active",
            )
        )).scalar_one_or_none()
        if integration:
            return await _integration_token(db, owner, integration, provider)
        return None

    oauth_rows = (await db.execute(
        select(OAuthAccount)
        .where(
            OAuthAccount.user_id == owner.id,
            OAuthAccount.provider.in_(aliases),
        )
        .order_by(OAuthAccount.created_at.desc())
    )).scalars().all()
    if oauth_rows:
        chosen = next((row for row in oauth_rows if (row.profile or {}).get("is_default")), oauth_rows[0])
        if chosen.access_token:
            return chosen.access_token

    integrations = (await db.execute(
        select(Integration)
        .where(
            Integration.entity_id == owner.entity_id,
            Integration.provider.in_(aliases),
            Integration.status == "active",
        )
        .order_by(Integration.created_at.desc())
    )).scalars().all()
    if integrations:
        chosen = next((row for row in integrations if (row.config or {}).get("is_default")), integrations[0])
        return await _integration_token(db, owner, chosen, provider)
    return None


def _calendar_description(link: BookingLink, booking: BookingRecord, owner: User) -> str:
    lines = [
        f"Booked via Manor AI.",
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


def _all_day_datetime(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return f"{text}T00:00:00"


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
    starts_at = str(start.get("dateTime") or _all_day_datetime(start.get("date")) or "")
    if not starts_at:
        return None
    ends_at = str(end.get("dateTime") or _all_day_datetime(end.get("date")) or "") or None
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
    starts_at = str(start.get("dateTime") or "").strip()
    if not starts_at:
        return None
    ends_at = str(end.get("dateTime") or "").strip() or None
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


async def _external_busy_ranges(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[datetime, datetime]]:
    provider = settings.provider
    if provider not in {"google_calendar", "ms_calendar"}:
        return []

    token = await _resolve_calendar_token(db, owner, provider, settings.connection_id)
    if not token:
        return []

    tz = _timezone(settings.timezone)
    calendars = _conflict_calendar_ids(settings, link, owner)
    busy_ranges: list[tuple[datetime, datetime]] = []

    try:
        if provider == "google_calendar":
            from packages.core.ai.mcp import google_calendar

            result = await google_calendar.call_tool("freebusy_query", {
                "calendars": calendars,
                "time_min": _iso_utc(range_start),
                "time_max": _iso_utc(range_end),
                "timezone": settings.timezone,
            }, token)
            if result.get("isError"):
                logger.warning("Google Calendar free/busy failed: %s", _result_text(result))
                return []
            data = _json_result(result)
            for calendar in (data.get("calendars") or {}).values():
                for item in (calendar or {}).get("busy") or []:
                    start = _parse_external_datetime(item.get("start"), tz)
                    end = _parse_external_datetime(item.get("end"), tz)
                    if start and end and start < end:
                        busy_ranges.append((start, end))
            return busy_ranges

        from packages.core.ai.mcp import ms_calendar

        range_start_local = range_start.astimezone(tz)
        range_end_local = range_end.astimezone(tz)
        result = await ms_calendar.call_tool("get_schedule", {
            "calendars": calendars,
            "time_min": range_start_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "time_max": range_end_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "interval_minutes": 15,
            "timezone": settings.timezone,
        }, token)
        if result.get("isError"):
            logger.warning("MS Calendar free/busy failed: %s", _result_text(result))
            return []
        data = _json_result(result)
        for schedule in data.get("value") or []:
            for item in (schedule or {}).get("scheduleItems") or []:
                status = str(item.get("status") or "").lower()
                if status not in {"busy", "tentative", "oof", "workingelsewhere"}:
                    continue
                start = _parse_external_datetime(item.get("start"), tz)
                end = _parse_external_datetime(item.get("end"), tz)
                if start and end and start < end:
                    busy_ranges.append((start, end))
    except Exception:
        logger.warning("Calendar free/busy lookup failed", exc_info=True)
        return []

    return busy_ranges


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
    )


async def _create_external_calendar_event(
    db: AsyncSession,
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    booking: BookingRecord,
) -> dict[str, Any]:
    provider = settings.provider
    if provider not in {"google_calendar", "ms_calendar"}:
        return {}
    token = await _resolve_calendar_token(db, owner, provider, settings.connection_id)
    if not token:
        return {}

    tz = _timezone(settings.timezone)
    starts_at = _parse_datetime(booking.starts_at)
    ends_at = _parse_datetime(booking.ends_at)
    if not starts_at or not ends_at:
        return {}
    starts_local = starts_at.astimezone(tz)
    ends_local = ends_at.astimezone(tz)
    summary = f"{link.name} with {booking.guest_name}"
    location = _calendar_location(link)

    if provider == "google_calendar":
        from packages.core.ai.mcp import google_calendar
        result = await google_calendar.call_tool("create_event", {
            "summary": summary,
            "start_time": starts_local.isoformat(),
            "end_time": ends_local.isoformat(),
            "description": _calendar_description(link, booking, owner),
            "location": location,
            "attendees": [booking.guest_email],
            "calendar_id": link.calendar_id or settings.default_calendar_id or "primary",
            "create_meet_link": link.location_type == "video" and not link.location_detail,
            "conference_request_id": booking.id,
        }, token)
        if result.get("isError"):
            logger.warning("Google Calendar booking event failed: %s", _result_text(result))
            return {}
        data = _json_result(result)
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
        }

    from packages.core.ai.mcp import ms_calendar
    result = await ms_calendar.call_tool("create_event", {
        "subject": summary,
        "start_time": starts_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time": ends_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": settings.timezone,
        "body": escape(_calendar_description(link, booking, owner)).replace("\n", "<br />"),
        "location": location,
        "attendees": [booking.guest_email],
        "calendar_id": None if (link.calendar_id or settings.default_calendar_id) == "primary" else (link.calendar_id or settings.default_calendar_id),
        "is_online_meeting": link.location_type == "video" and not link.location_detail,
    }, token)
    if result.get("isError"):
        logger.warning("MS Calendar booking event failed: %s", _result_text(result))
        return {}
    data = _json_result(result)
    online = data.get("onlineMeeting") or {}
    return {
        "calendar_event_id": data.get("id"),
        "calendar_event_url": data.get("webLink"),
        "meeting_url": online.get("joinUrl"),
    }


async def _send_booking_confirmation_emails(
    owner: User,
    link: BookingLink,
    booking: BookingRecord,
) -> bool:
    from packages.core.services.email_service import send_common_email

    when = _booking_display_when(booking)
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
            ("When", when, False),
            ("Location", location, bool(booking.meeting_url)),
            ("Host", owner.display_name or owner.email, False),
        ])
        + meeting_cta
        + _booking_email_note_block("Your note", booking.note)
    )
    owner_html = (
        "<p>New booking confirmed.</p>"
        + _booking_email_detail_table([
            ("Meeting", link.name, False),
            ("When", when, False),
            ("Guest", f"{booking.guest_name} <{booking.guest_email}>", False),
            ("Location", location, bool(booking.meeting_url)),
        ])
        + _booking_email_note_block("Guest note", booking.note)
    )
    guest_ok = await send_common_email(booking.guest_email, subject, guest_html)
    owner_ok = await send_common_email(owner.email, subject, owner_html)
    return bool(guest_ok and owner_ok)


@router.get("", response_model=CalendarSettingsResponse)
async def get_calendar_settings(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's calendar preferences and calendar OAuth accounts."""
    return CalendarSettingsResponse(
        settings=_with_booking_urls(_normalize_settings(user), user, request),
        connections=await _connection_options(db, user),
    )


@router.put("", response_model=CalendarSettingsResponse)
async def update_calendar_settings(
    body: CalendarSettingsWrite,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Patch the current user's personal calendar settings."""
    settings = _normalize_settings(user)
    patch = body.model_dump(exclude_unset=True)
    if "provider" in patch and patch["provider"] not in _SUPPORTED_PROVIDERS:
        raise HTTPException(400, "Unsupported calendar provider")
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
    settings = await _save_settings(db, user, settings)
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
        created_at=now,
        updated_at=now,
    )
    settings.booking_links.append(link)
    await _save_settings(db, user, settings)
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
    settings = _normalize_settings(user)
    for idx, link in enumerate(settings.booking_links):
        if link.id != link_id:
            continue
        patch = body.model_dump(exclude_unset=True)
        if "slug" in patch:
            patch["slug"] = _unique_slug(patch.get("slug") or patch.get("name") or link.name, settings.booking_links, excluding_id=link_id)
        if "name" in patch:
            patch["name"] = str(patch["name"]).strip() or link.name
        updated = link.model_copy(update={**patch, "updated_at": _now_iso()})
        settings.booking_links[idx] = updated
        await _save_settings(db, user, settings)
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
    settings = _normalize_settings(user)
    next_links = [link for link in settings.booking_links if link.id != link_id]
    if len(next_links) == len(settings.booking_links):
        raise HTTPException(404, "Booking link not found")
    settings.booking_links = next_links
    await _save_settings(db, user, settings)


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

    token = await _resolve_calendar_token(db, user, provider, settings.connection_id)
    if not token:
        return ExternalCalendarEventsResponse(**response_base, events=[])

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

    calendar_ids = _calendar_ids_for_external_events(settings)
    if provider == "google_calendar":
        from packages.core.ai.mcp import google_calendar

        for calendar_id in calendar_ids:
            result = await google_calendar.call_tool("list_events", {
                **range_args,
                "calendar_id": calendar_id,
                "max_results": 250,
            }, token)
            if result.get("isError"):
                logger.warning("Google Calendar visible event sync failed for %s: %s", calendar_id, _result_text(result))
                continue
            data = _json_result(result)
            for item in data.get("items") or []:
                if not isinstance(item, dict) or str(item.get("id") or "") in booking_event_ids:
                    continue
                event = _normalize_google_event(
                    item,
                    calendar_id=calendar_id,
                    calendar_name=data.get("summary") or calendar_id,
                    settings=settings,
                )
                if event:
                    events.append(event)
    else:
        from packages.core.ai.mcp import ms_calendar

        for calendar_id in calendar_ids:
            args = {
                **range_args,
                "top": 250,
                "select": "id,subject,start,end,webLink,onlineMeeting,onlineMeetingUrl,location,bodyPreview,attendees,organizer,showAs,isCancelled,isAllDay",
            }
            if calendar_id and calendar_id != "primary":
                args["calendar_id"] = calendar_id
            result = await ms_calendar.call_tool("list_events", args, token)
            if result.get("isError"):
                logger.warning("Microsoft Calendar visible event sync failed for %s: %s", calendar_id, _result_text(result))
                continue
            data = _json_result(result)
            for item in data.get("value") or []:
                if not isinstance(item, dict) or str(item.get("id") or "") in booking_event_ids:
                    continue
                event = _normalize_ms_event(
                    item,
                    calendar_id=calendar_id or "primary",
                    calendar_name=calendar_id or "Primary calendar",
                    settings=settings,
                )
                if event:
                    events.append(event)

    events.sort(key=_external_event_sort_key)
    return ExternalCalendarEventsResponse(**response_base, events=events)


@router.get("/public/booking-links/{slug}", response_model=PublicBookingLinkResponse)
async def get_public_booking_link(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Legacy public metadata lookup by slug only. Prefer the owner-scoped route."""
    owner, settings, link = await _find_public_booking(db, slug)
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
        working_hours=settings.working_hours,
        available_slots=await _available_slots(db, owner, settings, link),
    )


@router.get("/public/booking-links/u/{owner_id}/{slug}", response_model=PublicBookingLinkResponse)
async def get_public_booking_link_for_owner(
    owner_id: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Public metadata for an owner-scoped booking link."""
    owner, settings, link = await _find_public_booking(db, slug, owner_id=owner_id)
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
        working_hours=settings.working_hours,
        available_slots=await _available_slots(db, owner, settings, link),
    )


@router.post("/public/booking-links/{slug}/book", response_model=BookingConfirmationResponse)
async def book_public_booking_link(
    slug: str,
    body: PublicBookingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Legacy booking endpoint by slug only. Prefer the owner-scoped route."""
    owner, settings, link = await _find_public_booking(db, slug)
    return await _book_public_link(owner, settings, link, body, db)


@router.post("/public/booking-links/u/{owner_id}/{slug}/book", response_model=BookingConfirmationResponse)
async def book_public_booking_link_for_owner(
    owner_id: str,
    slug: str,
    body: PublicBookingRequest,
    db: AsyncSession = Depends(get_db),
):
    owner, settings, link = await _find_public_booking(db, slug, owner_id=owner_id)
    return await _book_public_link(owner, settings, link, body, db)


async def _book_public_link(
    owner: User,
    settings: CalendarSettings,
    link: BookingLink,
    body: PublicBookingRequest,
    db: AsyncSession,
) -> BookingConfirmationResponse:
    guest_name = body.guest_name.strip()
    guest_email = body.guest_email.strip().lower()
    if not guest_name:
        raise HTTPException(400, "Name is required")
    if not _EMAIL_RE.match(guest_email):
        raise HTTPException(400, "Valid email is required")

    tz = _timezone(settings.timezone)
    starts_local = _parse_public_datetime(body.starts_at, tz)
    external_busy_ranges = await _external_busy_ranges(
        db,
        owner,
        settings,
        link,
        starts_local - timedelta(minutes=link.buffer_before_minutes),
        starts_local + timedelta(minutes=link.duration_minutes + link.buffer_after_minutes),
    )
    ends_local = _validate_booking_slot(
        settings,
        link,
        starts_local,
        external_busy_ranges=external_busy_ranges,
    )
    now = _now_iso()
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
        calendar_provider=settings.provider or None,
        calendar_account_id=settings.connection_id,
        created_at=now,
    )

    event = await _create_external_calendar_event(db, owner, settings, link, booking)
    if event.get("calendar_event_id") or event.get("calendar_event_url"):
        booking.calendar_event_created = True
        booking.calendar_event_id = event.get("calendar_event_id")
        booking.calendar_event_url = event.get("calendar_event_url")
        booking.meeting_url = event.get("meeting_url")
    booking.email_sent = await _send_booking_confirmation_emails(owner, link, booking)

    settings.bookings.append(booking)
    await _save_settings(db, owner, settings)
    await _send_booking_notification(owner, settings, link, booking)
    return BookingConfirmationResponse(
        id=booking.id,
        status=booking.status,
        booking_link_slug=booking.booking_link_slug,
        guest_name=booking.guest_name,
        guest_email=booking.guest_email,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        timezone=booking.timezone,
        calendar_event_created=booking.calendar_event_created,
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
