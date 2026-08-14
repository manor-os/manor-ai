from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from packages.core.goals.measurers import twitter_x as twitter_x_measurer
from packages.core.goals.service import create_goal
from packages.core.blueprints.exporter import _export_goals, _export_stats
from packages.core.blueprints.installer import InstallMode, _install_goal, _install_stat
from packages.core.models.base import generate_ulid
from packages.core.models.document import (
    Document,
    DocumentGroup,
    DocumentGroupMember,
    Integration,
    VectorStatus,
)
from packages.core.models.goal import GoalMeasurement
from packages.core.models.scheduler import AgentExecution, ScheduledJob
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.stats.integration_keys import (
    StatIntegrationKey,
    get_stat_integration_spec,
    stat_integration_key_for_provider,
)
from packages.core.stats.library import get_library_entry, list_library_entries
from packages.core.stats.service import (
    _window_bounds,
    collect_stat,
    create_stat,
    create_stat_from_library,
    list_observations,
    record_observation,
)


INTERNAL_LIBRARY_EXPECTATIONS = {
    "workspace.tasks.created": 6,
    "workspace.tasks.completed": 3,
    "workspace.tasks.completion_rate": 100 / 3,
    "workspace.tasks.on_time_rate": 200 / 3,
    "workspace.tasks.overdue": 1,
    "workspace.tasks.blocked": 2,
    "workspace.tasks.avg_cycle_time_hours": 4,
    "workspace.workflows.runs": 4,
    "workspace.workflows.success_rate": 100 / 3,
    "workspace.agents.runs": 4,
    "workspace.agents.success_rate": 100 / 3,
    "workspace.knowledge.ready_documents": 2,
}

X_LIBRARY_EXPECTATIONS = {
    "twitter_x.followers_count": 1250,
    "twitter_x.following_count": 84,
    "twitter_x.tweet_count": 321,
    "twitter_x.listed_count": 19,
}


def _workspace(db_session) -> Workspace:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Stats test workspace",
        status="active",
    )
    db_session.add(workspace)
    return workspace


def test_first_party_stats_library_has_internal_and_integration_collectors() -> None:
    entries = list_library_entries()
    keys = {entry.key for entry in entries}

    assert keys == set(INTERNAL_LIBRARY_EXPECTATIONS) | set(X_LIBRARY_EXPECTATIONS)
    assert get_library_entry("workspace.tasks.completed").goal_eligible is True
    assert {entry.collector_type for entry in entries} >= {"workspace_internal", "integration"}
    integration_entries = [
        entry for entry in entries if entry.collector_type == "integration"
    ]
    assert {
        entry.integration_key for entry in integration_entries
    } == {StatIntegrationKey.TWITTER_X}
    assert all("provider" not in entry.collector_config for entry in integration_entries)
    assert len({key.value for key in StatIntegrationKey}) == len(StatIntegrationKey)
    assert get_stat_integration_spec(StatIntegrationKey.TWITTER_X).provider_key == "twitter_x"
    assert stat_integration_key_for_provider("x") == StatIntegrationKey.TWITTER_X
    assert stat_integration_key_for_provider("twitter") == StatIntegrationKey.TWITTER_X


def test_stat_window_boundaries_use_complete_utc_windows() -> None:
    observed_at = datetime(2026, 8, 12, 12, 34, 56, tzinfo=timezone.utc)

    assert _window_bounds("latest", observed_at) == (None, observed_at)
    assert _window_bounds("lifetime", observed_at) == (None, observed_at)
    assert _window_bounds("rolling_24h", observed_at) == (
        observed_at - timedelta(hours=24), observed_at,
    )
    assert _window_bounds("rolling_7d", observed_at) == (
        observed_at - timedelta(days=7), observed_at,
    )
    assert _window_bounds("rolling_30d", observed_at) == (
        observed_at - timedelta(days=30), observed_at,
    )
    assert _window_bounds("calendar_week", observed_at) == (
        datetime(2026, 8, 10, tzinfo=timezone.utc), observed_at,
    )
    assert _window_bounds("calendar_month", observed_at) == (
        datetime(2026, 8, 1, tzinfo=timezone.utc), observed_at,
    )


