"""App presentation and built-in Skills survive a different deployment's IDs."""

import copy
import json
import runpy
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.blueprints.app_view import remap_app_view
from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.payload import PayloadError, validate_payload
from packages.core.models.base import generate_ulid
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.builtin_skill_loader import seed_builtin_skills
from packages.core.services.marketplace_skill_service import ensure_marketplace_skill_installed
from packages.core.services.workspace_app_view import normalize_app_view_config
from packages.core.governance.approval_policy_service import initialize_workspace_approval


def test_app_mapping_covers_all_target_types_and_presentation():
    source = {
        "layout": "cover",
        "visual": "plain",
        "title": "Daily Radar",
        "hero": "workflow:old-flow",
        "order": ["workflow:old-flow", "action:draft"],
        "hidden": ["automation:old-job"],
        "labels": {"workflow:old-flow": "Collect"},
        "descriptions": {"workflow:old-flow": "Find public Posts and Reels."},
        "entries": {"action:draft": {"title": "Draft"}},
        "forms": {"action:draft": [{"key": "brief", "label": "Brief", "type": "text"}]},
        "prompts": {"action:draft": "Prepare an original draft."},
        "targets": {
            "workflow:old-flow": "workflow:old-flow",
            "action:draft": "agent:old-agent",
            "service:review": "default",
        },
    }
    portable = remap_app_view(
        source, workflows={"old-flow": "collect"}, agents={"old-agent": "writer"}, automations={"old-job": "daily"}
    )
    assert portable["hero"] == "workflow:collect"
    assert portable["targets"]["action:draft"] == "agent:writer"
    assert portable["targets"]["service:review"] == "agent:default"
    installed = remap_app_view(
        portable, workflows={"collect": "new-flow"}, agents={"writer": "new-agent"}, automations={"daily": "new-job"}
    )
    again = remap_app_view(
        installed, workflows={"new-flow": "collect"}, agents={"new-agent": "writer"}, automations={"new-job": "daily"}
    )
    assert again == portable
    assert not any(old in json.dumps(portable) for old in ["old-flow", "old-agent", "old-job"])
    with pytest.raises(ValueError, match="not included"):
        remap_app_view(source, workflows={}, agents={}, automations={})
    with pytest.raises(ValueError, match="HTTPS asset"):
        remap_app_view(
            {"motion_cover_url": "/viewer/local", "motion_cover_source_url": "/viewer/local"},
            workflows={},
            agents={},
            automations={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [InstallMode.LIVE, InstallMode.SIMULATE])
async def test_export_install_reexport_preserves_app_and_builtin_with_new_ids(db_session, mode, tmp_path):
    db = db_session
    entity = generate_ulid()
    ws = Workspace(
        id=generate_ulid(),
        entity_id=entity,
        name="Instagram Studio",
        kind="social_media",
        operating_model={},
        settings={"access_mode": "members_only"},
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity,
        workspace_id=ws.id,
        slug="researcher",
        name="Researcher",
        is_public=False,
        status="active",
        config={"model_mode": "inherit"},
    )
    db.add_all([ws, agent])
    await db.flush()
    await initialize_workspace_approval(db, ws)
    db.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity,
            workspace_id=ws.id,
            agent_id=agent.id,
            service_key="research",
            status="active",
        )
    )
    builtin = next(s for s in await seed_builtin_skills(db) if s.slug == "file_engine")
    old_skill_id = builtin.id
    sb = AgentSkillBinding(id=generate_ulid(), agent_id=agent.id, skill_id=builtin.id, status="active")
    db.add(sb)
    wf = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity,
        workspace_id=ws.id,
        name="Collect",
        trigger_type="manual",
        steps=[
            {"id": "start", "name": "start", "type": "trigger", "config": {"trigger_type": "manual"}, "next": ["end"]},
            {"id": "end", "name": "end", "type": "end", "config": {}, "next": []},
        ],
        variables={},
        status="active",
        is_active=True,
    )
    db.add(wf)
    await db.flush()
    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=entity,
        workspace_id=ws.id,
        workflow_id=wf.id,
        name="Collect",
        trigger_type="manual",
        status="active",
        enabled=True,
        config={"chat_entrypoint": {"enabled": True, "title": "Collect", "description": "Long execution instructions"}},
    )
    db.add(binding)
    await db.flush()
    job = ScheduledJob(
        entity_id=entity,
        workspace_id=ws.id,
        job_id=f"daily-{ws.id[-8:]}",
        name="Daily files",
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        execution_type="skill",
        execution_target={"skill_id": builtin.id},
        enabled=False,
    )
    db.add(job)
    await db.flush()
    source_key = f"workflow:{binding.id}"
    ws.operating_model = {
        "strategist": {"runtime_state": "idle"},
        "services": [{"key": "research", "name": "Research"}],
        "app_view": {
            "layout": "cover",
            "visual": "plain",
            "title": "Instagram Daily Radar",
            "description": "Track creators and prepare original content.",
            "hero": source_key,
            "order": [source_key],
            "hidden": ["service:research"],
            "descriptions": {source_key: "Find today’s best public Posts and Reels."},
            "entries": {"action:write": {"title": "Write"}},
            "targets": {"action:write": f"agent:{agent.id}"},
            "prompts": {"action:write": "Prepare a draft."},
        },
    }
    ws.operating_model["app_view"]["order"].append(f"automation:{job.job_id}")
    await db.flush()
    payload = await export_workspace(
        db, ws.id, title=ws.name, context=ExportContext(include_embedded_agents=True, include_embedded_skills=True)
    )
    validate_payload(payload)
    requirement = next(s for s in payload["contract"]["requires"]["skills"] if s["slug"] == "file_engine")
    assert requirement["marketplace_id"] == "builtin:file_engine"
    assert old_skill_id not in json.dumps(payload)
    assert binding.id not in json.dumps(payload)
    # Model a clean target catalog with a different built-in row ID, in test DB only.
    await db.delete(sb)
    await db.flush()
    await db.delete(builtin)
    await db.flush()
    result = await install_blueprint(db, payload=payload, entity_id=generate_ulid(), mode=mode)
    assert not [t for t in result.todos if t.kind == "missing_skill"]
    target = await db.get(Workspace, result.workspace_id)
    config = normalize_app_view_config(target.operating_model["app_view"])
    assert config["layout"] == "cover"
    assert config["hero"] == f"workflow:{result.workflow_binding_ids[0]}"
    assert config["descriptions"][config["hero"]] == "Find today’s best public Posts and Reels."
    target_agent_id = config["targets"]["action:write"].removeprefix("agent:")
    bound = (
        (
            await db.execute(
                select(Skill)
                .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
                .where(AgentSkillBinding.agent_id == target_agent_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(bound) == 1 and bound[0].slug == "file_engine" and bound[0].id != old_skill_id
    exported = await export_workspace(
        db,
        target.id,
        title=target.name,
        context=ExportContext(include_embedded_agents=True, include_embedded_skills=True),
    )
    assert exported["recipe"]["operating_model"]["app_view"] == payload["recipe"]["operating_model"]["app_view"]
    assert exported["contract"]["requires"]["skills"] == payload["contract"]["requires"]["skills"]
    assert exported["recipe"]["scheduled_jobs"] == payload["recipe"]["scheduled_jobs"]
    for name, data in [("source", payload), ("installed", exported)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data, indent=2))
    checker = runpy.run_path(
        str(Path(__file__).parents[1] / ".agents/skills/manor-workspace-blueprint/scripts/compare_roundtrip.py")
    )
    assert checker["compare"](payload, exported) == []

    # Updating and reverting an installed App must retain its runtime targets.
    from packages.core.blueprints.configuration_upgrade import (
        capture_configuration,
        plan_configuration,
        apply_configuration,
        revert_configuration,
    )

    baseline = await capture_configuration(db, workspace=target, payload=payload)
    updated = copy.deepcopy(payload)
    updated["recipe"]["operating_model"]["app_view"]["title"] = "Updated radar"
    plan = await plan_configuration(db, workspace=target, payload=updated, baseline=baseline)
    app_item = next(i for i in plan["items"] if i["key"] == "workspace_configuration:operating_model/app_view")
    assert app_item["action"] == "update"
    changes = await apply_configuration(db, workspace=target, payload=updated, plan=plan)
    assert target.operating_model["app_view"]["title"] == "Updated radar"
    assert target.operating_model["app_view"]["hero"] == config["hero"]
    assert target.operating_model["app_view"]["targets"] == config["targets"]
    await revert_configuration(db, workspace=target, restore=changes["restore"])
    assert target.operating_model["app_view"] == config
    bad = copy.deepcopy(payload)
    bad["recipe"]["operating_model"]["app_view"]["hero"] = "workflow:source-row-id"
    with pytest.raises(PayloadError, match="not included"):
        validate_payload(bad)


@pytest.mark.asyncio
async def test_builtin_identity_never_resolves_a_local_or_unrelated_marketplace_skill(db_session):
    db = db_session
    entity = generate_ulid()
    slug = "builtin-test-isolated"
    impostor = Skill(
        id=generate_ulid(),
        entity_id=entity,
        slug=slug,
        name=slug,
        system_prompt="Test guidance.",
        config={"source": "builtin"},
        is_public=True,
        status="active",
    )
    db.add(impostor)
    await db.flush()
    with pytest.raises(ValueError, match="missing or ambiguous"):
        await ensure_marketplace_skill_installed(db, entity_id=entity, skill_id=f"builtin:{slug}")
    builtin = Skill(
        id=generate_ulid(),
        entity_id=None,
        slug=slug,
        name=slug,
        system_prompt="Test guidance.",
        config={"source": "builtin"},
        is_public=True,
        status="active",
    )
    db.add(builtin)
    await db.flush()
    # A platform row cannot invent a repository-controlled built-in identity.
    with pytest.raises(ValueError, match="missing or ambiguous"):
        await ensure_marketplace_skill_installed(db, entity_id=entity, skill_id=f"builtin:{slug}")
    with pytest.raises(ValueError, match="not found"):
        await ensure_marketplace_skill_installed(db, entity_id=entity, skill_id=generate_ulid())
    # The catalog also prevents two platform built-ins from sharing an identity.
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(
                Skill(
                    id=generate_ulid(),
                    entity_id=None,
                    slug=slug,
                    name=slug,
                    system_prompt="Test guidance.",
                    config={"source": "builtin"},
                    is_public=True,
                    status="active",
                )
            )
            await db.flush()
