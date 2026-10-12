from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import json

import pytest

from packages.core.goals.pace import compute_pace
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_operation_service import _sync_goals_from_operation_state
from packages.core.workspaces.sandbox import simulate_goal_value


@pytest.mark.parametrize(
    "status,heartbeat_enabled,deleted,manual_reason,automatic_reason",
    [
        ("active", True, False, None, None),
        ("active", False, False, None, "workspace_runtime_disabled"),
        ("paused", True, False, None, "workspace_paused"),
        ("paused", False, False, None, "workspace_paused"),
        ("needs_setup", True, False, "workspace_needs_setup", "workspace_needs_setup"),
        ("error", True, False, "workspace_error", "workspace_error"),
        ("paused", True, True, "workspace_not_found", "workspace_not_found"),
    ],
)
def test_manual_measurement_remains_available_with_workspace_autonomy_paused(
    status, heartbeat_enabled, deleted, manual_reason, automatic_reason,
):
    from packages.core.goals.measurement import _workspace_measurement_skip_reason

    workspace = SimpleNamespace(
        status=status,
        heartbeat_enabled=heartbeat_enabled,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    assert _workspace_measurement_skip_reason(
        workspace, require_autonomous_runtime=False,
    ) == manual_reason
    assert _workspace_measurement_skip_reason(
        workspace, require_autonomous_runtime=True,
    ) == automatic_reason


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


def test_simulated_goal_measurement_preserves_large_decimal_range():
    goal = Goal(
        id="goal_exact_simulation_01",
        entity_id="ent_goal_direction",
        workspace_id="ws_goal_direction",
        title="Preserve exact simulated progress",
        metric_key="exact_progress",
        baseline_value=Decimal("9999999999999999.9998"),
        current_value=None,
        target_value=Decimal("9999999999999999.9999"),
        deadline=None,
        status="active",
        pace_status="unknown",
    )

    value = simulate_goal_value(goal)

    assert value == Decimal("9999999999999999.9998")
    assert abs(value) < Decimal("10000000000000000")


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


@pytest.mark.asyncio
async def test_update_goal_value_accepts_workspace_goal_key(monkeypatch) -> None:
    from datetime import datetime, timezone

    from sqlalchemy.exc import MultipleResultsFound

    from packages.core.ai.runtime.goal_actions import runtime_update_goal_value_action
    from packages.core.models.base import generate_ulid

    captured: dict = {}
    goal_ulid = generate_ulid()

    class ScalarOneResult:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, query):
            captured["stmt"] = query
            return ScalarOneResult(
                SimpleNamespace(id=goal_ulid, workspace_id="ws-1")
            )

        async def commit(self):
            return None

    async def fake_record_measurement(_db, goal, *, value, **_kwargs):
        return SimpleNamespace(measured_at=datetime.now(timezone.utc), meta=None)

    monkeypatch.setattr("packages.core.database.async_session", lambda: FakeSession())
    monkeypatch.setattr(
        "packages.core.goals.service.record_measurement",
        fake_record_measurement,
    )

    payload = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={
                "workspace_id": "ws-1",
                "goal_id": "royalty_registration_ready",
                "value": 66,
            },
        )
    )
    assert payload["goal_id"] == goal_ulid
    where_sql = str(
        captured["stmt"].whereclause.compile(compile_kwargs={"literal_binds": False})
    ).lower()
    assert "goal_key" in where_sql

    captured.clear()
    id_only = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={"goal_id": "royalty_registration_ready", "value": 66},
        )
    )
    assert id_only["goal_id"] == goal_ulid
    where_sql = str(
        captured["stmt"].whereclause.compile(compile_kwargs={"literal_binds": False})
    ).lower()
    assert "goal_key" not in where_sql

    class AmbiguousSession(FakeSession):
        async def execute(self, query):
            captured["stmt"] = query

            class AmbiguousResult:
                def scalar_one_or_none(self):
                    raise MultipleResultsFound()

            return AmbiguousResult()

    monkeypatch.setattr("packages.core.database.async_session", lambda: AmbiguousSession())
    missing = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={
                "workspace_id": "ws-1",
                "goal_id": "royalty_registration_ready",
                "value": 66,
            },
        )
    )
    assert missing == {"error": "goal not found"}


