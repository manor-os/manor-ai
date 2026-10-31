import pytest

from packages.core.services.schedule_occurrences import expand_schedule_occurrences

pytestmark = pytest.mark.oss_regression


def test_recurring_content_and_maintenance_use_the_same_scheduler_calendar():
    weekly = expand_schedule_occurrences(
        start_at="2026-10-01T00:00:00Z",
        end_at="2026-11-01T00:00:00Z",
        cron="0 9 * * 1",
        timezone="Asia/Shanghai",
        lead_seconds=86400,
    )
    assert weekly["count"] == 4
    assert weekly["occurrences"][0] == {
        "key": "2026-10-05T01:00:00+00:00",
        "due_at": "2026-10-05T01:00:00+00:00",
        "prepare_at": "2026-10-04T01:00:00+00:00",
    }
    maintenance = expand_schedule_occurrences(
        start_at="2026-10-01T00:00:00Z", end_at="2027-01-01T00:00:00Z", cron="0 12 15 * *", timezone="UTC"
    )
    assert [item["due_at"] for item in maintenance["occurrences"]] == [
        f"2026-{month}-15T12:00:00+00:00" for month in ("10", "11", "12")
    ]


def test_recurrence_uses_finite_half_open_windows_limits_and_dst_gap_semantics():
    result = expand_schedule_occurrences(
        start_at="2026-03-07T00:00:00-05:00",
        end_at="2026-03-10T00:00:00-04:00",
        cron="30 2 * * *",
        timezone="America/New_York",
    )
    assert result["count"] == 2  # March 8 02:30 does not exist.
    with pytest.raises(ValueError, match="max_occurrences"):
        expand_schedule_occurrences(
            start_at="2026-10-01T00:00:00Z",
            end_at="2026-10-02T00:00:00Z",
            cron="* * * * *",
            timezone="UTC",
            max_occurrences=5,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        expand_schedule_occurrences(start_at="2026-10-01", end_at="2026-10-02", cron="0 9 * * *", timezone="UTC")
