"""Expand bounded recurrence windows using the Scheduler's calendar semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from zoneinfo import ZoneInfo

from packages.core.schedule_clock import next_cron_occurrence


def expand_schedule_occurrences(
    *,
    start_at: str,
    end_at: str,
    cron: str,
    timezone: str,
    lead_seconds: float = 0,
    max_occurrences: int = 500,
) -> dict:
    """Return stable UTC slot keys for any finite recurring Workflow queue.

    The interval is [start_at, end_at). DST gaps/folds and day-of-week semantics
    are exactly those used for actual ScheduledJob dispatch.
    """
    start, end = datetime.fromisoformat(start_at), datetime.fromisoformat(end_at)
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("An ordered, timezone-aware start_at/end_at interval is required")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    ZoneInfo(timezone)
    if end - start > timedelta(days=366):
        raise ValueError("A recurrence window cannot exceed 366 days")
    if isinstance(max_occurrences, bool) or not isinstance(max_occurrences, int) or not 1 <= max_occurrences <= 1000:
        raise ValueError("max_occurrences must be an integer from 1 to 1000")
    if isinstance(lead_seconds, bool) or not math.isfinite(lead_seconds) or not 0 <= lead_seconds <= 31 * 86400:
        raise ValueError("lead_seconds must be finite and between zero and 31 days")
    if len(cron.split()) != 5:
        raise ValueError("A five-field cron expression is required")
    items = []
    candidate = next_cron_occurrence(cron, timezone_name=timezone, after=start, inclusive=True)
    if candidate is None:
        raise ValueError("The cron expression has no valid occurrence")
    while candidate < end:
        if candidate >= start:
            if len(items) == max_occurrences:
                raise ValueError("The recurrence exceeds max_occurrences; shorten the window or reduce frequency")
            items.append(
                {
                    "key": candidate.isoformat(),
                    "due_at": candidate.isoformat(),
                    "prepare_at": (candidate - timedelta(seconds=lead_seconds)).isoformat(),
                }
            )
        candidate = next_cron_occurrence(cron, timezone_name=timezone, after=candidate)
        if candidate is None:
            break
    return {
        "occurrences": items,
        "count": len(items),
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "timezone": timezone,
    }
