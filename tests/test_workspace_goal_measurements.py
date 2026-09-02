from __future__ import annotations

import copy
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.constants.workspace_drafts import (
    CREATION_PREFERENCES_FIELD,
    CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION,
    WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD,
)
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Agent, Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.services.workspace_goal_measurements import (
    draft_measurement_library,
    measurement_stat_definition,
    resolve_draft_goal_measurements,
)
from packages.core.services.workspace_setup_service import (
    WorkspaceProvisioningError,
    WorkspaceSetupSession,
    finalize_setup,
)


def _measurement() -> dict:
    return {
        "key": "interview_readiness",
        "name": "Interview readiness score",
        "description": "Mean of coding, ML design and behavioral rubric scores (0–5), divided by 5 × 100; missing assessments stay unmeasured.",
        "source": "Coach-reviewed mock interview rubric and assessment report, recorded manually after each review.",
        "unit": "percent",
        "value_type": "percent",
        "window": "latest",
    }


def _goal(**updates) -> dict:
    return {
        "goal_key": "interview_readiness",
        "title": "Interview readiness",
        "description": "Reach the confirmed mock interview standard.",
        "target": "80%",
        "cadence": "weekly",
        "measurement": _measurement(),
        **updates,
    }


def _fields(**updates) -> dict:
    from test_workspace_creation_modes import _complete_new_draft_fields

    return {
        **copy.deepcopy(_complete_new_draft_fields()),
        "goals": [_goal()],
        "stats": [],
        "heartbeat_enabled": False,
        CREATION_PREFERENCES_FIELD: {"goal_confirmed": True, "autonomy_confirmed": True},
        **updates,
    }


async def _create(db, *, fields=None):
    entity_id = generate_ulid()
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Interview coach", status="active")
    db.add(agent)
    await db.flush()
    fields = fields or _fields()
    fields["agent_mappings"] = [{
        "service_key": "workspace_operations", "agent_id": agent.id, "strategy": "match",
    }]
    session = WorkspaceSetupSession(entity_id=entity_id, fields=fields, messages=[], ready=True, missing=[])
    workspace_id = await finalize_setup(session, db)
    return await db.get(Workspace, workspace_id)


@pytest.mark.parametrize("field", ["key", "name", "description", "source", "unit", "value_type", "window"])
def test_custom_measurement_requires_a_complete_definition(field):
    measurement = _measurement()
    measurement.pop(field)
    with pytest.raises(ValueError, match="measurement requires"):
        measurement_stat_definition(measurement, cadence="weekly")


@pytest.mark.parametrize("measurement", [
    {**_measurement(), "source": "   "},
    {**_measurement(), "key": "A bad key"},
    {**_measurement(), "key": "a" * 101},
    {"library_key": "not.a.collector"},
    {"library_key": "workspace.tasks.created"},  # Not Goal-eligible.
    {"library_key": "workspace.tasks.completed", "key": "BAD"},
    {"library_key": "workspace.tasks.completed", "name": "  "},
])
def test_invalid_measurements_are_not_accepted(measurement):
    with pytest.raises(ValueError):
        measurement_stat_definition(measurement, cadence="weekly")


def test_new_drafts_require_measurements_but_do_not_rewrite_legacy_drafts():
    goal = _goal()
    goal.pop("measurement")
    with pytest.raises(ValueError, match="confirmed measurement"):
        resolve_draft_goal_measurements(_fields(goals=[goal]))
    for version in (None, 2):
        fields = _fields(goals=[goal])
        fields[WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD] = version
        assert resolve_draft_goal_measurements(fields) == ([goal], [])
    assert resolve_draft_goal_measurements(_fields(goals=[])) == ([], [])


def test_stat_references_are_validated_and_share_a_single_definition():
    goals, stats = resolve_draft_goal_measurements(_fields(goals=[_goal(), _goal(goal_key="second_goal")]))
    assert len(stats) == 1
    assert all(goal["stat_key"] == "interview_readiness" for goal in goals)
    for fields in (
        _fields(goals=[{"goal_key": "missing", "stat_key": "missing"}]),
        _fields(goals=[{"stat_key": "workspace.tasks.created"}], stats=[{"library_key": "workspace.tasks.created"}]),
        _fields(stats=[{"key": "bad stat", "name": "Invalid"}]),
        _fields(goals=[{"stat_key": "undefined_manual"}], stats=[{"key": "undefined_manual", "name": "No rubric"}]),
        _fields(stats=[{**stats[0], "description": "A different formula"}]),
        _fields(goals={"not": "a list"}),
    ):
        with pytest.raises(ValueError):
            resolve_draft_goal_measurements(fields)


