from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from packages.core.tasks.monitor_tasks import (
    _hitl_last_reminded_at,
    _hitl_reminder_due,
    _record_hitl_reminder,
)


def test_hitl_reminder_due_after_threshold_without_prior_reminder():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert _hitl_reminder_due(
        wait_started_at=now - timedelta(minutes=61),
        last_reminded_at=None,
        now=now,
        after_minutes=60,
        cooldown_minutes=240,
    )


def test_hitl_reminder_not_due_inside_wait_threshold_or_cooldown():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert not _hitl_reminder_due(
        wait_started_at=now - timedelta(minutes=30),
        last_reminded_at=None,
        now=now,
        after_minutes=60,
        cooldown_minutes=240,
    )
    assert not _hitl_reminder_due(
        wait_started_at=now - timedelta(hours=6),
        last_reminded_at=now - timedelta(minutes=30),
        now=now,
        after_minutes=60,
        cooldown_minutes=240,
    )


def test_record_hitl_reminder_updates_plan_dispatcher_state():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    plan = SimpleNamespace(dispatcher_state={})

    _record_hitl_reminder(plan, "step_123", now=now, wait_minutes=90)

    reminder = plan.dispatcher_state["hitl_reminders"]["step_123"]
    assert reminder["last_reminded_at"] == now.isoformat()
    assert reminder["wait_minutes"] == 90
    assert _hitl_last_reminded_at(plan, "step_123") == now
