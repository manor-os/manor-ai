"""Workflow Skill identity must survive a cross-company Blueprint install."""
import copy
import json

import pytest
from sqlalchemy import select

from packages.core.blueprints.exporter import ExportContext, ExportError, export_workspace
from packages.core.blueprints.installer import InstallError, InstallMode, install_blueprint, _blueprint_workflow_definition_values
from packages.core.blueprints.workflow_dependencies import WorkflowSkillDependencyFactory
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.models.user import Entity, User, UserMembership
from packages.core.governance.approval_policy_service import (
    account_defaults, initialize_personal_approval, initialize_workspace_approval,
)
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import Workspace
from packages.core.services.reusable_resource_locks import reusable_resource_ids_from_payload


pytestmark = pytest.mark.oss_regression


def steps(skill):
    return [
        {"id": "start", "type": "trigger", "next": ["read"]},
        {"id": "read", "type": "tool", "config": {
            "tool": "manor", "args": {"skill": skill},
            "steps": [{"id": "nested", "type": "tool", "config": {
                "tool": "manor", "args": {"skill_id": skill}}}],
        }, "chat_projection": "support", "next": ["dynamic"]},
        {"id": "dynamic", "type": "tool", "config": {
            "tool": "manor", "args": {"skill_id": "{{skill_id}}"}}, "next": []},
    ]


async def account(db):
    entity_id, user_id = generate_ulid(), generate_ulid()
    db.add_all([
        Entity(id=entity_id, name="Workflow skill account",
            settings={"approval_defaults": account_defaults()}),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.com",
            password_hash="test-only", role="owner", status="active"),
        UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
    ])
    await db.flush()
    await initialize_personal_approval(db, user_id=user_id, entity_id=entity_id)
    return entity_id


async def seed(db, *, skill_entity=None):
    entity_id = await account(db)
    ws = Workspace(id=generate_ulid(), entity_id=entity_id, name="Marketing", kind="campaign")
    skill = Skill(id=generate_ulid(), entity_id=skill_entity or entity_id,
                  slug="marketing-guide", name="Marketing guide", system_prompt="Use factual product claims.",
                  is_public=False, status="active", tools=[])
    wf = WorkflowDefinition(id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
                            name="campaign-video", steps=_blueprint_workflow_definition_values({"slug": "campaign-video", "steps": steps(skill.id)})["steps"], is_active=True)
    db.add_all([ws, skill, wf])
    await db.flush()
    await initialize_workspace_approval(db, ws)
    return ws, skill


@pytest.mark.parametrize("mode", [InstallMode.LIVE, InstallMode.SIMULATE])
async def test_workflow_only_skill_installs_with_new_identity_and_roundtrips(db_session, mode):
    source, skill = await seed(db_session)
    payload = await export_workspace(db_session, source.id, title=source.name, context=ExportContext(
        include_embedded_agents=False, include_embedded_skills=False,
    ))
    assert [s["slug"] for s in payload["embedded"]["skills"]] == [skill.slug]
    assert skill.id not in json.dumps(payload)
    portable = payload["recipe"]["workflows"][0]["steps"]
    args = portable[1]["config"]["args"]
    assert args == {"skill_refs": [{"field": "skill", "target": {"skill_component_key": skill.slug}}]}
    assert portable[2]["config"]["args"]["skill_id"] == "{{skill_id}}"
    customer = await account(db_session)
    installed = await install_blueprint(db_session, entity_id=customer, payload=payload, mode=mode)
    target_skill = await db_session.scalar(select(Skill).where(Skill.workspace_id == installed.workspace_id))
    assert target_skill.id != skill.id and target_skill.entity_id == customer
    wf = await db_session.scalar(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == installed.workspace_id))
    _, skill_ids, _ = reusable_resource_ids_from_payload(wf.steps)
    assert skill_ids == {target_skill.id}
    assert not WorkflowSkillDependencyFactory.has_portable_refs(wf.steps)
    assert wf.steps[1]["config"]["args"]["skill"] == target_skill.id
    assert wf.steps[1]["chat_projection"] == "support"
    assert "chat_projection" not in wf.steps[1]["config"]
    assert wf.steps[1]["config"]["steps"][0]["config"]["args"]["skill_id"] == target_skill.id
    again = await export_workspace(db_session, installed.workspace_id, title=source.name)
    assert again["recipe"]["workflows"][0]["steps"] == portable


