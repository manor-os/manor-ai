"""Configuration upgrades retain Workspace identity, evidence, and local edits."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.blueprints.configuration_upgrade import (
    ConfigurationUpgradeError,
    _merge_portable_dict,
    _remap_runtime_references,
    _read_path,
    _targets,
    _write_path,
    apply_configuration,
    capture_configuration,
    configuration_action,
    plan_configuration,
    revert_configuration,
    validate_configuration_restore,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.goal import Goal, GoalMeasurement
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation


def _payload():
    return {
        "manifest": {"kind": "studio"},
        "contract": {},
        "embedded": {},
        "recipe": {
            "operating_model": {
                "kind": "studio",
                "context": "Original",
                "primary_work": "Make videos",
                "heartbeat_enabled": False,
            }
        },
        "policy": {},
    }


def _choices(plan):
    return {item["key"]: "keep_yours" if item["kind"] == "governance" else "use_blueprint" for item in plan["items"]}


async def _workspace(db, payload=None):
    payload = payload or _payload()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Keep this name",
        kind="studio",
        operating_context="Original",
        primary_work="Make videos",
        heartbeat_enabled=False,
        operating_model={},
        settings={
            "runtime_cursor": "keep",
            "workspace_shared_assets": {"narrator": "user-voice"},
            "_blueprint": {"blueprint_id": "builtin:configuration-test"},
        },
    )
    db.add(workspace)
    await db.flush()
    from packages.core.governance.approval_policy_service import initialize_workspace_approval

    await initialize_workspace_approval(db, workspace)
    return workspace


@pytest.mark.parametrize(
    ("current", "source", "previous", "expected"),
    [
        ({"x": 1}, {"x": 2}, None, "keep_yours"),
        (None, {"x": 2}, None, "missing"),
        ({"x": 1}, {"x": 1}, None, "unchanged"),
        ({"x": 1}, {"x": 2}, {"source_content": {"x": 1}, "installed_content": {"x": 1}}, "update"),
        ({"x": 3}, {"x": 2}, {"source_content": {"x": 2}, "installed_content": {"x": 3}}, "unchanged"),
        ({"x": 3}, {"x": 4}, {"source_content": {"x": 2}, "installed_content": {"x": 3}}, "keep_yours"),
        ({"x": 1}, None, {"source_content": {"x": 1}, "installed_content": {"x": 1}}, "remove"),
        ({"x": 2}, None, {"source_content": {"x": 1}, "installed_content": {"x": 1}}, "keep_yours"),
        ({"x": 1}, None, None, "unchanged"),
    ],
)
def test_configuration_three_way_decisions(current, source, previous, expected):
    assert configuration_action(current, source, previous) == expected


def test_shell_projection_preserves_runtime_and_shared_assets():
    payload = _payload()
    payload["recipe"]["operating_model"]["settings"] = {
        "timezone": "America/Los_Angeles",
        "sandbox": False,
        "workspace_shared_assets": {"narrator": "replacement"},
        "audio_defaults": {"voice": "replacement"},
        "runtime_profile": {"enabled": True},
    }
    targets = _targets(payload, "workspace")
    assert "workspace_configuration:settings/timezone" in targets
    assert not any("shared_assets" in key or "audio_defaults" in key or "sandbox" in key for key in targets)
    workspace = SimpleNamespace(settings={"runtime_cursor": 42, "runtime_profile": {"enabled": False, "local": "keep"}})
    target = targets["workspace_configuration:settings/runtime_profile/enabled"]
    assert _read_path(workspace, target["path"]) == {"value": False}
    _write_path(workspace, target["path"], target["source_content"])
    assert workspace.settings == {"runtime_cursor": 42, "runtime_profile": {"enabled": True, "local": "keep"}}
    _write_path(workspace, target["path"], None)
    assert workspace.settings["runtime_profile"] == {"local": "keep"}


def test_empty_portable_parents_and_missing_shell_match_installer_defaults():
    payload = _payload()
    payload["recipe"]["operating_model"] = {"settings": {"runtime_profile": {}}, "knowledge": {}}
    targets = _targets(payload, "workspace")
    assert targets["workspace_configuration:primary_work"]["source_content"] == {"value": None}
    assert targets["workspace_configuration:operating_context"]["source_content"] == {"value": None}
    assert "workspace_configuration:settings/runtime_profile" not in targets
    assert "workspace_configuration:operating_model/knowledge" not in targets


def test_portable_config_replacement_preserves_private_children():
    current = {
        "agent_id": "local-agent",
        "api_key": "keep-secret",
        "limit": 1,
        "nested": {"workspace_id": "local-workspace", "mode": "old"},
    }

    assert _merge_portable_dict(current, {"limit": 2}) == {
        "agent_id": "local-agent",
        "api_key": "keep-secret",
        "limit": 2,
        "nested": {"workspace_id": "local-workspace"},
    }


def test_runtime_reference_remap_only_rewrites_typed_component_keys():
    assert _remap_runtime_references(
        {"strategist_agent_id": "old", "notes": "old", "nested": {"agent_id": "old", "skill_id": "skill"}},
        {"agents": {"old": "new"}, "skills": {"skill": "new-skill"}},
    ) == {"strategist_agent_id": "new", "notes": "old", "nested": {"agent_id": "new", "skill_id": "new-skill"}}


def test_governance_source_retains_installed_hard_block_choice():
    payload = _payload()
    payload["policy"]["governance"] = {"approval": {"rules": [{
        "id": "author-publish", "effect": "deny",
        "selector": {"kind": "action", "action_key": "social_post.publish"},
    }]}}
    demoted = _targets(
        payload, "workspace", {"_blueprint": {"governance_preset": "follow_default", "author_hard_blocks_installed": False}}
    )["governance:policy"]["source_content"]
    allowed = _targets(
        payload, "workspace", {"_blueprint": {"governance_preset": "follow_default", "author_hard_blocks_installed": True}}
    )["governance:policy"]["source_content"]
    assert demoted["approval"]["rules"][0]["effect"] == "ask"
    assert allowed["approval"]["rules"][0]["effect"] == "deny"
    assert "automated_runs_always_allow" not in allowed


@pytest.mark.asyncio
async def test_configuration_in_place_upgrade_and_stale_revert(db_session):
    payload = _payload()
    workspace = await _workspace(db_session)
    original_id = workspace.id
    payload["recipe"]["operating_model"]["context"] = "Updated studio"
    payload["recipe"]["operating_model"]["settings"] = {
        "timezone": "America/New_York",
        "workspace_shared_assets": {"narrator": "replacement"},
    }
    planned = await plan_configuration(db_session, workspace=workspace, payload=payload)
    context = next(item for item in planned["items"] if item["key"] == "workspace_configuration:operating_context")
    assert context["action"] == "keep_yours"
    with pytest.raises(ConfigurationUpgradeError, match="conflict"):
        await apply_configuration(db_session, workspace=workspace, payload=payload, plan=planned)
    result = await apply_configuration(
        db_session, workspace=workspace, payload=payload, plan=planned, choices=_choices(planned)
    )
    assert workspace.id == original_id
    assert workspace.name == "Keep this name"
    assert workspace.operating_context == "Updated studio"
    assert workspace.settings["runtime_cursor"] == "keep"
    assert workspace.settings["workspace_shared_assets"] == {"narrator": "user-voice"}
    again = await plan_configuration(db_session, workspace=workspace, payload=payload, baseline=result["baseline"])
    assert all(item["action"] == "unchanged" for item in again["items"])
    workspace.operating_context = "Edited after upgrade"
    await db_session.flush()
    with pytest.raises(ConfigurationUpgradeError, match="changed"):
        await validate_configuration_restore(db_session, workspace=workspace, restore=result["restore"])
    workspace.operating_context = "Updated studio"
    await db_session.flush()
    await revert_configuration(db_session, workspace=workspace, restore=result["restore"])
    assert workspace.operating_context == "Original"
    assert "timezone" not in workspace.settings


@pytest.mark.asyncio
async def test_goal_stat_upgrade_preserves_ids_and_observation_history(db_session):
    payload = _payload()
    payload["recipe"]["stats"] = [{"key": "views", "name": "Views", "collector_type": "manual"}]
    payload["recipe"]["goals"] = [
        {"goal_key": "audience", "title": "Grow audience", "stat_key": "views", "target_value": 100}
    ]
    workspace = await _workspace(db_session)
    planned = await plan_configuration(db_session, workspace=workspace, payload=payload)
    installed = await apply_configuration(
        db_session, workspace=workspace, payload=payload, plan=planned, choices=_choices(planned)
    )
    stat = (
        await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace.id))
    ).scalar_one()
    goal = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace.id))).scalar_one()
    identities = (stat.id, goal.id)
    now = datetime.now(timezone.utc)
    db_session.add(
        WorkspaceStatObservation(
            stat_id=stat.id,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            value=23,
            observed_at=now,
            source="manual",
            idempotency_key="original-observation",
        )
    )
    db_session.add(GoalMeasurement(goal_id=goal.id, measured_at=now, value=23, source="manual"))
    stat.current_value = Decimal("23")
    goal.current_value = Decimal("23")
    await db_session.flush()
    newer = copy.deepcopy(payload)
    newer["recipe"]["goals"][0]["target_value"] = 200
    newer["recipe"]["stats"][0]["name"] = "Verified views"
    planned = await plan_configuration(db_session, workspace=workspace, payload=newer, baseline=installed["baseline"])
    upgraded = await apply_configuration(
        db_session, workspace=workspace, payload=newer, plan=planned, choices=_choices(planned)
    )
    assert (stat.id, goal.id) == identities
    assert goal.target_value == 200
    assert stat.name == "Verified views"
    assert goal.current_value == stat.current_value == Decimal("23")
    assert (
        await db_session.execute(select(GoalMeasurement).where(GoalMeasurement.goal_id == goal.id))
    ).scalar_one().value == 23
    assert (
        await db_session.execute(select(WorkspaceStatObservation).where(WorkspaceStatObservation.stat_id == stat.id))
    ).scalar_one().value == 23
    removed = _payload()
    planned = await plan_configuration(db_session, workspace=workspace, payload=removed, baseline=upgraded["baseline"])
    retired = await apply_configuration(
        db_session, workspace=workspace, payload=removed, plan=planned, choices=_choices(planned)
    )
    assert goal.status == "paused"
    assert stat.status == "inactive"
    await revert_configuration(db_session, workspace=workspace, restore=retired["restore"])
    assert goal.status == "active"
    assert stat.status == "active"
    assert (stat.id, goal.id) == identities


@pytest.mark.asyncio
async def test_knowledge_update_and_retirement_keep_documents(db_session):
    payload = _payload()
    payload["embedded"]["knowledge_packs"] = [
        {
            "slug": "studio",
            "title": "Studio notes",
            "mode": "inline_text",
            "starter_documents": [{"key": "voice-guide", "path": "voice.md", "body_md": "Original instructions"}],
        }
    ]
    workspace = await _workspace(db_session)
    planned = await plan_configuration(db_session, workspace=workspace, payload=payload)
    installed = await apply_configuration(
        db_session, workspace=workspace, payload=payload, plan=planned, choices=_choices(planned)
    )
    group = (
        await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace.id))
    ).scalar_one()
    document = (
        await db_session.execute(
            select(Document)
            .join(DocumentGroupMember, Document.id == DocumentGroupMember.document_id)
            .where(DocumentGroupMember.group_id == group.id)
        )
    ).scalar_one()
    identities = group.id, document.id
    newer = copy.deepcopy(payload)
    newer["embedded"]["knowledge_packs"][0]["starter_documents"][0]["body_md"] = "Updated instructions"
    planned = await plan_configuration(db_session, workspace=workspace, payload=newer, baseline=installed["baseline"])
    upgraded = await apply_configuration(
        db_session, workspace=workspace, payload=newer, plan=planned, choices=_choices(planned)
    )
    assert document.metadata_["content_text"] == "Updated instructions"
    await revert_configuration(db_session, workspace=workspace, restore=upgraded["restore"])
    assert document.metadata_["content_text"] == "Original instructions"
    baseline = await capture_configuration(db_session, workspace=workspace, payload=payload)
    planned = await plan_configuration(db_session, workspace=workspace, payload=_payload(), baseline=baseline)
    retired = await apply_configuration(
        db_session, workspace=workspace, payload=_payload(), plan=planned, choices=_choices(planned)
    )
    assert group.workspace_id is None
    assert await db_session.get(Document, document.id) is not None
    await revert_configuration(db_session, workspace=workspace, restore=retired["restore"])
    assert (group.id, document.id) == identities
    assert group.workspace_id == workspace.id
    assert await db_session.get(DocumentGroupMember, (document.id, group.id)) is not None


@pytest.mark.asyncio
async def test_subscription_rebind_reconciles_schedule_and_reverts_exact_local_references(db_session):
    workspace = await _workspace(db_session)
    old = Agent(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, name="Old", slug="old")
    new = Agent(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, name="New", slug="new")
    db_session.add_all([old, new])
    await db_session.flush()
    payload = _payload()
    payload["recipe"]["subscriptions"] = [{"agent_slug": "old", "service_key": "video", "config": {"limit": 1}}]
    payload["recipe"]["scheduled_jobs"] = [
        {
            "job_id": "daily-video",
            "job_type": "interval",
            "schedule_kind": "every",
            "every_seconds": 3600,
            "execution_type": "agent",
            "execution_target": {"service_key": "video"},
            "payload_message": "Make video",
        }
    ]
    plan = await plan_configuration(db_session, workspace=workspace, payload=payload)
    installed = await apply_configuration(
        db_session, workspace=workspace, payload=payload, plan=plan, choices=_choices(plan)
    )
    subscription = (
        await db_session.execute(select(AgentSubscription).where(AgentSubscription.workspace_id == workspace.id))
    ).scalar_one()
    job = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id))).scalar_one()
    identities = subscription.id, job.id
    original_clock = job.next_run_at
    subscription.config = {**subscription.config, "api_key": "keep-local-secret", "workspace_id": workspace.id}
    db_session.add(
        ScheduledJobRun(
            id=generate_ulid(), job_id=job.job_id, status="completed", started_at=datetime.now(timezone.utc)
        )
    )
    await db_session.flush()
    newer = copy.deepcopy(payload)
    newer["recipe"]["subscriptions"][0].update({"agent_slug": "new", "config": {"limit": 2}})
    plan = await plan_configuration(db_session, workspace=workspace, payload=newer, baseline=installed["baseline"])
    assert next(item for item in plan["items"] if item["kind"] == "scheduled_job")["action"] == "unchanged"
    result = await apply_configuration(
        db_session, workspace=workspace, payload=newer, plan=plan, choices=_choices(plan)
    )
    assert (subscription.id, job.id) == identities
    assert subscription.agent_id == job.agent_id == new.id
    assert subscription.config["api_key"] == "keep-local-secret"
    assert subscription.config["workspace_id"] == workspace.id
    assert job.next_run_at == original_clock
    assert (
        await db_session.execute(select(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id))
    ).scalar_one().status == "completed"
    await revert_configuration(db_session, workspace=workspace, restore=result["restore"])
    assert subscription.agent_id == job.agent_id == old.id
    assert subscription.config["limit"] == 1
    assert subscription.config["api_key"] == "keep-local-secret"


@pytest.mark.asyncio
async def test_shared_component_reference_remap_is_cas_revertible(db_session):
    workspace = await _workspace(db_session)
    old = Agent(id=generate_ulid(), entity_id=workspace.entity_id, name="Shared", slug="shared")
    new = Agent(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, name="Copy", slug="copy")
    db_session.add_all([old, new])
    workspace.operating_model = {"strategist_agent_id": old.id, "notes": old.id}
    await db_session.flush()
    payload = _payload()
    plan = await plan_configuration(db_session, workspace=workspace, payload=payload)
    result = await apply_configuration(
        db_session,
        workspace=workspace,
        payload=payload,
        plan=plan,
        choices=_choices(plan),
        reference_remaps={"agents": {old.id: new.id}},
    )
    assert workspace.operating_model["strategist_agent_id"] == new.id
    assert workspace.operating_model["notes"] == old.id
    assert not any("runtime_reference:" in key for key in result["baseline"]["entries"])
    await revert_configuration(db_session, workspace=workspace, restore=result["restore"])
    assert workspace.operating_model["strategist_agent_id"] == old.id


@pytest.mark.asyncio
async def test_heartbeat_upgrade_and_undo_preserve_derived_schedule_rows(db_session):
    workspace = await _workspace(db_session)
    source = _payload()
    source["recipe"]["operating_model"].update({"heartbeat_enabled": True, "heartbeat_cadence": "daily"})
    source["recipe"]["goals"] = [{"title": "Publish daily", "target_value": 30}]
    plan = await plan_configuration(db_session, workspace=workspace, payload=source)
    applied = await apply_configuration(
        db_session, workspace=workspace, payload=source, plan=plan, choices=_choices(plan)
    )
    goal = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace.id))).scalar_one()
    assert goal.goal_key == "goal_publish_daily"
    jobs = list(
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id))).scalars()
    )
    assert jobs and all(job.enabled for job in jobs)
    identities = {job.id for job in jobs}
    await revert_configuration(db_session, workspace=workspace, restore=applied["restore"])
    jobs_after = list(
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id))).scalars()
    )
    assert {job.id for job in jobs_after} == identities
    assert all(not job.enabled for job in jobs_after)
    assert workspace.heartbeat_enabled is False


@pytest.mark.asyncio
async def test_shell_upgrade_refreshes_only_generated_subscription_framing_and_undo(db_session):
    from packages.core.services.workspace_operation_service import _workspace_subscription_prompt

    workspace = await _workspace(db_session)
    agent = Agent(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, name="Local", slug="local"
    )
    db_session.add(agent)
    await db_session.flush()
    original = _workspace_subscription_prompt(workspace, "video", {})
    subscription = AgentSubscription(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
        service_key="video",
        custom_prompt=original,
        status="active",
    )
    db_session.add(subscription)
    await db_session.flush()
    payload = _payload()
    payload["recipe"]["operating_model"]["context"] = "New studio context"
    plan = await plan_configuration(db_session, workspace=workspace, payload=payload)
    applied = await apply_configuration(
        db_session, workspace=workspace, payload=payload, plan=plan, choices=_choices(plan)
    )
    assert "New studio context" in subscription.custom_prompt
    assert subscription.custom_prompt != original
    await revert_configuration(db_session, workspace=workspace, restore=applied["restore"])
    assert subscription.custom_prompt == original