@pytest.mark.asyncio
async def test_every_internal_library_stat_records_the_documented_value(db_session) -> None:
    workspace = _workspace(db_session)
    observed_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    current = observed_at - timedelta(days=1)
    other_workspace_id = generate_ulid()
    other_entity_id = generate_ulid()

    tasks = [
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Created and completed on time", status="completed", details={},
            created_at=current, started_at=observed_at - timedelta(hours=6),
            completed_at=observed_at - timedelta(hours=4),
            deadline=observed_at - timedelta(hours=3),
        ),
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Created and completed late", status="completed", details={},
            created_at=current, started_at=observed_at - timedelta(hours=6),
            completed_at=observed_at - timedelta(hours=2),
            deadline=observed_at - timedelta(hours=3),
        ),
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Open and overdue", status="pending", details={},
            created_at=current, deadline=observed_at - timedelta(hours=1),
        ),
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Explicitly blocked", status="blocked", details={}, created_at=current,
        ),
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Waiting for dependency", status="pending",
            details={"dependency_status": "waiting", "depends_on_task_ids": [generate_ulid()]},
            created_at=current,
        ),
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Failed terminal task", status="failed", details={},
            created_at=current, completed_at=observed_at - timedelta(hours=1),
            deadline=observed_at - timedelta(hours=2),
        ),
        # This contributes to completed throughput, on-time rate, and cycle time,
        # but not the cohort completion rate because it was created before the week.
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            title="Old task completed this week", status="completed", details={},
            created_at=observed_at - timedelta(days=10),
            started_at=observed_at - timedelta(hours=7),
            completed_at=observed_at - timedelta(hours=1), deadline=observed_at,
        ),
        Task(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=other_workspace_id,
            title="Other workspace", status="completed", details={}, created_at=current,
            completed_at=observed_at - timedelta(hours=1),
        ),
        Task(
            id=generate_ulid(), entity_id=other_entity_id, workspace_id=workspace.id,
            title="Other entity", status="completed", details={}, created_at=current,
            completed_at=observed_at - timedelta(hours=1),
        ),
    ]

    workflow_rows = [
        WorkflowRun(
            id=generate_ulid(), workflow_id=generate_ulid(), entity_id=workspace.entity_id,
            workspace_id=workspace.id, status=status, created_at=current,
        )
        for status in ("completed", "failed", "cancelled", "running")
    ]
    workflow_rows.extend([
        WorkflowRun(
            id=generate_ulid(), workflow_id=generate_ulid(), entity_id=workspace.entity_id,
            workspace_id=workspace.id, status="completed",
            created_at=observed_at - timedelta(days=40),
        ),
        WorkflowRun(
            id=generate_ulid(), workflow_id=generate_ulid(), entity_id=workspace.entity_id,
            workspace_id=other_workspace_id, status="completed", created_at=current,
        ),
    ])

    agent_rows = [
        AgentExecution(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            agent_id="stats-agent", status=status, created_at=current,
        )
        for status in ("completed", "failed", "cancelled", "running")
    ]
    agent_rows.extend([
        AgentExecution(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            agent_id="stats-agent", status="completed",
            created_at=observed_at - timedelta(days=40),
        ),
        AgentExecution(
            id=generate_ulid(), entity_id=other_entity_id, workspace_id=workspace.id,
            agent_id="stats-agent", status="completed", created_at=current,
        ),
    ])

    group_a = DocumentGroup(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        name="Primary knowledge",
    )
    group_b = DocumentGroup(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        name="Secondary knowledge",
    )
    group_other = DocumentGroup(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=other_workspace_id,
        name="Other workspace knowledge",
    )
    ready = Document(
        id=generate_ulid(), entity_id=workspace.entity_id, name="ready.md",
        vector_status=VectorStatus.READY, is_trashed=False,
    )
    indexed = Document(
        id=generate_ulid(), entity_id=workspace.entity_id, name="indexed.md",
        vector_status=VectorStatus.INDEXED, is_trashed=False,
    )
    pending = Document(
        id=generate_ulid(), entity_id=workspace.entity_id, name="pending.md",
        vector_status=VectorStatus.PENDING, is_trashed=False,
    )
    trashed = Document(
        id=generate_ulid(), entity_id=workspace.entity_id, name="trashed.md",
        vector_status=VectorStatus.READY, is_trashed=True,
    )
    ready_elsewhere = Document(
        id=generate_ulid(), entity_id=workspace.entity_id, name="elsewhere.md",
        vector_status=VectorStatus.READY, is_trashed=False,
    )
    wrong_entity = Document(
        id=generate_ulid(), entity_id=other_entity_id, name="wrong-entity.md",
        vector_status=VectorStatus.READY, is_trashed=False,
    )

    db_session.add_all([
        *tasks, *workflow_rows, *agent_rows,
        group_a, group_b, group_other,
        ready, indexed, pending, trashed, ready_elsewhere, wrong_entity,
    ])
    await db_session.flush()
    db_session.add_all([
        DocumentGroupMember(document_id=ready.id, group_id=group_a.id),
        DocumentGroupMember(document_id=ready.id, group_id=group_b.id),
        DocumentGroupMember(document_id=indexed.id, group_id=group_a.id),
        DocumentGroupMember(document_id=pending.id, group_id=group_a.id),
        DocumentGroupMember(document_id=trashed.id, group_id=group_a.id),
        DocumentGroupMember(document_id=ready_elsewhere.id, group_id=group_other.id),
        DocumentGroupMember(document_id=wrong_entity.id, group_id=group_a.id),
    ])
    await db_session.flush()

    observed_values: dict[str, float] = {}
    for library_key in INTERNAL_LIBRARY_EXPECTATIONS:
        stat = await create_stat_from_library(
            db_session,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            library_key=library_key,
            collection_cadence=None,
        )
        observation = await collect_stat(db_session, stat, observed_at=observed_at)
        observed_values[library_key] = float(observation.value)
        assert observation.evidence["metric"] == stat.collector_config["metric"]
        assert observation.source == "workspace_internal"
        assert stat.current_value == observation.value
        assert stat.current_value_updated_at == observed_at
        assert stat.last_collection_status == "success"

    assert observed_values == pytest.approx(INTERNAL_LIBRARY_EXPECTATIONS)


