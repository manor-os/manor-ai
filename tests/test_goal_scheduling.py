from __future__ import annotations

from packages.core.goals.scheduling import _cadence_to_schedule


def test_goal_calendar_cadences_include_quarterly_and_yearly():
    assert _cadence_to_schedule("monthly") == ("cron", {"cron_expr": "0 9 1 * *"})
    assert _cadence_to_schedule("quarterly") == ("cron", {"cron_expr": "0 9 1 */3 *"})
    assert _cadence_to_schedule("yearly") == ("cron", {"cron_expr": "0 9 1 1 *"})
