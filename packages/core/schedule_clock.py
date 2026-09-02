"""Typed scheduling clock helpers shared by writers and the scheduler tick."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.core.cron import cron_field_matches


INTERVAL_SCHEDULE_KINDS = frozenset({"every", "interval"})
_CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_GREGORIAN_CYCLE_DAYS = 146_097


def _schedule_timezone(timezone_name: object) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(str(timezone_name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _allowed_cron_values(
    pattern: str,
    *,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    return tuple(
        value
        for value in range(minimum, maximum + 1)
        if cron_field_matches(
            value,
            pattern,
            minimum=minimum,
            maximum=maximum,
        )
    )


def next_cron_occurrence(
    expression: str,
    *,
    timezone_name: str,
    after: datetime,
    inclusive: bool = False,
) -> datetime | None:
    """Return the next matching cron minute in UTC without minute scanning.

    Manor's five-field cron grammar uses AND semantics for day-of-month and
    day-of-week. Calendar days are therefore the natural bounded search unit;
    the allowed minute/hour sets are computed only once.
    """

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    parts = str(expression or "").strip().split()
    if len(parts) != 5:
        return None
    allowed = tuple(
        _allowed_cron_values(pattern, minimum=minimum, maximum=maximum)
        for pattern, (minimum, maximum) in zip(parts, _CRON_FIELD_RANGES)
    )
    if any(not values for values in allowed):
        return None
    minutes, hours, days, months, weekdays = allowed

    tz = _schedule_timezone(timezone_name)
    local_after = after.astimezone(tz)
    local_floor = local_after.replace(second=0, microsecond=0)
    first_local_minute = (
        local_floor if inclusive else local_floor + timedelta(minutes=1)
    )
    first_utc = first_local_minute.astimezone(timezone.utc)
    current_date = first_local_minute.date()

    for day_offset in range(_GREGORIAN_CYCLE_DAYS + 1):
        candidate_date = current_date + timedelta(days=day_offset)
        cron_weekday = (candidate_date.weekday() + 1) % 7
        if (
            candidate_date.month not in months
            or candidate_date.day not in days
            or cron_weekday not in weekdays
        ):
            continue
        for hour in hours:
            for minute in minutes:
                candidate_local = datetime.combine(
                    candidate_date,
                    time(hour=hour, minute=minute),
                    tzinfo=tz,
                )
                candidate_utc = candidate_local.astimezone(timezone.utc)
                if candidate_utc < first_utc:
                    continue
                # ZoneInfo accepts nonexistent DST wall times. A UTC
                # round-trip rejects those minutes while retaining fold=0 for
                # an ambiguous fall-back minute, matching the scheduler's
                # once-per-local-minute behavior.
                round_trip = candidate_utc.astimezone(tz)
                if (
                    round_trip.date() != candidate_date
                    or round_trip.hour != hour
                    or round_trip.minute != minute
                ):
                    continue
                return candidate_utc
    return None


def _parse_run_at(run_at: object, *, timezone_name: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(run_at or ""))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_schedule_timezone(timezone_name))
    return value.astimezone(timezone.utc)


def scheduled_job_next_run_at(
    job: Any,
    *,
    now: datetime,
    inclusive: bool,
) -> datetime | None:
    """Project one definition to its next indexed UTC due time."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    # SQLAlchemy column defaults may still be ``None`` before the first flush;
    # only an explicit false value disables the clock.
    if getattr(job, "enabled", True) is False:
        return None
    if bool(getattr(job, "delete_after_run", False)) and getattr(
        job,
        "last_run_at",
        None,
    ) is not None:
        return None

    schedule_kind = str(getattr(job, "schedule_kind", "") or "")
    if schedule_kind == "at":
        run_at = _parse_run_at(
            getattr(job, "run_at", None),
            timezone_name=getattr(job, "timezone", "UTC"),
        )
        if run_at is None:
            return None
        last_run_at = getattr(job, "last_run_at", None)
        if last_run_at is None:
            return run_at
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
        return run_at if run_at > last_run_at.astimezone(timezone.utc) else None

    if schedule_kind in INTERVAL_SCHEDULE_KINDS:
        try:
            every_seconds = float(getattr(job, "every_seconds", 0) or 0)
        except (TypeError, ValueError):
            return None
        if every_seconds <= 0:
            return None
        last_run_at = getattr(job, "last_run_at", None)
        if last_run_at is None:
            return now_utc
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
        return last_run_at.astimezone(timezone.utc) + timedelta(
            seconds=every_seconds
        )

    if schedule_kind == "cron" and getattr(job, "cron_expr", None):
        expression = str(job.cron_expr)
        timezone_name = str(getattr(job, "timezone", None) or "UTC")
        candidate = next_cron_occurrence(
            expression,
            timezone_name=timezone_name,
            after=now_utc,
            inclusive=inclusive,
        )
        last_run_at = getattr(job, "last_run_at", None)
        if candidate is None or last_run_at is None:
            return candidate
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
        last_cron_minute = last_run_at.astimezone(timezone.utc).replace(
            second=0,
            microsecond=0,
        )
        if candidate > last_cron_minute:
            return candidate
        return next_cron_occurrence(
            expression,
            timezone_name=timezone_name,
            after=last_cron_minute,
            inclusive=False,
        )
    return None


def refresh_scheduled_job_next_run_at(
    job: Any,
    *,
    now: datetime | None = None,
    inclusive: bool = True,
) -> datetime | None:
    """Recompute and assign the indexed due clock for one locked Job."""

    next_run_at = scheduled_job_next_run_at(
        job,
        now=now or datetime.now(timezone.utc),
        inclusive=inclusive,
    )
    job.next_run_at = next_run_at
    return next_run_at