@pytest.mark.asyncio
async def test_update_goal_value_accepts_goal_key_param_without_goal_id(monkeypatch) -> None:
    from datetime import datetime, timezone

    from packages.core.ai.runtime.goal_actions import runtime_update_goal_value_action
    from packages.core.models.base import generate_ulid

    captured: dict = {}
    goal_ulid = generate_ulid()

    class ScalarOneResult:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, query):
            captured["stmt"] = query
            return ScalarOneResult(
                SimpleNamespace(id=goal_ulid, workspace_id="ws-1")
            )

        async def commit(self):
            return None

    async def fake_record_measurement(_db, goal, *, value, **_kwargs):
        captured["value"] = value
        return SimpleNamespace(measured_at=datetime.now(timezone.utc), meta=None)

    monkeypatch.setattr("packages.core.database.async_session", lambda: FakeSession())
    monkeypatch.setattr(
        "packages.core.goals.service.record_measurement",
        fake_record_measurement,
    )

    payload = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={
                "workspace_id": "ws-1",
                "goal_key": "royalty_registration_ready",
                "value": 66,
            },
        )
    )
    assert payload["goal_id"] == goal_ulid
    where_sql = str(
        captured["stmt"].whereclause.compile(compile_kwargs={"literal_binds": False})
    ).lower()
    assert "goal_key" in where_sql


@pytest.mark.asyncio
async def test_update_goal_value_reports_missing_fields() -> None:
    from packages.core.ai.runtime.goal_actions import runtime_update_goal_value_action

    payload = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={"workspace_id": "ws-1", "note": "scoring"},
        )
    )
    assert payload["error"].startswith("goal_id (or goal_key) and value are required")
    assert payload["received_keys"] == []
    assert "example" in payload


def _fake_goal_session(monkeypatch, *, goal, captured):
    from datetime import datetime, timezone

    from packages.core.models.base import generate_ulid

    goal_ulid = getattr(goal, "id", None) or generate_ulid()
    if getattr(goal, "id", None) is None:
        goal.id = goal_ulid
    if getattr(goal, "workspace_id", None) is None:
        goal.workspace_id = "ws-1"

    class ScalarOneResult:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, query):
            captured["stmt"] = query
            return ScalarOneResult(goal)

        async def commit(self):
            captured["committed"] = True

    async def fake_record_measurement(_db, recorded_goal, *, value, **_kwargs):
        captured["value"] = value
        captured["goal_id"] = recorded_goal.id
        return SimpleNamespace(measured_at=datetime.now(timezone.utc), meta=None)

    monkeypatch.setattr("packages.core.database.async_session", lambda: FakeSession())
    monkeypatch.setattr(
        "packages.core.goals.service.record_measurement",
        fake_record_measurement,
    )
    return goal_ulid


@pytest.mark.asyncio
async def test_update_goal_value_rejects_fraction_on_100_scale(monkeypatch) -> None:
    from packages.core.ai.runtime.goal_actions import runtime_update_goal_value_action

    captured: dict = {}
    _fake_goal_session(
        monkeypatch,
        goal=SimpleNamespace(target_value=Decimal("100")),
        captured=captured,
    )

    payload = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={
                "workspace_id": "ws-1",
                "goal_key": "rights_clearance_ready",
                "value": 0.84,
            },
        )
    )
    assert payload["error"].startswith("value is on the wrong scale")
    assert payload["target_value"] == 100.0
    assert payload["received_value"] == 0.84
    assert "value" not in captured
    assert "committed" not in captured


@pytest.mark.asyncio
async def test_update_goal_value_keeps_zero_and_whole_numbers_on_100_scale(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.goal_actions import runtime_update_goal_value_action

    captured: dict = {}
    goal_ulid = _fake_goal_session(
        monkeypatch,
        goal=SimpleNamespace(target_value=Decimal("100")),
        captured=captured,
    )

    for value in (0, 1, 84):
        captured.clear()
        payload = json.loads(
            await runtime_update_goal_value_action(
                entity_id="ent-1",
                params={
                    "workspace_id": "ws-1",
                    "goal_id": goal_ulid,
                    "value": value,
                },
            )
        )
        assert payload["goal_id"] == goal_ulid
        assert payload["value"] == float(value)
        assert captured["value"] == Decimal(str(value))


@pytest.mark.asyncio
async def test_update_goal_value_allows_fraction_when_target_is_one(monkeypatch) -> None:
    from packages.core.ai.runtime.goal_actions import runtime_update_goal_value_action

    captured: dict = {}
    goal_ulid = _fake_goal_session(
        monkeypatch,
        goal=SimpleNamespace(target_value=Decimal("1")),
        captured=captured,
    )

    payload = json.loads(
        await runtime_update_goal_value_action(
            entity_id="ent-1",
            params={"goal_id": goal_ulid, "value": 0.84},
        )
    )
    assert payload["goal_id"] == goal_ulid
    assert payload["value"] == 0.84
    assert captured["value"] == Decimal("0.84")
