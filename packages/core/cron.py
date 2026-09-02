"""Shared five-field cron grammar for persisted scheduler jobs."""
from __future__ import annotations

from calendar import monthrange
from datetime import date


_CRON_FIELD_RANGES = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),   # day of week; scheduler uses 0=Sunday
)


def _parse_cron_field(
    pattern: str,
    *,
    minimum: int,
    maximum: int,
) -> list[tuple[int, int, int | None, bool]] | None:
    """Parse one field into ``(start, end, step, wildcard)`` parts."""
    parsed: list[tuple[int, int, int | None, bool]] = []
    for raw_part in pattern.strip().split(","):
        part = raw_part.strip()
        if not part:
            return None
        base = part
        step: int | None = None
        if "/" in part:
            base, raw_step = part.split("/", 1)
            try:
                step = int(raw_step)
            except ValueError:
                return None
            if step <= 0:
                return None

        wildcard = base == "*"
        try:
            if wildcard:
                start, end = minimum, maximum
            elif "-" in base:
                start, end = [int(value) for value in base.split("-", 1)]
            else:
                start = end = int(base)
        except ValueError:
            return None

        if start < minimum or end > maximum or start > end:
            return None
        parsed.append((start, end, step, wildcard))
    return parsed


def cron_field_matches(
    field_value: int,
    pattern: str,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    """Return whether one scheduler field matches the current value."""
    parts = _parse_cron_field(pattern, minimum=minimum, maximum=maximum)
    if parts is None:
        return False
    for start, end, step, wildcard in parts:
        if not start <= field_value <= end:
            continue
        if step is None:
            return True
        if wildcard:
            if field_value % step == 0:
                return True
        elif (field_value - start) % step == 0:
            return True
    return False


def _has_calendar_occurrence(parts: list[str]) -> bool:
    """Check the scheduler's AND semantics over one Gregorian cycle."""
    day_pattern, month_pattern, weekday_pattern = parts[2:]
    for year in range(2000, 2400):
        for month in range(1, 13):
            if not cron_field_matches(
                month,
                month_pattern,
                minimum=1,
                maximum=12,
            ):
                continue
            for day in range(1, monthrange(year, month)[1] + 1):
                if not cron_field_matches(
                    day,
                    day_pattern,
                    minimum=1,
                    maximum=31,
                ):
                    continue
                cron_weekday = (date(year, month, day).weekday() + 1) % 7
                if cron_field_matches(
                    cron_weekday,
                    weekday_pattern,
                    minimum=0,
                    maximum=6,
                ):
                    return True
    return False


def validate_cron_expression(expression: object) -> str:
    """Return a normalized cron expression accepted by Manor's scheduler."""
    parts = str(expression or "").strip().lower().split()
    if len(parts) != 5:
        raise ValueError("expected a 5-field cron expression")
    for pattern, (minimum, maximum) in zip(parts, _CRON_FIELD_RANGES):
        if _parse_cron_field(pattern, minimum=minimum, maximum=maximum) is None:
            raise ValueError(f"invalid cron field {pattern!r}")
    if not _has_calendar_occurrence(parts):
        raise ValueError("cron expression has no possible calendar occurrence")
    return " ".join(parts)
