"""Unit tests for scheduler tick logic — no DB required."""

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

try:
    from packages.core.tasks.scheduler_tasks import (
        _agent_task_max_turns_for_target,
        _cron_matches,
        _is_due,
        _tighten_file_deliverable_completion,
    )
except ImportError:
    pytest.skip("Celery not installed — skipping scheduler tests", allow_module_level=True)


def _make_job(**kwargs):
    """Create a mock job object with sensible defaults."""
    defaults = {
        "job_id": "test-job-1",
        "schedule_kind": None,
        "cron_expr": None,
        "every_seconds": None,
        "run_at": None,
        "timezone": "UTC",
        "last_run_at": None,
        "last_status": None,
        "enabled": True,
        "delete_after_run": False,
        "consecutive_errors": 0,
        "agent_id": None,
        "goal_id": None,
        "manor_task_id": None,
        "execution_type": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── _cron_matches tests ──


def test_cron_matches_wildcard():
    """'* * * * *' should always match (when no previous run this minute)."""
    now = datetime(2026, 4, 21, 10, 30, 0, tzinfo=timezone.utc)
    assert _cron_matches("* * * * *", now, None) is True


def test_cron_matches_specific():
    """'30 9 * * *' matches at 09:30 but not at 10:30."""
    at_0930 = datetime(2026, 4, 21, 9, 30, 0, tzinfo=timezone.utc)
    at_1030 = datetime(2026, 4, 21, 10, 30, 0, tzinfo=timezone.utc)

    assert _cron_matches("30 9 * * *", at_0930, None) is True
    assert _cron_matches("30 9 * * *", at_1030, None) is False


def test_cron_every_n():
    """'*/5 * * * *' matches minutes 0, 5, 10, ... but not 3, 7, etc."""
    for minute in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55):
        now = datetime(2026, 4, 21, 12, minute, 0, tzinfo=timezone.utc)
        assert _cron_matches("*/5 * * * *", now, None) is True, f"Should match minute {minute}"

    for minute in (1, 3, 7, 13, 22, 59):
        now = datetime(2026, 4, 21, 12, minute, 0, tzinfo=timezone.utc)
        assert _cron_matches("*/5 * * * *", now, None) is False, f"Should NOT match minute {minute}"


def test_cron_matches_lists_and_ranges():
    """Weekly schedules from the UI use comma lists and weekday ranges."""
    monday = datetime(2026, 4, 20, 9, 0, 0, tzinfo=timezone.utc)
    wednesday = datetime(2026, 4, 22, 9, 0, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 4, 25, 9, 0, 0, tzinfo=timezone.utc)

    assert _cron_matches("0 9 * * 1,3,5", monday, None) is True
    assert _cron_matches("0 9 * * 1,3,5", wednesday, None) is True
    assert _cron_matches("0 9 * * 1-5", wednesday, None) is True
    assert _cron_matches("0 9 * * 1-5", saturday, None) is False


# ── _is_due tests ──


def test_is_due_interval():
    """every_seconds=300: due when elapsed >= 300, not due when < 300."""
    now = datetime(2026, 4, 21, 12, 10, 0, tzinfo=timezone.utc)

    # Never run before — should be due immediately
    job_never_run = _make_job(schedule_kind="every", every_seconds=300)
    assert _is_due(job_never_run, now) is True

    # Last run 5 minutes ago — exactly due
    job_due = _make_job(
        schedule_kind="every",
        every_seconds=300,
        last_run_at=now - timedelta(seconds=300),
    )
    assert _is_due(job_due, now) is True

    # Last run 2 minutes ago — not yet due
    job_not_due = _make_job(
        schedule_kind="every",
        every_seconds=300,
        last_run_at=now - timedelta(seconds=120),
    )
    assert _is_due(job_not_due, now) is False


def test_is_due_accepts_legacy_interval_alias():
    """Older workspace cadence installers stored fixed intervals as 'interval'."""
    now = datetime(2026, 4, 21, 12, 10, 0, tzinfo=timezone.utc)
    job = _make_job(schedule_kind="interval", every_seconds=300)

    assert _is_due(job, now) is True


def test_is_due_cron_uses_job_timezone_los_angeles_dst():
    """'0 9 * * *' in LA should fire at 16:00 UTC during PDT, not 09:00 UTC."""
    job = _make_job(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Los_Angeles",
    )

    assert _is_due(job, datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)) is False
    assert _is_due(job, datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)) is True


def test_is_due_cron_uses_job_timezone_los_angeles_standard_time():
    """The same LA 9am cron should shift to 17:00 UTC outside DST."""
    job = _make_job(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Los_Angeles",
    )

    assert _is_due(job, datetime(2026, 1, 5, 16, 59, tzinfo=timezone.utc)) is False
    assert _is_due(job, datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc)) is True


def test_is_due_cron_does_not_repeat_same_local_minute():
    job = _make_job(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Los_Angeles",
        last_run_at=datetime(2026, 5, 1, 16, 0, 5, tzinfo=timezone.utc),
    )

    assert _is_due(job, datetime(2026, 5, 1, 16, 0, 30, tzinfo=timezone.utc)) is False


def test_is_due_one_shot_naive_run_at_uses_job_timezone():
    job = _make_job(
        schedule_kind="at",
        run_at="2026-05-01T09:00",
        timezone="America/Los_Angeles",
    )

    assert _is_due(job, datetime(2026, 5, 1, 15, 59, tzinfo=timezone.utc)) is False
    assert _is_due(job, datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)) is True


def test_agent_task_max_turns_stays_default_for_summary_jobs():
    turns = _agent_task_max_turns_for_target({})

    assert turns == 50


def test_agent_task_max_turns_expands_for_video_deliverables():
    turns = _agent_task_max_turns_for_target({"output_kind": "video"})

    assert turns == 50


def test_agent_task_max_turns_does_not_scan_prompt_text():
    turns = _agent_task_max_turns_for_target({"prompt": "Generate a product video."})

    assert turns == 50


def test_scheduled_execution_target_uses_explicit_fields_only():
    from packages.core.ai.runtime.scheduling import _coerce_scheduled_execution_target

    text_only = _coerce_scheduled_execution_target(
        {"prompt": "Generate a product video.", "notes": "Prompt text is not a contract."},
    )
    structured = _coerce_scheduled_execution_target(
        output_kind="video",
        requires_generated_file=True,
        max_turns="18",
    )

    assert text_only == {
        "prompt": "Generate a product video.",
        "notes": "Prompt text is not a contract.",
    }
    assert structured == {
        "output_kind": "video",
        "requires_generated_file": True,
        "max_turns": 18,
    }


def test_agent_task_max_turns_respects_explicit_budget():
    turns = _agent_task_max_turns_for_target({"max_turns": 20, "output_kind": "video"})

    assert turns == 20


def test_file_deliverable_completion_requires_generation_tool_result():
    done_when, deliverable = _tighten_file_deliverable_completion(
        target={"deliverable": {"kind": "video"}},
        done_when="The requested automation has completed.",
        deliverable="A video recap.",
    )

    assert "generate_file" in done_when
    assert "text-only report" in done_when
    assert "terminal failure reason" in done_when
    assert "generated video" in deliverable
