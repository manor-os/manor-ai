from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from packages.core.blueprints.report import (
    GoalPace,
    _build_activity,
    _build_cost,
    _dedupe_goal_paces,
)
from packages.core.constants.execution import (
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
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


def test_simulation_report_cost_excludes_coordinator_child_rollup() -> None:
    child_usd = 0.10
    parent_own_usd = 0.15
    rolled_up_usd = child_usd + parent_own_usd
    now = datetime.now(timezone.utc)
    parent = SimpleNamespace(
        plan_id="plan",
        step_key="coordinate",
        finished_at=None,
        params={COORDINATOR_PARAM_KEY: True},
        cost={},
        kind="subagent",
    )
    child = SimpleNamespace(
        plan_id="plan",
        step_key="coordinate__child",
        finished_at=now,
        params={
            COORDINATOR_CHILD_PARAM_KEY: True,
            COORDINATOR_PARENT_STEP_PARAM_KEY: parent.step_key,
        },
        cost={"usd": child_usd},
        kind="subagent",
    )
    steps = [
        parent,
        child,
    ]

    while_parent_running = _build_cost(steps, [], now, now)

    assert while_parent_running.total_usd == child_usd

    parent.finished_at = now
    parent.cost = {"usd": rolled_up_usd}
    after_parent_rollup = _build_cost(steps, [], now, now)

    assert after_parent_rollup.total_usd == rolled_up_usd
    assert (
        sum(after_parent_rollup.by_kind_credits.values())
        == after_parent_rollup.total_credits
    )


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
