"""Shared helpers for user-local time semantics.

Database timestamps stay in UTC. These helpers are for user-facing concepts
such as "today", date buckets, and date-only task deadlines.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc


def load_user_timezone(timezone_name: str | None) -> ZoneInfo:
    """Return a valid IANA timezone, falling back to UTC."""
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def user_timezone_name(timezone_name: str | None) -> str:
    """Return a DB-safe IANA timezone name."""
    return load_user_timezone(timezone_name).key


def utc_now() -> datetime:
    return datetime.now(UTC)


def user_now(timezone_name: str | None, now: datetime | None = None) -> datetime:
    """Return ``now`` converted to the user's timezone."""
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(load_user_timezone(timezone_name))


def user_current_date(timezone_name: str | None, now: datetime | None = None) -> date:
    """The calendar date the user sees as today."""
    return user_now(timezone_name, now).date()


def user_day_bounds_utc(
    timezone_name: str | None,
    day: date | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return UTC [start, end) bounds for a user-local calendar day."""
    tz = load_user_timezone(timezone_name)
    local_day = day or user_current_date(timezone_name, now)
    start_local = datetime.combine(local_day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def user_range_start_utc(
    timezone_name: str | None,
    days: int,
    *,
    now: datetime | None = None,
) -> datetime:
    """Start of the earliest local day in a rolling ``days`` day window."""
    safe_days = max(1, int(days or 1))
    start_day = user_current_date(timezone_name, now) - timedelta(days=safe_days - 1)
    start, _ = user_day_bounds_utc(timezone_name, start_day)
    return start