@pytest.mark.asyncio
async def test_propose_goal_rejects_incomplete_measurement_without_mutation(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _propose_goal

    draft = WorkspaceDraft(
        id=generate_ulid(), entity_id=generate_ulid(), user_id=generate_ulid(),
        fields=_fields(goals=[]), status="active", messages=[], ready=False, missing=[],
    )
    db_session.add(draft)
    await db_session.flush()
    before = copy.deepcopy(draft.fields)
    args = _goal()
    args.pop("measurement")
    result = json.loads(await _propose_goal(
        db_session, entity_id=draft.entity_id, user_id=draft.user_id, draft_id=draft.id, **args,
    ))
    assert result["ok"] is False
    assert draft.fields == before
    result = json.loads(await _propose_goal(
        db_session, entity_id=draft.entity_id, user_id=draft.user_id, draft_id=draft.id, **_goal(),
    ))
    assert result["ok"] is True
    assert draft.fields["goals"][0]["stat_key"] == "interview_readiness"
    assert draft.fields["goals"][0]["measurement"] == _measurement()
    imported = measurement_stat_definition(_measurement(), cadence="weekly")
    imported["library_key"] = None  # The canonical Blueprint export shape.
    draft.fields = {**draft.fields, "stats": [imported]}
    updated = {**_measurement(), "description": "An explicitly confirmed replacement rubric."}
    result = json.loads(await _propose_goal(
        db_session, entity_id=draft.entity_id, user_id=draft.user_id,
        draft_id=draft.id, **_goal(measurement=updated),
    ))
    assert result["ok"] is True
    _, stats = resolve_draft_goal_measurements(draft.fields)
    assert stats[0]["description"] == updated["description"]


@pytest.mark.asyncio
async def test_incomplete_measurement_blocks_lint_and_finalization(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _lint_draft

    goal = _goal()
    goal.pop("measurement")
    fields = _fields(goals=[goal])
    draft = WorkspaceDraft(
        id=generate_ulid(), entity_id=generate_ulid(), user_id=generate_ulid(),
        fields=fields, status="ready", messages=[], ready=True, missing=[],
    )
    db_session.add(draft)
    await db_session.flush()
    result = json.loads(await _lint_draft(
        db_session, entity_id=draft.entity_id, user_id=draft.user_id, draft_id=draft.id,
    ))
    assert any(issue["severity"] == "P0" and issue["where"] == "goals.measurement" for issue in result["issues"])
    session = WorkspaceSetupSession(entity_id=draft.entity_id, fields=fields, messages=[], ready=True, missing=[])
    with pytest.raises(WorkspaceProvisioningError, match="confirmed measurement"):
        await finalize_setup(session, db_session)
    assert (await db_session.execute(select(Workspace).where(Workspace.entity_id == draft.entity_id))).scalars().all() == []


@pytest.mark.asyncio
async def test_creation_binds_manual_stat_without_inventing_a_baseline_or_score(db_session):
    from packages.core.stats.service import record_observation

    workspace = await _create(db_session)
    stat = (await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace.id))).scalar_one()
    goal = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace.id))).scalar_one()
    assert goal.stat_id == stat.id
    assert goal.target_value == Decimal("80")
    assert goal.baseline_value is None and goal.current_value is None
    assert goal.measurement_source is None and goal.measurement_cadence is None
    assert stat.current_value is None
    assert stat.collector_type == "manual"
    assert stat.description == _measurement()["description"]
    assert stat.collector_config == {"source": _measurement()["source"]}
    assert stat.collection_cadence == "weekly"
    assert (await db_session.execute(select(WorkspaceStatObservation).where(WorkspaceStatObservation.stat_id == stat.id))).scalars().all() == []
    assert (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id))).scalars().all() == []
    await record_observation(db_session, stat, value=60, source="coach_review", evidence={"report": "mock-interview-1"})
    await db_session.refresh(goal)
    assert goal.current_value == Decimal("60")