async def test_export_rejects_foreign_workflow_skill(db_session):
    source, _skill = await seed(db_session, skill_entity=generate_ulid())
    with pytest.raises(ExportError, match="inaccessible"):
        await export_workspace(db_session, source.id, title=source.name)


async def test_missing_embedded_ref_does_not_adopt_same_slug_skill(db_session):
    source, skill = await seed(db_session)
    payload = await export_workspace(db_session, source.id, title=source.name)
    payload["embedded"]["skills"] = []
    customer = await account(db_session)
    db_session.add(Skill(id=generate_ulid(), entity_id=customer, slug=skill.slug,
                         name="Unrelated", system_prompt="Unrelated", is_public=False, status="active"))
    await db_session.flush()
    with pytest.raises(InstallError, match="missing from this installed Workspace"):
        await install_blueprint(db_session, entity_id=customer, payload=payload, mode=InstallMode.LIVE)


async def test_exact_platform_ref_wins_over_embedded_slug(db_session):
    source, private = await seed(db_session)
    public = Skill(id=generate_ulid(), entity_id=None, slug=private.slug, name="Platform guide",
                   system_prompt="Public exact content", is_public=True, status="active", tools=[])
    wf = WorkflowDefinition(id=generate_ulid(), entity_id=source.entity_id, workspace_id=source.id,
                            name="public-guide", steps=steps(public.id), is_active=True)
    db_session.add_all([public, wf])
    await db_session.flush()
    payload = await export_workspace(db_session, source.id, title=source.name)
    assert any(s.get("marketplace_id") == public.id for s in payload["contract"]["requires"]["skills"])
    customer = await account(db_session)
    installed = await install_blueprint(db_session, entity_id=customer, payload=payload, mode=InstallMode.LIVE)
    workflows = (await db_session.scalars(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == installed.workspace_id))).all()
    ids = {wf.name: wf.steps[1]["config"]["args"]["skill"] for wf in workflows}
    assert ids["campaign-video"] != ids["public-guide"]
    sk = await db_session.get(Skill, ids["public-guide"])
    assert sk.entity_id == customer and sk.config["source_skill_id"] == public.id


async def test_upgrade_preview_apply_and_revert_keep_customer_skill_identity(db_session):
    from packages.core.blueprints.upgrade import plan, apply, revert

    source, original_skill = await seed(db_session)
    payload = await export_workspace(db_session, source.id, title=source.name)
    installed = await install_blueprint(db_session, entity_id=await account(db_session), payload=payload,
        mode=InstallMode.LIVE, blueprint_id="builtin:workflow-skill-ref", blueprint_slug="workflow-skill-ref")
    ws = await db_session.get(Workspace, installed.workspace_id)
    wf = await db_session.scalar(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == ws.id))
    target_id = wf.steps[1]["config"]["args"]["skill"]
    before = copy.deepcopy(payload)
    preview = await plan(db_session, workspace=ws, payload=payload)
    assert next(item for item in preview["items"] if item["kind"] == "workflow")["action"] == "unchanged"
    assert payload == before
    updated = copy.deepcopy(payload)
    updated["recipe"]["workflows"][0]["steps"][1]["config"]["args"]["extra"] = "v2"
    preview = await plan(db_session, workspace=ws, payload=updated)
    assert next(item for item in preview["items"] if item["kind"] == "workflow")["action"] == "update"
    await apply(db_session, workspace=ws, payload=updated, current_version="1.0.1")
    assert wf.steps[1]["config"]["args"]["extra"] == "v2"
    assert wf.steps[1]["config"]["args"]["skill"] == target_id != original_skill.id
    assert not WorkflowSkillDependencyFactory.has_portable_refs(wf.steps)
    await revert(db_session, workspace=ws)
    assert "extra" not in wf.steps[1]["config"]["args"]
    assert wf.steps[1]["config"]["args"]["skill"] == target_id


async def test_excluded_workflows_do_not_require_their_skill_dependencies(db_session):
    source, _skill = await seed(db_session, skill_entity=generate_ulid())
    payload = await export_workspace(db_session, source.id, title=source.name,
        context=ExportContext(include_workflows=False))
    assert payload["recipe"]["workflows"] == []
    assert payload["embedded"]["skills"] == []
