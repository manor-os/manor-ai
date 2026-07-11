from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.goals.pace import compute_pace
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_operation_service import _sync_goals_from_operation_state
from packages.core.workspaces.sandbox import simulate_goal_value


def test_compute_pace_supports_lower_is_better_goals():
    created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    today = date(2026, 5, 11)
    deadline = date(2026, 5, 21)

    assert (
        compute_pace(
            current_value=Decimal("4"),
            baseline_value=Decimal("6"),
            target_value=Decimal("2"),
            created_at=created_at,
            deadline=deadline,
            today=today,
        )
        == "on_track"
    )

    assert (
        compute_pace(
            current_value=Decimal("1.9"),
            baseline_value=Decimal("6"),
            target_value=Decimal("2"),
            created_at=created_at,
            deadline=deadline,
            today=today,
        )
        == "achieved"
    )


def test_compute_pace_keeps_higher_is_better_goals_working():
    created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)

    assert (
        compute_pace(
            current_value=Decimal("150"),
            baseline_value=Decimal("100"),
            target_value=Decimal("200"),
            created_at=created_at,
            deadline=date(2026, 5, 21),
            today=date(2026, 5, 11),
        )
        == "on_track"
    )

    assert (
        compute_pace(
            current_value=Decimal("200"),
            baseline_value=Decimal("100"),
            target_value=Decimal("200"),
            created_at=created_at,
            deadline=date(2026, 5, 21),
            today=date(2026, 5, 11),
        )
        == "achieved"
    )


def test_compute_pace_keeps_brand_new_positive_movement_visible():
    created_at = datetime(2026, 5, 17, tzinfo=timezone.utc)

    assert (
        compute_pace(
            current_value=Decimal("3.3"),
            baseline_value=Decimal("6"),
            target_value=Decimal("2"),
            created_at=created_at,
            deadline=date(2026, 8, 15),
            today=date(2026, 5, 17),
        )
        == "ahead"
    )

    assert (
        compute_pace(
            current_value=Decimal("28"),
            baseline_value=Decimal("22"),
            target_value=Decimal("40"),
            created_at=created_at,
            deadline=date(2026, 8, 15),
            today=date(2026, 5, 17),
        )
        == "ahead"
    )


def test_sandbox_lower_is_better_measurement_moves_toward_target_without_auto_achieving():
    created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    goal = Goal(
        id="goal_lower_is_better_01",
        entity_id="ent_goal_direction",
        workspace_id="ws_goal_direction",
        title="Reduce stale lead rate below 10%",
        metric_key="stale_lead_rate_pct",
        baseline_value=Decimal("31"),
        current_value=Decimal("18"),
        target_value=Decimal("10"),
        created_at=created_at,
        deadline=(created_at + timedelta(days=90)).date(),
        status="active",
        pace_status="at_risk",
    )

    value = simulate_goal_value(goal, today=created_at.date())

    assert Decimal("10") < value < Decimal("18")


def test_sandbox_higher_is_better_measurement_does_not_regress_to_baseline():
    created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    goal = Goal(
        id="goal_higher_is_better_01",
        entity_id="ent_goal_direction",
        workspace_id="ws_goal_direction",
        title="Lift lead-to-tour conversion to 40%",
        metric_key="lead_to_tour_conversion_pct",
        baseline_value=Decimal("22"),
        current_value=Decimal("28"),
        target_value=Decimal("40"),
        created_at=created_at,
        deadline=(created_at + timedelta(days=90)).date(),
        status="active",
        pace_status="behind",
    )

    value = simulate_goal_value(goal, today=created_at.date())

    assert Decimal("28") < value < Decimal("40")


@pytest.mark.asyncio
async def test_operation_goal_sync_preserves_runtime_measurement_fields(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    deadline = date(2026, 8, 15)
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Measurement Sync",
        operating_model={},
        settings={"sandbox": True},
        status="active",
    )
    goal = Goal(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Reply within 2 hours",
        description="Keep qualified lead response time low.",
        metric_key="avg_draft_response_time_hours",
        baseline_value=Decimal("6"),
        current_value=Decimal("3.4"),
        target_value=Decimal("2"),
        deadline=deadline,
        measurement_source={
            "provider": "sandbox_leasing",
            "action": "leasing.get_response_time",
            "params": {"workspace_id": workspace_id, "owner_service_key": "lead_intake"},
            "_sandbox": True,
        },
        measurement_cadence="daily",
        priority=5,
        status="active",
        pace_status="behind",
    )
    db_session.add_all([workspace, goal])
    await db_session.flush()

    await _sync_goals_from_operation_state(
        db_session,
        workspace,
        [
            {
                "goal_key": "lead_response_time",
                "title": "Reply to qualified leasing leads within 2 hours",
                "metric_key": "avg_draft_response_time_hours",
                "target_value": 2,
                "baseline_value": 6,
                "measurement_source": {
                    "provider": "sandbox_leasing",
                    "action": "leasing.get_response_time",
                },
                "cadence": "daily",
            }
        ],
        deactivate_missing=False,
    )
    await db_session.refresh(goal)

    assert goal.deadline == deadline
    assert goal.description == "Keep qualified lead response time low."
    assert goal.priority == 5
    assert goal.status == "active"
    assert goal.measurement_source == {
        "provider": "sandbox_leasing",
        "action": "leasing.get_response_time",
        "params": {"workspace_id": workspace_id, "owner_service_key": "lead_intake"},
        "_sandbox": True,
    }
