"""Cadence words become the schedule the user said, or no schedule at all."""
from __future__ import annotations

import pytest

from packages.core.schedule_cadence import (
    automation_cadence,
    automation_timezone,
    looks_like_event_trigger,
    parse_cadence,
)


@pytest.mark.parametrize(
    ("words", "cron_expr"),
    [
        # Daily
        ("每天早上8点", "0 8 * * *"),
        ("daily 08:00", "0 8 * * *"),
        ("每晚9点半", "30 21 * * *"),
        # A part of the day without an hour
        ("每周日晚上", "0 20 * * 0"),
        ("每天下午", "0 14 * * *"),
        ("工作日傍晚", "0 18 * * 1-5"),
        # A weekday, several weekdays, a weekday range
        ("每周一上午9点", "0 9 * * 1"),
        ("every Monday at 8am", "0 8 * * 1"),
        ("周一、三、五 晚上8点", "0 20 * * 1,3,5"),
        ("Mon-Fri 7:15am", "15 7 * * 1-5"),
        ("每周一次", "0 9 * * 1"),
        # Day of month
        ("每月1号", "0 9 1 * *"),
        ("每月1号和15号下午3点", "0 15 1,15 * *"),
        ("monthly on the 15th at 9pm", "0 21 15 * *"),
        # Working days
        ("工作日 9:30", "30 9 * * 1-5"),
        ("weekdays at 8:30am", "30 8 * * 1-5"),
        # A cron expression already in the words
        ("cron 0 9 * * 1", "0 9 * * 1"),
    ],
)
def test_clock_cadences(words, cron_expr):
    cadence = parse_cadence(words)
    assert cadence is not None and (cadence.schedule_kind, cadence.cron_expr) == ("cron", cron_expr)


@pytest.mark.parametrize(
    ("words", "seconds"),
    [("每2小时", 7200), ("每隔30分钟", 1800), ("every 4 hours", 14400), ("hourly", 3600)],
)
def test_interval_cadences(words, seconds):
    cadence = parse_cadence(words)
    assert cadence is not None and (cadence.schedule_kind, cadence.every_seconds) == ("every", seconds)


@pytest.mark.parametrize(
    "words",
    ["on_message_received", "每两周", "every other week", "每月第一个周一", "月底", "08:00", ""],
)
def test_cadences_without_a_cron_form_are_not_guessed(words):
    assert parse_cadence(words) is None


def test_event_triggers_are_recognised():
    assert looks_like_event_trigger("on_message_received")
    assert looks_like_event_trigger("收到新询盘时")
    assert not looks_like_event_trigger("每周一上午9点")


def test_automation_schedule_ignores_description_and_names_its_timezone():
    automation = {
        "trigger": "weekly Monday 9am, Beijing time",
        "description": "Summarise Monday–Sunday activity.",
    }
    assert automation_cadence(automation).cron_expr == "0 9 * * 1"
    assert automation_timezone(automation, default="America/Los_Angeles") == "Asia/Shanghai"
    assert automation_timezone({"trigger": "daily 08:00"}, default="America/Los_Angeles") == "America/Los_Angeles"
    with pytest.raises(ValueError):
        automation_timezone({"timezone": "Mars/Olympus"})
