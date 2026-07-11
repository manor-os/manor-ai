from __future__ import annotations

from datetime import datetime, timezone

from packages.core.blueprints.report import (
    GoalPace,
    _build_activity,
    _build_cost,
    _dedupe_goal_paces,
)
from packages.core.models.runtime_learning import RuntimeEvidence


def test_simulation_report_counts_runtime_evidence_when_no_steps_ran() -> None:
    evidence = RuntimeEvidence(
        entity_id="ent_report_runtime",
        workspace_id="ws_report_runtime",
        evidence_type="strategist_review",
        source="strategist",
        status="succeeded",
        summary="Strategist proposed a work wave",
        details={"source_kind": "strategist_proposal"},
        metrics={"credits": 120},
    )

    activity = _build_activity([], [evidence])
    assert activity.total_steps == 1
    assert activity.by_status == {"succeeded": 1}
    assert activity.by_kind == {"strategist_review": 1}
    assert activity.by_action_key == {"strategist_proposal": 1}

    now = datetime.now(timezone.utc)
    cost = _build_cost([], [evidence], now, now)
    assert cost.total_credits == 120
    assert cost.by_kind_credits == {"strategist_review": 120}


def test_simulation_report_dedupes_duplicate_goal_rows_by_title() -> None:
    stale = GoalPace(
        goal_id="goal_stale",
        title="Grow newsletter",
        metric_key="newsletter_growth",
        target_value=None,
        baseline_value=None,
        first_measurement_value=None,
        last_measurement_value=None,
        measurement_count=0,
    )
    measured = GoalPace(
        goal_id="goal_measured",
        title="Grow newsletter",
        metric_key="newsletter_growth_v2",
        target_value=1000,
        baseline_value=100,
        first_measurement_value=100,
        last_measurement_value=220,
        measurement_count=3,
        progress_fraction=0.1333,
    )

    deduped = _dedupe_goal_paces([stale, measured])
    assert len(deduped) == 1
    assert deduped[0].goal_id == "goal_measured"