@pytest.mark.asyncio
async def test_every_x_library_stat_records_its_public_metric(db_session, monkeypatch) -> None:
    workspace = _workspace(db_session)
    connection = Integration(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        provider="x",
        status="active",
        config={},
        credentials={},
    )
    db_session.add(connection)
    await db_session.flush()

    class CredentialServiceStub:
        def lease_integration(self, integration, *, requester, reason):
            assert integration.id == connection.id
            assert requester.id == connection.id
            assert reason.startswith("measure:twitter_x.")
            return {"access_token": "test-access-token"}

    # The API response uses short metric names, while the expectation keys are
    # full library keys. Keep the fixture shaped exactly like X API v2.
    async def fake_get_me(token: str, params: dict) -> str:
        assert token == "test-access-token"
        assert params == {}
        return json.dumps({"data": {"public_metrics": {
            library_key.removeprefix("twitter_x."): value
            for library_key, value in X_LIBRARY_EXPECTATIONS.items()
        }}})

    monkeypatch.setattr(twitter_x_measurer, "get_credential_service", lambda: CredentialServiceStub())
    monkeypatch.setattr(twitter_x_measurer._adapter, "_get_me", fake_get_me)

    observed_values: dict[str, float] = {}
    for library_key in X_LIBRARY_EXPECTATIONS:
        stat = await create_stat_from_library(
            db_session,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            library_key=library_key,
            collection_cadence=None,
            collector_overrides={"connection_id": connection.id},
        )
        observation = await collect_stat(
            db_session, stat, observed_at=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
        )
        observed_values[library_key] = float(observation.value)
        assert observation.source == "integration:twitter_x"
        assert observation.evidence == {
            "integration_key": "twitter_x",
            "provider": "twitter_x",
            "connection_id": connection.id,
            "metric_key": library_key.removeprefix("twitter_x."),
        }
        assert stat.collector_config["integration_key"] == "twitter_x"
        assert "provider" not in stat.collector_config

    assert observed_values == pytest.approx(X_LIBRARY_EXPECTATIONS)


