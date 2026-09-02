from __future__ import annotations

import pytest

from packages.core.goals.scheduling import (
    _cadence_to_schedule,
    validate_measurement_cadence,
)


def test_goal_calendar_cadences_include_quarterly_and_yearly():
    assert _cadence_to_schedule("monthly") == ("cron", {"cron_expr": "0 9 1 * *"})
    assert _cadence_to_schedule("quarterly") == ("cron", {"cron_expr": "0 9 1 */3 *"})
    assert _cadence_to_schedule("yearly") == ("cron", {"cron_expr": "0 9 1 1 *"})


def test_goal_cadence_rejects_unknown_names_and_malformed_cron():
    with pytest.raises(ValueError, match="unsupported measurement_cadence"):
        _cadence_to_schedule("fortnightly")
    with pytest.raises(ValueError, match="unsupported measurement_cadence"):
        _cadence_to_schedule("not a cron")
    with pytest.raises(ValueError, match="unsupported measurement_cadence"):
        _cadence_to_schedule("x x x x x")
    with pytest.raises(ValueError, match="unsupported measurement_cadence"):
        _cadence_to_schedule("0 0 0 * * *")
    with pytest.raises(ValueError, match="unsupported measurement_cadence"):
        _cadence_to_schedule("0 0 31 2 *")

    assert _cadence_to_schedule("0 8 * * *") == (
        "cron",
        {"cron_expr": "0 8 * * *"},
    )
    assert _cadence_to_schedule("0 0 29 2 *") == (
        "cron",
        {"cron_expr": "0 0 29 2 *"},
    )


def test_goal_manual_cadence_respects_database_column_length():
    assert validate_measurement_cadence("manual_demo") == "manual_demo"
    with pytest.raises(ValueError, match="at most 64 characters"):
        validate_measurement_cadence("manual_" + "x" * 64)