@pytest.mark.asyncio
@pytest.mark.parametrize("autonomous", [False, True])
async def test_library_collector_follows_workspace_runtime_switch(db_session, autonomous):
    from packages.core.services.workspace_runtime import sync_workspace_runtime_schedules

    workspace = await _create(db_session, fields=_fields(
        heartbeat_enabled=autonomous,
        goals=[_goal(measurement={"library_key": "workspace.tasks.completed"}, target="10")],
    ))
    stat = (await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace.id))).scalar_one()
    assert stat.collector_config["metric"] == "tasks_completed"
    assert stat.current_value is None

    async def collector_jobs():
        return list((await db_session.execute(select(ScheduledJob).where(
            ScheduledJob.workspace_id == workspace.id,
            ScheduledJob.execution_type == "workspace_stat_collection",
        ))).scalars().all())

    assert len(await collector_jobs()) == int(autonomous)
    workspace.heartbeat_enabled = True
    await sync_workspace_runtime_schedules(db_session, workspace)
    assert len(await collector_jobs()) == 1
    workspace.status = "paused"
    await sync_workspace_runtime_schedules(db_session, workspace)
    assert all(not job.enabled for job in await collector_jobs())
    workspace.status = "active"
    await sync_workspace_runtime_schedules(db_session, workspace)
    assert len(await collector_jobs()) == 1
    assert (await collector_jobs())[0].enabled
    workspace.heartbeat_enabled = False
    await sync_workspace_runtime_schedules(db_session, workspace)
    assert await collector_jobs() == []


@pytest.mark.asyncio
async def test_created_measurement_exports_and_reinstalls_without_runtime_values(db_session):
    from packages.core.blueprints.exporter import _export_goals, _export_stats
    from packages.core.blueprints.installer import InstallMode, _install_goal, _install_stat
    from packages.core.stats.service import record_observation

    workspace = await _create(db_session)
    stat = (await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace.id))).scalar_one()
    await record_observation(db_session, stat, value=60, source="coach_review")
    stats = await _export_stats(db_session, workspace.entity_id, workspace.id)
    goals = await _export_goals(db_session, workspace.entity_id, workspace.id)
    assert goals[0]["stat_key"] == stats[0]["key"] == "interview_readiness"
    assert "current_value" not in stats[0] and "current_value" not in goals[0]
    assert stats[0]["collector_config"]["source"] == _measurement()["source"]
    copied = Workspace(id=generate_ulid(), entity_id=workspace.entity_id, name="Copied rubric")
    db_session.add(copied)
    await db_session.flush()
    stat_id = await _install_stat(db_session, entity_id=copied.entity_id, workspace_id=copied.id, payload=stats[0], mode=InstallMode.LIVE)
    goal_id = await _install_goal(db_session, entity_id=copied.entity_id, workspace_id=copied.id, g=goals[0])
    copied_stat = await db_session.get(WorkspaceStat, stat_id)
    copied_goal = await db_session.get(Goal, goal_id)
    assert copied_goal.stat_id == copied_stat.id
    assert copied_goal.current_value is None and copied_stat.current_value is None
    assert await _export_stats(db_session, copied.entity_id, copied.id) == stats
    assert await _export_goals(db_session, copied.entity_id, copied.id) == goals
    # Stat-linked Blueprint goals intentionally have no independent cadence.
    resolved, _ = resolve_draft_goal_measurements(_fields(goals=goals, stats=stats))
    assert resolved[0]["cadence"] == "weekly"


def test_public_stat_edits_require_reconfirmation():
    from packages.core.services.workspace_draft_service import apply_public_field_updates

    draft = WorkspaceDraft(fields=_fields())
    apply_public_field_updates(draft, {"stats": [measurement_stat_definition(_measurement(), cadence="weekly")]})
    assert draft.fields[CREATION_PREFERENCES_FIELD]["goal_confirmed"] is False


@pytest.mark.asyncio
async def test_legacy_setup_completion_cannot_drop_new_measurement_guard(monkeypatch):
    from packages.core.services import workspace_setup_service as setup

    session = await setup.start_setup(generate_ulid())
    fields = _fields()
    fields.pop(WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD)
    fields["goals"][0].pop("measurement")
    completion = SimpleNamespace(content="<workspace_setup>" + json.dumps({
        "fields": fields, "ready": True, "missing": [],
    }) + "</workspace_setup>")
    monkeypatch.setattr(setup, "_build_setup_context", AsyncMock(return_value={}))
    monkeypatch.setattr(setup, "runtime_execute_workspace_setup_turn_completion", AsyncMock(return_value=completion))
    _, updated = await setup.process_setup_turn(session, "Create it", AsyncMock())
    assert updated.fields[WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD] == CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION
    assert updated.ready is False and "goals" in updated.missing


def test_creation_library_does_not_advertise_unbound_external_accounts():
    entries = draft_measurement_library()
    assert entries
    assert all(entry["collector_type"] == "workspace_internal" and entry["goal_eligible"] for entry in entries)
    assert CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION >= 3