@pytest.mark.asyncio
async def test_internal_stat_collects_history_and_updates_linked_goal(db_session) -> None:
    workspace = _workspace(db_session)
    completed_at = datetime.now(timezone.utc).replace(second=5, microsecond=0)
    db_session.add(Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Completed task",
        status="completed",
        completed_at=completed_at,
        details={},
    ))
    await db_session.flush()

    stat = await create_stat_from_library(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        library_key="workspace.tasks.completed",
    )
    goal = await create_goal(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        stat_id=stat.id,
        title="Complete two tasks",
        metric_key=stat.key,
        target_value=2,
        baseline_value=0,
    )

    observation = await collect_stat(db_session, stat, observed_at=completed_at)
    duplicate = await collect_stat(
        db_session, stat, observed_at=completed_at + timedelta(seconds=20),
    )

    assert observation.value == Decimal("1")
    assert duplicate.id == observation.id
    assert stat.current_value == Decimal("1")
    assert stat.last_collection_status == "success"
    assert goal.current_value == Decimal("1")
    assert goal.measurement_source is None
    assert goal.measurement_cadence is None
    [history] = await list_observations(db_session, stat_id=stat.id)
    assert history.id == observation.id

    measurement = (await db_session.execute(select(GoalMeasurement).where(
        GoalMeasurement.goal_id == goal.id,
    ))).scalar_one()
    assert measurement.value == Decimal("1")
    assert measurement.source == f"workspace_stat:{stat.key}"

    scheduled = (await db_session.execute(select(ScheduledJob).where(
        ScheduledJob.job_id == f"wsstat:{stat.id}",
    ))).scalar_one()
    assert scheduled.execution_type == "workspace_stat_collection"
    assert scheduled.execution_target == {"stat_id": stat.id}
    assert scheduled.schedule_kind == "every"
    assert scheduled.every_seconds == 86400


@pytest.mark.asyncio
async def test_manual_stat_accepts_append_only_values_without_schedule(db_session) -> None:
    workspace = _workspace(db_session)
    await db_session.flush()
    stat = await create_stat(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        key="sales.qualified_leads",
        name="Qualified leads",
        unit="leads",
        collector_type="manual",
        window="calendar_week",
    )

    first = await record_observation(db_session, stat, value=3, source="manual")
    second = await record_observation(db_session, stat, value=5, source="manual")

    history = await list_observations(db_session, stat_id=stat.id)
    assert {row.id for row in history} == {first.id, second.id}
    assert stat.current_value == Decimal("5")
    scheduled = (await db_session.execute(select(ScheduledJob).where(
        ScheduledJob.job_id == f"wsstat:{stat.id}",
    ))).scalar_one_or_none()
    assert scheduled is None


@pytest.mark.asyncio
async def test_library_stat_allows_explicitly_disabling_default_schedule(db_session) -> None:
    workspace = _workspace(db_session)
    await db_session.flush()
    stat = await create_stat_from_library(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        library_key="workspace.tasks.completed",
        collection_cadence=None,
    )

    assert stat.collection_cadence is None
    scheduled = (await db_session.execute(select(ScheduledJob).where(
        ScheduledJob.job_id == f"wsstat:{stat.id}",
    ))).scalar_one_or_none()
    assert scheduled is None


@pytest.mark.asyncio
async def test_blueprint_stats_install_before_linked_goals_and_export_portably(db_session) -> None:
    workspace = _workspace(db_session)
    await db_session.flush()

    stat_id = await _install_stat(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        payload={"library_key": "workspace.tasks.completed"},
        mode=InstallMode.LIVE,
    )
    goal_id = await _install_goal(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        g={
            "title": "Complete ten tasks",
            "stat_key": "workspace.tasks.completed",
            "target_value": 10,
        },
        mode=InstallMode.LIVE,
    )

    exported_stats = await _export_stats(db_session, workspace.entity_id, workspace.id)
    exported_goals = await _export_goals(db_session, workspace.entity_id, workspace.id)

    assert stat_id
    assert goal_id
    assert exported_stats[0]["library_key"] == "workspace.tasks.completed"
    assert "current_value" not in exported_stats[0]
    assert exported_goals[0]["stat_key"] == "workspace.tasks.completed"
    assert exported_goals[0]["measurement_source"] is None


