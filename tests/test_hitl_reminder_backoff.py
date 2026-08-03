"""Reminding harder does not make anyone answer.

One governance approval on staging had been waiting ten days. The reminder
job worked perfectly the whole time: a flat 4-hour cooldown with no ceiling,
so roughly sixty notices went out. Every one landed in an inbox where 120 of
120 HITL reminders — and ~500 of ~500 notifications overall — were unread.

Backing off is the fix, not a louder version of the same delivery: fast while
someone might plausibly still be looking (hourly for the first hours), slower
as it becomes clear nobody is, and eventually silent. The request is not
forgotten when reminders stop — the step still waits and the task still shows
it — we just stop repeating ourselves into the void.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.tasks.monitor_tasks import (
    HITL_REMINDER_GIVE_UP_MINUTES,
    _hitl_reminder_due,
    hitl_reminder_interval_minutes,
)

HOUR = 60
DAY = 60 * 24


@pytest.mark.parametrize(
    "waited_minutes,expected",
    [
        (0, HOUR),          # just started waiting → hourly
        (30, HOUR),
        (HOUR * 3, HOUR),
        (HOUR * 4, HOUR * 4),   # after 4h → every 4h
        (HOUR * 12, HOUR * 4),
        (DAY, DAY),             # after a day → daily
        (DAY * 2, DAY),
    ],
)
def test_the_gap_grows_with_the_wait(waited_minutes, expected):
    assert hitl_reminder_interval_minutes(waited_minutes) == expected


def test_reminders_stop_rather_than_repeat_forever():
    assert hitl_reminder_interval_minutes(HITL_REMINDER_GIVE_UP_MINUTES) is None
    assert hitl_reminder_interval_minutes(DAY * 10) is None


def test_early_reminders_are_more_prompt_than_before():
    """The old flat cooldown was 4 hours from the very first reminder — the
    window where someone plausibly IS still at their desk got the slowest
    cadence."""
    assert hitl_reminder_interval_minutes(HOUR) < 240


def test_ten_day_wait_sends_a_fraction_of_the_notices():
    def notices(total_minutes: int, interval_of) -> int:
        elapsed, count = HOUR, 0     # first reminder one hour into the wait
        while elapsed < total_minutes:
            count += 1
            gap = interval_of(elapsed)
            if gap is None:
                break
            elapsed += gap
        return count

    flat = notices(DAY * 10, lambda _m: 240)
    backed_off = notices(DAY * 10, hitl_reminder_interval_minutes)

    assert flat > 50
    assert backed_off < 15
    assert backed_off < flat / 4


# ── the due check honours the schedule ────────────────────────────────


def _due(*, waited_minutes: int, since_last_reminder: int | None) -> bool:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    return _hitl_reminder_due(
        wait_started_at=now - timedelta(minutes=waited_minutes),
        last_reminded_at=(
            None if since_last_reminder is None
            else now - timedelta(minutes=since_last_reminder)
        ),
        now=now,
    )


def test_nothing_is_sent_before_the_initial_delay():
    assert not _due(waited_minutes=30, since_last_reminder=None)


def test_first_reminder_goes_out_once_the_wait_is_real():
    assert _due(waited_minutes=HOUR + 5, since_last_reminder=None)


def test_a_recent_reminder_suppresses_the_next_one():
    assert not _due(waited_minutes=HOUR * 2, since_last_reminder=10)
    assert _due(waited_minutes=HOUR * 2, since_last_reminder=HOUR + 5)


def test_a_day_long_wait_is_reminded_daily_not_four_hourly():
    assert not _due(waited_minutes=DAY + HOUR, since_last_reminder=HOUR * 5)
    assert _due(waited_minutes=DAY + HOUR, since_last_reminder=DAY + 1)


def test_an_abandoned_request_is_never_reminded_again():
    assert not _due(waited_minutes=DAY * 10, since_last_reminder=DAY * 5)


def test_an_explicit_cooldown_still_overrides_the_schedule():
    """Callers (and tests) can still pin a cadence."""
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    assert _hitl_reminder_due(
        wait_started_at=now - timedelta(days=10),
        last_reminded_at=now - timedelta(hours=5),
        now=now,
        cooldown_minutes=240,
    )
