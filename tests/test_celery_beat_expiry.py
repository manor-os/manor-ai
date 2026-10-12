"""Short-interval beat ticks must expire before their successor is due.

Without ``expires`` a worker restart replays every tick that queued while it
was down, and the live sandbox-scheduler tick waits behind that backlog.
"""

from __future__ import annotations

from celery.schedules import crontab

from packages.core.celery_app import (
    BEAT_EXPIRE_MAX_INTERVAL_SECONDS,
    _apply_beat_expiry,
    celery_app,
)


def test_every_short_interval_entry_expires_by_its_next_tick():
    schedule = celery_app.conf.beat_schedule
    short = {
        name: entry
        for name, entry in schedule.items()
        if isinstance(entry.get("schedule"), (int, float)) and entry["schedule"] <= BEAT_EXPIRE_MAX_INTERVAL_SECONDS
    }
    from packages.core.queues import TASK_QUEUES, UNDECLARED_TASK_QUEUE, CeleryQueue

    assert "runtime-sandbox-scheduler" in short
    assert "runtime-outbox-dispatch" in short
    for name, entry in short.items():
        expires = (entry.get("options") or {}).get("expires")
        queue = TASK_QUEUES.get(entry["task"], UNDECLARED_TASK_QUEUE)
        if queue in (CeleryQueue.CONTROL, CeleryQueue.OPS):
            # Control-plane and monitoring ticks are superseded by their successor.
            assert expires is not None and 0 < expires <= entry["schedule"], f"{name}: {expires}"
        else:
            # A heavy/recovery tick may wait minutes behind long work; an
            # interval-length expiry would drop it exactly when it is needed.
            assert expires is None or expires > entry["schedule"], f"{name}: expires too early"


def test_long_interval_and_crontab_entries_are_left_alone():
    schedule = {
        "hourly": {"task": "scheduler.tick", "schedule": 3600.0},
        "daily": {"task": "scheduler.tick", "schedule": crontab(hour=8, minute=0)},
        "explicit": {"task": "scheduler.tick", "schedule": 2.0, "options": {"expires": 1.0}},
    }
    _apply_beat_expiry(schedule)
    assert "options" not in schedule["hourly"]
    assert "options" not in schedule["daily"]
    assert schedule["explicit"]["options"]["expires"] == 1.0


def test_timedelta_intervals_are_treated_like_numbers():
    from datetime import timedelta

    schedule = {"ten": {"task": "runtime.sandbox_scheduler", "schedule": timedelta(seconds=10)}}
    _apply_beat_expiry(schedule)
    assert schedule["ten"]["options"]["expires"] == 10.0


def test_only_control_and_ops_queue_tasks_get_an_automatic_expiry():
    schedule = {
        "control": {"task": "runtime.sandbox_scheduler", "schedule": 2.0},
        "ops": {"task": "ops.collect_snapshot", "schedule": 30.0},
        "heavy": {"task": "media.recover_stale_jobs", "schedule": 60.0},
        "unknown": {"task": "not.registered", "schedule": 5.0},
    }
    _apply_beat_expiry(schedule)
    assert schedule["control"]["options"]["expires"] == 2.0
    assert schedule["ops"]["options"]["expires"] == 30.0
    assert "options" not in schedule["heavy"]
    assert "options" not in schedule["unknown"], "undeclared tasks route to heavy and must not expire"


def test_no_real_entry_hides_behind_a_timedelta_schedule():
    from datetime import timedelta

    from packages.core.celery_app import beat_interval_seconds

    for name, entry in celery_app.conf.beat_schedule.items():
        value = entry.get("schedule")
        assert not isinstance(value, timedelta), f"{name}: use a number so the expiry guard covers it"
        seconds = beat_interval_seconds(value)
        if seconds is not None and seconds <= BEAT_EXPIRE_MAX_INTERVAL_SECONDS:
            expires = (entry.get("options") or {}).get("expires")
            assert expires is None or expires > 0


def test_ops_entries_expire_like_any_control_tick():
    """The ops queue keeps up (bounded walks, single-flight snapshot), so a
    stale monitoring tick is simply superseded by its successor."""
    schedule = celery_app.conf.beat_schedule
    for name in ("ops-collect-snapshot", "ops-log-scan"):
        assert schedule[name]["options"]["expires"] <= schedule[name]["schedule"]