@pytest.mark.asyncio
async def test_stat_library_only_exposes_connected_integration_stats_and_binds_account(client) -> None:
    username = "stats_connections"
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Stats Connections",
        },
    )
    assert register.status_code == 200, register.text
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Connection-aware stats"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    without_connections = await client.get(
        "/api/v1/workspaces/stats/library", headers=headers,
    )
    assert without_connections.status_code == 200
    assert {
        item["key"] for item in without_connections.json()["items"]
    } == set(INTERNAL_LIBRARY_EXPECTATIONS)

    first = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "twitter_x", "config": {"username": "primary"}},
    )
    second = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "x", "config": {"username": "secondary"}},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["provider"] == "twitter_x"
    assert second.json()["config"]["is_default"] is False

    with_connections = await client.get(
        "/api/v1/workspaces/stats/library", headers=headers,
    )
    assert with_connections.status_code == 200
    library_items = with_connections.json()["items"]
    assert {item["key"] for item in library_items} == (
        set(INTERNAL_LIBRARY_EXPECTATIONS) | set(X_LIBRARY_EXPECTATIONS)
    )
    x_followers = next(
        item for item in library_items
        if item["key"] == "twitter_x.followers_count"
    )
    assert {
        connection["id"] for connection in x_followers["available_connections"]
    } == {first.json()["id"], second.json()["id"]}
    assert {
        connection["label"] for connection in x_followers["available_connections"]
    } == {"@primary", "@secondary"}
    assert x_followers["integration_key"] == "twitter_x"
    assert {
        connection["integration_key"]
        for connection in x_followers["available_connections"]
    } == {"twitter_x"}
    assert "provider" not in x_followers["collector_config"]

    missing_connection = await client.post(
        f"/api/v1/workspaces/{workspace_id}/stats",
        headers=headers,
        json={"library_key": "twitter_x.followers_count"},
    )
    assert missing_connection.status_code == 400
    assert "select an active twitter_x connection" in missing_connection.json()["detail"]

    invalid_connection = await client.post(
        f"/api/v1/workspaces/{workspace_id}/stats",
        headers=headers,
        json={
            "library_key": "twitter_x.followers_count",
            "collector_config": {"connection_id": generate_ulid()},
        },
    )
    assert invalid_connection.status_code == 400
    assert "selected twitter_x connection is not active" in invalid_connection.json()["detail"]

    spoofed_integration_key = await client.post(
        f"/api/v1/workspaces/{workspace_id}/stats",
        headers=headers,
        json={
            "library_key": "twitter_x.followers_count",
            "collector_config": {
                "connection_id": second.json()["id"],
                "integration_key": "slack",
            },
        },
    )
    assert spoofed_integration_key.status_code == 400
    assert "requires integration_key 'twitter_x'" in (
        spoofed_integration_key.json()["detail"]
    )

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/stats",
        headers=headers,
        json={
            "library_key": "twitter_x.followers_count",
            "collector_config": {"connection_id": second.json()["id"]},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["collector_config"]["connection_id"] == second.json()["id"]
    assert created.json()["collector_config"]["integration_key"] == "twitter_x"
    assert "provider" not in created.json()["collector_config"]

    for integration_id in (first.json()["id"], second.json()["id"]):
        disabled = await client.put(
            f"/api/v1/integrations/{integration_id}",
            headers=headers,
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text

    after_disconnect = await client.get(
        "/api/v1/workspaces/stats/library", headers=headers,
    )
    assert after_disconnect.status_code == 200
    assert {
        item["key"] for item in after_disconnect.json()["items"]
    } == set(INTERNAL_LIBRARY_EXPECTATIONS)


@pytest.mark.asyncio
async def test_workspace_stats_quick_view_is_personal_and_filters_unknown_stats(client) -> None:
    username = "stats_quick_view"
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Stats Quick View",
        },
    )
    assert register.status_code == 200, register.text
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Configurable quick stats"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    created_stats = []
    for library_key in (
        "workspace.tasks.completed",
        "workspace.knowledge.ready_documents",
    ):
        created = await client.post(
            f"/api/v1/workspaces/{workspace_id}/stats",
            headers=headers,
            json={"library_key": library_key},
        )
        assert created.status_code == 201, created.text
        created_stats.append(created.json())

    first_id = created_stats[0]["id"]
    second_id = created_stats[1]["id"]
    unknown_id = generate_ulid()
    saved = await client.put(
        f"/api/v1/workspaces/{workspace_id}/stats/quick-view",
        headers=headers,
        json={
            "ordered_stat_ids": [second_id, first_id, second_id, unknown_id],
            "hidden_stat_ids": [first_id, unknown_id, first_id],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "ordered_stat_ids": [second_id, first_id],
        "hidden_stat_ids": [first_id],
        "configured": True,
    }

    loaded = await client.get(
        f"/api/v1/workspaces/{workspace_id}/stats/quick-view",
        headers=headers,
    )
    assert loaded.status_code == 200, loaded.text
    assert loaded.json() == saved.json()
