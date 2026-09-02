"""Upgrading an installed workspace, and undoing it.

The faceless-stickman workspace ran a 636-character stand-in for its video
skill for five days after the real 4664-character procedure had shipped in
the blueprint. Re-installing would not have fixed it: the installer, meeting
a skill that already exists, reconciles only tools and status and leaves
system_prompt alone — at install time it cannot tell a workspace's own
wording from a stale copy.

An upgrade can tell, because ``revision`` already answers it. It moves only
when a behaviour-affecting field actually changes, and operator edits go
through skill_service, which bumps it:

    revision == 1  →  installed, never behaviourally edited  →  safe to update
    revision > 1   →  the workspace made this its own        →  never touched

Everything here is about that line holding, and about being able to step
back over an upgrade that turned out wrong.
"""
from __future__ import annotations

import copy
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    BlueprintFreshness,
    blueprint_content_fingerprint,
    blueprint_freshness,
    blueprint_upgrade_unsupported_fingerprint,
)
from packages.core.blueprints.solo_company import get_solo_company_blueprint
from packages.core.blueprints.upgrade import (
    BlueprintUpgradeAccessDeniedError,
    BlueprintUpgradeIncompleteError,
    BlueprintUpgradePlanChangedError,
    RESTORE_POINT_KEY,
    UpgradeAction,
    apply,
    plan,
    revert,
)
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Document, DocumentGroup, DocumentGroupMember
from packages.core.models.skill import Skill
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import AgentSubscription, Workspace

SLUG = "solo-faceless-stickman-studio-v1"
STALE_PROMPT = "You are a professional AI video producer specialising in stickman videos."


def test_upgrade_api_requires_the_reviewed_protocol_payload():
    from apps.api.routers.workspaces import (
        BLUEPRINT_UPGRADE_PROTOCOL_VERSION,
        apply_blueprint_upgrade,
    )

    assert BLUEPRINT_UPGRADE_PROTOCOL_VERSION == 2
    assert (
        inspect.signature(apply_blueprint_upgrade).parameters["req"].default
        is inspect.Parameter.empty
    )


def test_workflow_reconcile_locks_mapping_and_definition_before_authorizing():
    from packages.core.blueprints.installer import _install_workflow

    body = inspect.getsource(_install_workflow)
    before_authorization = body.split("await authorize_existing_update", 1)[0]
    assert before_authorization.count(".with_for_update()") >= 2


@pytest.fixture
def payload():
    return get_solo_company_blueprint(SLUG)


async def _workspace(db_session, entity_id, *, installed_from):
    """A workspace installed from an older version of the blueprint."""
    ws = Workspace(
        entity_id=entity_id,
        name="Faceless Stickman Video Studio",
        settings={
            BLUEPRINT_SETTINGS_KEY: {
                # Identity is the id; the slug rides along as a display name.
                BLUEPRINT_ID_KEY: f"builtin:{SLUG}",
                "blueprint_slug": SLUG,
                "installed_at": "2026-07-22T23:24:48Z",
                CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(installed_from),
                UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
                    blueprint_upgrade_unsupported_fingerprint(installed_from)
                ),
            }
        },
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


async def test_unsupported_configuration_allows_safe_partial_component_upgrade(
    db_session,
    scenario,
    payload,
):
    workspace = scenario["workspace"]
    settings = copy.deepcopy(workspace.settings)
    settings[BLUEPRINT_SETTINGS_KEY][BLUEPRINT_VERSION_KEY] = "1.0.0"
    workspace.settings = settings
    await db_session.flush()

    current = copy.deepcopy(payload)
    current["embedded"]["agents"] = []
    current["embedded"]["knowledge_packs"] = []
    current["recipe"]["workflows"] = []
    current["policy"]["governance"]["max_risk_level"] = "low"

    upgrade_plan = await plan(
        db_session,
        workspace=workspace,
        payload=current,
    )

    assert upgrade_plan["unsupported_changes"] is True
    blocker = next(
        item for item in upgrade_plan["items"]
        if item["kind"] == "blueprint_configuration"
    )
    assert blocker["action"] == UpgradeAction.RECONFIGURE.value
    assert "reinstall/reconfigure" in blocker["changes"][0]

    result = await apply(
        db_session,
        workspace=workspace,
        payload=current,
        current_version="1.0.1",
        require_complete_workspace=True,
    )

    persisted = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert result["fully_synchronized"] is False
    assert scenario["skill"].system_prompt == current["embedded"]["skills"][0]["system_prompt"]
    assert persisted[BLUEPRINT_VERSION_KEY] == "1.0.0"
    assert persisted[CONTENT_FINGERPRINT_KEY] == blueprint_content_fingerprint(
        scenario["older"]
    )


async def test_legacy_unknown_baseline_allows_safe_partial_upgrade(
    db_session,
    scenario,
    payload,
):
    workspace = scenario["workspace"]
    settings = copy.deepcopy(workspace.settings)
    record = settings[BLUEPRINT_SETTINGS_KEY]
    record.pop(UPGRADE_UNSUPPORTED_FINGERPRINT_KEY)
    record[BLUEPRINT_VERSION_KEY] = "1.0.0"
    workspace.settings = settings
    await db_session.flush()

    current = copy.deepcopy(payload)
    current["embedded"]["agents"] = []
    current["embedded"]["knowledge_packs"] = []
    current["recipe"]["workflows"] = []
    preview = await plan(db_session, workspace=workspace, payload=current)

    assert preview["unsupported_changes"] is True
    baseline = next(
        item for item in preview["items"]
        if item["kind"] == "blueprint_configuration"
    )
    assert baseline["action"] == UpgradeAction.BASELINE_UNKNOWN.value

    result = await apply(
        db_session,
        workspace=workspace,
        payload=current,
        current_version="1.0.1",
        require_complete_workspace=True,
    )

    persisted = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert scenario["skill"].system_prompt == current["embedded"]["skills"][0]["system_prompt"]
    assert result["fully_synchronized"] is False
    assert persisted[CONTENT_FINGERPRINT_KEY] == blueprint_content_fingerprint(
        scenario["older"]
    )
    assert persisted[BLUEPRINT_VERSION_KEY] == "1.0.0"
    assert UPGRADE_UNSUPPORTED_FINGERPRINT_KEY not in persisted


async def test_legacy_matching_content_backfills_upgrade_baseline(
    db_session,
):
    entity_id = "01TESTENTITY0000000000000V"
    payload = {
        "manifest": {"blueprint_version": "1.1"},
        "contract": {},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"workflows": []},
        "policy": {},
    }
    workspace = await _workspace(db_session, entity_id, installed_from=payload)
    settings = copy.deepcopy(workspace.settings)
    record = settings[BLUEPRINT_SETTINGS_KEY]
    record.pop(UPGRADE_UNSUPPORTED_FINGERPRINT_KEY)
    record[BLUEPRINT_VERSION_KEY] = "1.0.0"
    workspace.settings = settings
    await db_session.flush()

    preview = await plan(db_session, workspace=workspace, payload=payload)
    assert preview["unsupported_changes"] is False

    result = await apply(
        db_session,
        workspace=workspace,
        payload=payload,
        current_version="1.0.1",
        require_complete_workspace=True,
    )

    persisted = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert result["fully_synchronized"] is True
    assert persisted[BLUEPRINT_VERSION_KEY] == "1.0.1"
    assert persisted[UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] == (
        blueprint_upgrade_unsupported_fingerprint(payload)
    )


async def test_upgrade_selects_and_binds_new_channel_requirement(
    db_session,
):
    entity_id = "01TESTENTITY0000000000000C"
    user_id = generate_ulid()
    older = {
        "manifest": {"blueprint_version": "1.1", "name": "Channel Upgrade"},
        "contract": {},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"workflows": []},
        "policy": {},
    }
    current = copy.deepcopy(older)
    current["contract"] = {
        "channels": [{
            "channel_type": "telegram",
            "provider": "telegram_bot",
            "purpose": "Publish launch alerts",
            "required": True,
        }],
    }
    workspace = await _workspace(db_session, entity_id, installed_from=older)
    subscription = AgentSubscription(
        entity_id=entity_id,
        workspace_id=workspace.id,
        agent_id=generate_ulid(),
        service_key="launch-agent",
        name="Launch Agent",
        status="active",
        config={},
    )
    account = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=user_id,
        channel_type="telegram",
        provider="telegram_bot",
        name="Launch bot",
        status="active",
        config={},
        credentials={},
    )
    db_session.add_all([subscription, account])
    await db_session.flush()

    preview = await plan(db_session, workspace=workspace, payload=current)
    assert preview["unsupported_changes"] is False

    result = await apply(
        db_session,
        workspace=workspace,
        payload=current,
        by_user_id=user_id,
        current_version="1.0.1",
        expected_blueprint_fingerprint=blueprint_content_fingerprint(current),
        channel_config_ids={"channel:0:telegram": account.id},
        require_complete_workspace=True,
    )

    assert result["fully_synchronized"] is True
    binding = (await db_session.execute(
        select(Channel).where(Channel.workspace_id == workspace.id)
    )).scalar_one()
    assert binding.config["channel_config_id"] == account.id
    assert binding.config["purpose"] == "Publish launch alerts"
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][CONTENT_FINGERPRINT_KEY] == (
        blueprint_content_fingerprint(current)
    )


async def _skill(db_session, entity_id, spec, *, prompt, revision=1):
    """A skill as the installer would have written it from ``spec``.

    Everything but the prompt matches, because that is what the older
    blueprint actually carried — only the system prompt was later rewritten.
    """
    row = Skill(
        entity_id=entity_id,
        name=spec.get("name") or spec["slug"],
        slug=spec["slug"],
        system_prompt=prompt,
        tools=list(spec.get("tools") or []),
        input_schema=dict(spec.get("input_schema") or {}),
        output_format=spec.get("output_format") or "text",
        config=dict(spec.get("config") or {}),
        revision=revision,
        status="active",
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def scenario(db_session, payload):
    """The production shape: blueprint corrected, workspace still on the stub."""
    from packages.core.blueprints.installer import (
        _blueprint_workflow_definition_values,
    )

    entity_id = "01TESTENTITY0000000000000A"
    older = copy.deepcopy(payload)
    older["embedded"]["skills"][0]["system_prompt"] = STALE_PROMPT

    ws = await _workspace(db_session, entity_id, installed_from=older)
    skill = await _skill(
        db_session, entity_id, older["embedded"]["skills"][0], prompt=STALE_PROMPT,
    )
    workflow_spec = payload["recipe"]["workflows"][0]
    definition = WorkflowDefinition(
        entity_id=entity_id,
        revision=1,
        **_blueprint_workflow_definition_values(workflow_spec),
    )
    db_session.add(definition)
    await db_session.flush()
    db_session.add(WorkflowTemplateInstallation(
        entity_id=entity_id,
        template_id=f"builtin:{SLUG}",
        component_key=workflow_spec["slug"],
        workflow_id=definition.id,
        installed_version="1.0.0",
        source_type="workspace_blueprint",
        installation_metadata={"source_workflow_key": workflow_spec["slug"]},
    ))
    db_session.add(WorkflowBinding(
        entity_id=entity_id,
        workspace_id=ws.id,
        workflow_id=definition.id,
        name=workflow_spec["name"],
        trigger_type=workflow_spec["trigger_type"],
        config={
            "source": "blueprint",
            "source_template_id": f"builtin:{SLUG}",
            "workspace_blueprint_workflow_slug": workflow_spec["slug"],
        },
        enabled=True,
        status="active",
    ))
    await db_session.flush()
    return {"entity_id": entity_id, "workspace": ws, "skill": skill, "older": older}


async def test_upgrade_materializes_variables_but_reviews_raw_source_fingerprint(
    db_session,
    scenario,
    payload,
):
    from packages.core.blueprints.installer import resolve_install_variables

    source = copy.deepcopy(payload)
    source["contract"]["variables"] = [{
        "key": "brand_name",
        "label": "Brand name",
        "required": True,
        "materialize": True,
    }]
    source["embedded"]["skills"][0]["system_prompt"] = (
        "Create every deliverable for {{brand_name}}."
    )
    materialized, personalization = resolve_install_variables(
        source,
        {"brand_name": "Acme"},
    )

    preview = await plan(
        db_session,
        workspace=scenario["workspace"],
        payload=materialized,
        source_payload=source,
    )
    skill_item = next(
        item for item in preview["items"]
        if item["kind"] == "skill" and item["slug"] == source["embedded"]["skills"][0]["slug"]
    )

    assert preview["blueprint_fingerprint"] == blueprint_content_fingerprint(source)
    assert preview["blueprint_fingerprint"] != blueprint_content_fingerprint(materialized)
    assert "Acme" in skill_item["new_content"]["system_prompt"]
    assert "{{brand_name}}" not in skill_item["new_content"]["system_prompt"]

    await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=materialized,
        source_payload=source,
        personalization=personalization,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
    )

    assert "Acme" in scenario["skill"].system_prompt
    assert scenario["workspace"].settings["blueprint_personalization"] == {
        "brand_name": "Acme",
    }
    assert "{{brand_name}}" in source["embedded"]["skills"][0]["system_prompt"]


def test_upgrade_variable_inputs_reuse_only_declared_saved_values(payload):
    from apps.api.routers.workspaces import _blueprint_upgrade_variable_inputs

    source = copy.deepcopy(payload)
    source["contract"]["variables"] = [
        {
            "key": "brand_name",
            "label": "Brand name",
            "required": True,
            "materialize": True,
        },
        {
            "key": "region",
            "label": "Region",
            "required": False,
            "default": "US",
            "materialize": True,
        },
    ]
    workspace = SimpleNamespace(settings={
        "blueprint_personalization": {
            "brand_name": "Acme",
            "removed_variable": "must not survive",
        },
    })

    values, resolved_keys = _blueprint_upgrade_variable_inputs(
        workspace=workspace,
        payload=source,
        supplied={},
    )

    assert values == {"brand_name": "Acme"}
    assert resolved_keys == ["brand_name", "region"]


async def test_conflict_resolution_applies_materialized_blueprint_content(
    db_session,
    scenario,
    payload,
):
    from packages.core.blueprints.installer import resolve_install_variables

    source = copy.deepcopy(payload)
    source["contract"]["variables"] = [{
        "key": "brand_name",
        "label": "Brand name",
        "required": True,
        "materialize": True,
    }]
    source["embedded"]["skills"][0]["system_prompt"] = (
        "Create every deliverable for {{brand_name}}."
    )
    materialized, personalization = resolve_install_variables(
        source,
        {"brand_name": "Acme"},
    )
    scenario["skill"].revision = 4
    await db_session.flush()

    preview = await plan(
        db_session,
        workspace=scenario["workspace"],
        payload=materialized,
        source_payload=source,
    )
    conflict = next(
        item for item in preview["items"]
        if item["kind"] == "skill"
        and item["slug"] == source["embedded"]["skills"][0]["slug"]
    )
    assert conflict["action"] == UpgradeAction.KEEP_YOURS.value

    await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=materialized,
        source_payload=source,
        personalization=personalization,
        conflict_resolutions=[{
            "kind": "skill",
            "slug": conflict["slug"],
            "resolution": "use_blueprint",
            "expected_revision": conflict["revision"],
        }],
    )

    assert scenario["skill"].system_prompt == "Create every deliverable for Acme."
    assert "{{brand_name}}" not in scenario["skill"].system_prompt


@pytest.mark.parametrize("legacy_fingerprint", [False, True])
async def test_revert_restores_personalization_and_creator_variable_schema(
    db_session,
    scenario,
    payload,
    legacy_fingerprint,
):
    from packages.core.blueprints.installer import resolve_install_variables

    old_source = copy.deepcopy(payload)
    old_declarations = [{
        "key": "brand_name",
        "label": "Brand",
        "required": True,
        "materialize": True,
    }]
    old_source["contract"]["variables"] = old_declarations
    old_source["embedded"]["skills"][0]["system_prompt"] = (
        "Use the old playbook for {{brand_name}}."
    )
    scenario["skill"].system_prompt = "Use the old playbook for OldCo."
    workspace = scenario["workspace"]
    settings = copy.deepcopy(workspace.settings)
    settings["blueprint_personalization"] = {"brand_name": "OldCo"}
    record = settings[BLUEPRINT_SETTINGS_KEY]
    record[CONTENT_FINGERPRINT_KEY] = blueprint_content_fingerprint(old_source)
    record[UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = (
        blueprint_upgrade_unsupported_fingerprint(old_source, include_channels=legacy_fingerprint)
    )
    record["variable_declarations"] = copy.deepcopy(old_declarations)
    workspace.settings = settings
    await db_session.flush()

    source = copy.deepcopy(old_source)
    new_declarations = [{
        **old_declarations[0],
        "label": "Company brand",
        "purpose": "Personalize every generated deliverable.",
    }]
    source["contract"]["variables"] = new_declarations
    source["embedded"]["skills"][0]["system_prompt"] = (
        "Use the new playbook for {{brand_name}}."
    )
    materialized, personalization = resolve_install_variables(
        source,
        {"brand_name": "Acme"},
    )

    preview = await plan(
        db_session,
        workspace=workspace,
        payload=materialized,
        source_payload=source,
    )
    assert preview["unsupported_changes"] is False

    result = await apply(
        db_session,
        workspace=workspace,
        payload=materialized,
        source_payload=source,
        personalization=personalization,
        current_version="1.0.1",
    )
    assert result["fully_synchronized"] is True
    assert result["can_revert"] is True
    assert scenario["skill"].system_prompt == "Use the new playbook for Acme."
    assert workspace.settings["blueprint_personalization"] == {
        "brand_name": "Acme",
    }
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
        "variable_declarations"
    ] == new_declarations

    await revert(db_session, workspace=workspace)

    assert scenario["skill"].system_prompt == "Use the old playbook for OldCo."
    assert workspace.settings["blueprint_personalization"] == {
        "brand_name": "OldCo",
    }
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
        "variable_declarations"
    ] == old_declarations
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
        UPGRADE_UNSUPPORTED_FINGERPRINT_KEY
    ] == blueprint_upgrade_unsupported_fingerprint(old_source, include_channels=legacy_fingerprint)


async def test_settings_only_personalization_upgrade_can_be_reverted(db_session):
    from packages.core.blueprints.installer import resolve_install_variables

    old_declarations = [{
        "key": "brand_name",
        "label": "Brand",
        "required": True,
        "materialize": True,
    }]
    old_source = {
        "manifest": {"blueprint_version": "1.1"},
        "contract": {"variables": old_declarations},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"workflows": []},
        "policy": {},
    }
    workspace = await _workspace(
        db_session,
        "01TESTENTITY0000000000000Y",
        installed_from=old_source,
    )
    settings = copy.deepcopy(workspace.settings)
    settings["blueprint_personalization"] = {"brand_name": "OldCo"}
    settings[BLUEPRINT_SETTINGS_KEY]["variable_declarations"] = copy.deepcopy(
        old_declarations
    )
    workspace.settings = settings
    await db_session.flush()

    source = copy.deepcopy(old_source)
    new_declarations = [{**old_declarations[0], "label": "Company brand"}]
    source["contract"]["variables"] = new_declarations
    materialized, personalization = resolve_install_variables(
        source,
        {"brand_name": "Acme"},
    )

    result = await apply(
        db_session,
        workspace=workspace,
        payload=materialized,
        source_payload=source,
        personalization=personalization,
        current_version="1.0.1",
    )

    assert result["updated"] == []
    assert result["can_revert"] is True
    assert workspace.settings["blueprint_personalization"] == {
        "brand_name": "Acme",
    }
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
        "variable_declarations"
    ] == new_declarations

    reverted = await revert(db_session, workspace=workspace)

    assert reverted["reverted"] == []
    assert workspace.settings["blueprint_personalization"] == {
        "brand_name": "OldCo",
    }
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
        "variable_declarations"
    ] == old_declarations


async def test_personalization_drift_outside_upgrade_scope_stays_partial(
    db_session,
):
    from packages.core.blueprints.installer import resolve_install_variables

    declarations = [{
        "key": "brand_name",
        "required": True,
        "materialize": True,
    }]
    source = {
        "manifest": {"blueprint_version": "1.1"},
        "contract": {"variables": declarations},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {"context": "Operate {{brand_name}}."},
            "workflows": [],
        },
        "policy": {},
    }
    installed_payload, _ = resolve_install_variables(
        source,
        {"brand_name": "Acme"},
    )
    workspace = await _workspace(
        db_session,
        "01TESTENTITY0000000000000X",
        installed_from=source,
    )
    workspace.operating_context = "Operate Acme."
    settings = copy.deepcopy(workspace.settings)
    settings["blueprint_personalization"] = {"brand_name": "Acme"}
    settings[BLUEPRINT_SETTINGS_KEY]["variable_declarations"] = copy.deepcopy(
        declarations
    )
    settings[BLUEPRINT_SETTINGS_KEY][
        MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY
    ] = blueprint_upgrade_unsupported_fingerprint(installed_payload)
    workspace.settings = settings
    await db_session.flush()

    current_payload, personalization = resolve_install_variables(
        source,
        {"brand_name": "Beta"},
    )
    preview = await plan(
        db_session,
        workspace=workspace,
        payload=current_payload,
        source_payload=source,
    )

    assert preview["unsupported_changes"] is True
    assert preview["unsupported_baseline_unknown"] is False
    assert preview["items"][0]["action"] == UpgradeAction.RECONFIGURE.value

    result = await apply(
        db_session,
        workspace=workspace,
        payload=current_payload,
        source_payload=source,
        personalization=personalization,
        current_version="1.0.1",
    )

    assert result["fully_synchronized"] is False
    assert workspace.operating_context == "Operate Acme."
    assert workspace.settings["blueprint_personalization"] == {
        "brand_name": "Beta",
    }
    assert workspace.settings[BLUEPRINT_SETTINGS_KEY][
        MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY
    ] == blueprint_upgrade_unsupported_fingerprint(installed_payload)


@pytest.fixture
def opc_payload():
    return get_solo_company_blueprint("solo-content-distribution-studio-v1")


@pytest.fixture
async def workflow_scenario(db_session, opc_payload):
    """An installed Article Flow from before approved-result prefilling."""
    from packages.core.blueprints.installer import (
        _blueprint_workflow_definition_values,
    )

    entity_id = "01TESTENTITY0000000000000F"
    workspace = Workspace(
        entity_id=entity_id,
        name="OPC Content Studio",
        settings={
            BLUEPRINT_SETTINGS_KEY: {
                BLUEPRINT_ID_KEY: "builtin:solo-content-distribution-studio-v1",
                "blueprint_slug": "solo-content-distribution-studio-v1",
            }
        },
    )
    db_session.add(workspace)
    await db_session.flush()

    current_spec = next(
        item for item in opc_payload["recipe"]["workflows"]
        if item["slug"] == "opc-write-article-from-topic-v1"
    )
    older_spec = copy.deepcopy(current_spec)
    older_spec["run_inputs"][0].pop("hidden", None)
    topic_input = next(
        item for item in older_spec["run_inputs"] if item["key"] == "topic_brief"
    )
    topic_input.pop("schema", None)
    topic_input.pop("prefill", None)
    values = _blueprint_workflow_definition_values(older_spec)
    definition = WorkflowDefinition(entity_id=entity_id, revision=1, **values)
    db_session.add(definition)
    await db_session.flush()
    installation = WorkflowTemplateInstallation(
        entity_id=entity_id,
        template_id="builtin:solo-content-distribution-studio-v1",
        component_key=current_spec["slug"],
        workflow_id=definition.id,
        installed_version="1.0.0",
        source_type="workspace_blueprint",
        installation_metadata={"source_workflow_key": current_spec["slug"]},
    )
    db_session.add(installation)
    binding = WorkflowBinding(
        entity_id=entity_id,
        workspace_id=workspace.id,
        workflow_id=definition.id,
        name=current_spec["name"],
        trigger_type="mcp",
        config={
            "source": "blueprint",
            "source_template_id": "builtin:solo-content-distribution-studio-v1",
            "workspace_blueprint_workflow_slug": current_spec["slug"],
        },
        enabled=True,
        status="active",
    )
    db_session.add(binding)
    await db_session.flush()
    return {
        "workspace": workspace,
        "definition": definition,
        "older_steps": copy.deepcopy(definition.steps),
        "spec": current_spec,
        "installation": installation,
    }


@pytest.mark.asyncio
async def test_an_archived_exact_blueprint_is_not_an_upgrade_release(
    db_session, opc_payload,
):
    """Archived edits cannot leak by redirecting to a same-slug release."""
    from apps.api.routers.workspaces import _blueprint_payloads_for

    slug = "solo-content-distribution-studio-v1"
    stable_id = f"builtin:{slug}"
    row = WorkspaceBlueprint(
        id=stable_id,
        entity_id=None,
        slug=slug,
        title="OPC Content Studio and Distribution",
        payload=opc_payload,
        content_version="1.0.7",
        status="published",
    )
    obsolete = WorkspaceBlueprint(
        id="01DELETEDBLUEPRINT00000001",
        entity_id="01TESTENTITY0000000000000G",
        slug=slug,
        title="Old duplicate OPC Blueprint",
        payload={"manifest": {"slug": slug}, "recipe": {}},
        content_version="1.0.0",
        status="archived",
    )
    workspace = Workspace(
        entity_id="01TESTENTITY0000000000000G",
        name="Legacy duplicate install",
        settings={
            BLUEPRINT_SETTINGS_KEY: {
                BLUEPRINT_ID_KEY: "01DELETEDBLUEPRINT00000001",
                "blueprint_slug": slug,
            }
        },
    )
    db_session.add_all([row, obsolete, workspace])
    await db_session.flush()

    resolved = await _blueprint_payloads_for(db_session, [workspace])

    assert resolved == {}


@pytest.mark.asyncio
async def test_a_live_exact_blueprint_id_wins_over_a_matching_platform_slug(
    db_session, opc_payload,
):
    """A custom Blueprint may share a display slug; its durable id is identity."""
    from apps.api.routers.workspaces import _blueprint_payloads_for

    entity_id = "01TESTENTITY0000000000000G"
    slug = "solo-content-distribution-studio-v1"
    custom_id = "01CUSTOMBLUEPRINT000000001"
    custom_payload = copy.deepcopy(opc_payload)
    custom_payload["manifest"]["name"] = "Operator-owned Content Studio"
    platform = WorkspaceBlueprint(
        id=f"builtin:{slug}",
        entity_id=None,
        slug=slug,
        title="Official Content Studio",
        payload=opc_payload,
        content_version="1.0.7",
        status="published",
    )
    custom = WorkspaceBlueprint(
        id=custom_id,
        entity_id=entity_id,
        slug=slug,
        title="Operator-owned Content Studio",
        payload=custom_payload,
        content_version="2.0.0",
        status="published",
    )
    workspace = Workspace(
        entity_id=entity_id,
        name="Custom Blueprint install",
        settings={
            BLUEPRINT_SETTINGS_KEY: {
                BLUEPRINT_ID_KEY: custom_id,
                "blueprint_slug": slug,
            }
        },
    )
    db_session.add_all([platform, custom, workspace])
    await db_session.flush()

    resolved = await _blueprint_payloads_for(db_session, [workspace])

    assert resolved[workspace.id] == (custom_payload, "2.0.0", custom_id)


@pytest.mark.asyncio
async def test_a_legacy_slug_only_install_gets_an_upgrade_plan(
    db_session, workflow_scenario, opc_payload,
):
    """The API resolves the payload from the legacy slug and repairs its id
    on apply; preview must not discard that already-resolved payload."""
    workspace = workflow_scenario["workspace"]
    settings = copy.deepcopy(workspace.settings)
    settings[BLUEPRINT_SETTINGS_KEY].pop(BLUEPRINT_ID_KEY)
    workspace.settings = settings
    await db_session.flush()

    result = await plan(
        db_session,
        workspace=workspace,
        payload=opc_payload,
        source_blueprint_id="builtin:solo-content-distribution-studio-v1",
    )

    article = next(
        item for item in result["items"]
        if item["slug"] == "opc-write-article-from-topic-v1"
    )
    assert article["action"] == UpgradeAction.UPDATE.value


@pytest.mark.asyncio
async def test_an_installed_internal_workflow_is_not_reported_missing(
    db_session, workflow_scenario, opc_payload,
):
    from packages.core.blueprints.installer import (
        _blueprint_workflow_definition_values,
    )

    internal_spec = next(
        item for item in opc_payload["recipe"]["workflows"]
        if item.get("internal")
    )
    internal = WorkflowDefinition(
        entity_id=workflow_scenario["workspace"].entity_id,
        revision=1,
        **_blueprint_workflow_definition_values(internal_spec),
    )
    db_session.add(internal)
    await db_session.flush()
    db_session.add(WorkflowTemplateInstallation(
        entity_id=workflow_scenario["workspace"].entity_id,
        template_id="builtin:solo-content-distribution-studio-v1",
        component_key=internal_spec["slug"],
        workflow_id=internal.id,
        installed_version="1.0.0",
        source_type="workspace_blueprint",
    ))
    await db_session.flush()

    result = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )

    item = next(
        candidate for candidate in result["items"]
        if candidate["slug"] == internal_spec["slug"]
    )
    assert item["action"] == UpgradeAction.UNCHANGED.value


@pytest.mark.asyncio
async def test_upgrade_rekeys_skill_by_portable_component_key(db_session):
    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_INSTALLED_COMPONENT,
        RESOURCE_SKILL,
        RESOURCE_WORKSPACE_BLUEPRINT,
        SCOPE_WORKSPACE,
        marketplace_link_for_local_resource,
        record_marketplace_resource_link,
    )

    entity_id = "01TESTENTITY0000000000000H"
    old_source_id = "builtin:component-key-regression"
    canonical_source_id = "01CANONICALCOMPONENTKEY0001"
    component_key = "portable-skill-component"
    spec = {
        "component_id": component_key,
        "slug": "portable-skill-slug",
        "name": "Portable Skill",
        "system_prompt": "Keep this exact portable procedure.",
        "tools": [],
        "input_schema": {},
        "output_format": "text",
        "config": {},
    }
    payload = {
        "manifest": {"blueprint_version": "1.1"},
        "contract": {},
        "embedded": {
            "skills": [spec],
            "agents": [],
            "knowledge_packs": [],
        },
        "recipe": {"workflows": []},
        "policy": {},
    }
    workspace = Workspace(
        entity_id=entity_id,
        name="Portable component key Workspace",
        settings={
            BLUEPRINT_SETTINGS_KEY: {
                BLUEPRINT_ID_KEY: old_source_id,
                "blueprint_slug": "component-key-regression",
            }
        },
    )
    db_session.add(workspace)
    await db_session.flush()
    skill = await _skill(
        db_session,
        entity_id,
        spec,
        prompt=spec["system_prompt"],
    )
    skill.workspace_id = workspace.id
    await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
        marketplace_resource_id=old_source_id,
        relationship=RELATIONSHIP_INSTALLED_COMPONENT,
        scope_type=SCOPE_WORKSPACE,
        scope_id=workspace.id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=skill.id,
        component_key=component_key,
    )

    preview = await plan(
        db_session,
        workspace=workspace,
        payload=payload,
        source_blueprint_id=canonical_source_id,
    )
    item = next(row for row in preview["items"] if row["kind"] == "skill")
    assert item["component_key"] == component_key
    assert item["id"] == skill.id

    await apply(
        db_session,
        workspace=workspace,
        payload=payload,
        current_version="1.0.1",
        source_blueprint_id=canonical_source_id,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
    )

    link = await marketplace_link_for_local_resource(
        db_session,
        entity_id=entity_id,
        local_resource_type=RESOURCE_SKILL,
        local_resource_id=skill.id,
        relationship=RELATIONSHIP_INSTALLED_COMPONENT,
    )
    assert link is not None
    assert link.marketplace_resource_id == canonical_source_id
    assert link.component_key == component_key
    assert link.link_metadata["source_slug"] == spec["slug"]


@pytest.mark.asyncio
async def test_upgrade_resolves_workflow_marketplace_component_key(
    db_session,
    workflow_scenario,
):
    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_INSTALLED_COMPONENT,
        RESOURCE_WORKFLOW,
        RESOURCE_WORKSPACE_BLUEPRINT,
        SCOPE_WORKSPACE,
        marketplace_link_for_local_resource,
        record_marketplace_resource_link,
    )

    old_source_id = "builtin:solo-content-distribution-studio-v1"
    canonical_source_id = "01CANONICALWORKFLOWKEY00001"
    component_key = "portable-workflow-component"
    workflow_scenario["definition"].workspace_id = workflow_scenario["workspace"].id
    spec = copy.deepcopy(workflow_scenario["spec"])
    spec["component_id"] = component_key
    payload = {
        "manifest": {"blueprint_version": "1.1"},
        "contract": {},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"workflows": [spec]},
        "policy": {},
    }
    await record_marketplace_resource_link(
        db_session,
        entity_id=workflow_scenario["workspace"].entity_id,
        marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
        marketplace_resource_id=old_source_id,
        relationship=RELATIONSHIP_INSTALLED_COMPONENT,
        scope_type=SCOPE_WORKSPACE,
        scope_id=workflow_scenario["workspace"].id,
        local_resource_type=RESOURCE_WORKFLOW,
        local_resource_id=workflow_scenario["definition"].id,
        component_key=component_key,
    )

    preview = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=payload,
        source_blueprint_id=canonical_source_id,
    )
    item = next(row for row in preview["items"] if row["kind"] == "workflow")
    assert item["component_key"] == component_key
    assert item["id"] == workflow_scenario["definition"].id

    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=payload,
        current_version="1.0.1",
        source_blueprint_id=canonical_source_id,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
    )
    link = await marketplace_link_for_local_resource(
        db_session,
        entity_id=workflow_scenario["workspace"].entity_id,
        local_resource_type=RESOURCE_WORKFLOW,
        local_resource_id=workflow_scenario["definition"].id,
        relationship=RELATIONSHIP_INSTALLED_COMPONENT,
    )
    assert link is not None
    assert link.marketplace_resource_id == canonical_source_id
    assert link.component_key == component_key
    await db_session.delete(workflow_scenario["installation"])
    await db_session.flush()

    second_preview = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=payload,
        source_blueprint_id=canonical_source_id,
    )
    second_item = next(
        row for row in second_preview["items"] if row["kind"] == "workflow"
    )
    assert second_item["id"] == workflow_scenario["definition"].id
    assert second_item["action"] != UpgradeAction.MISSING.value


@pytest.mark.asyncio
async def test_same_name_internal_workflow_without_source_mapping_is_missing(
    db_session, workflow_scenario, opc_payload,
):
    from packages.core.blueprints.installer import (
        _blueprint_workflow_definition_values,
    )

    internal_spec = next(
        item for item in opc_payload["recipe"]["workflows"]
        if item.get("internal")
    )
    db_session.add(WorkflowDefinition(
        entity_id=workflow_scenario["workspace"].entity_id,
        revision=1,
        **_blueprint_workflow_definition_values(internal_spec),
    ))
    await db_session.flush()

    result = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )

    item = next(
        candidate for candidate in result["items"]
        if candidate["slug"] == internal_spec["slug"]
    )
    assert item["action"] == UpgradeAction.MISSING.value


# ── The plan ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_plan_writes_nothing(db_session, scenario, payload):
    """It is what the operator confirms, so it must be safe to look at."""
    before = scenario["skill"].system_prompt
    await plan(db_session, workspace=scenario["workspace"], payload=payload)
    assert scenario["skill"].system_prompt == before


@pytest.mark.asyncio
async def test_an_untouched_item_is_offered_for_update(db_session, scenario, payload):
    result = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert item["action"] == UpgradeAction.UPDATE.value


@pytest.mark.asyncio
async def test_the_plan_says_what_changes_in_readable_terms(db_session, scenario, payload):
    """"system_prompt differs" is not something anyone can judge."""
    result = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert any("characters" in note for note in item["changes"]), item["changes"]


@pytest.mark.asyncio
async def test_an_edited_item_is_never_offered(db_session, scenario, payload):
    """revision > 1 means the workspace made it its own. Deciding that
    someone's edit is stale is not a decision code gets to make."""
    scenario["skill"].revision = 4
    await db_session.flush()

    result = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert item["action"] == UpgradeAction.KEEP_YOURS.value


@pytest.mark.asyncio
async def test_an_already_current_item_is_unchanged(db_session, scenario, payload):
    scenario["skill"].system_prompt = payload["embedded"]["skills"][0]["system_prompt"]
    await db_session.flush()

    result = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert item["action"] == UpgradeAction.UNCHANGED.value


@pytest.mark.asyncio
async def test_the_plan_carries_the_new_version_itself(db_session, scenario, payload):
    """"636 → 4664 characters" says how much changes, not what it now says.
    Someone approving an overwrite of the instructions their agents run
    should be able to read them first."""
    result = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)

    source_prompt = payload["embedded"]["skills"][0]["system_prompt"]
    new_prompt = item["new_content"]["system_prompt"]
    assert new_prompt.startswith(source_prompt[:80])
    # A marker well past the 80-char prefix checked above, so this only passes
    # if the full body made it through rather than a short preview. Sliced out
    # of the payload rather than hardcoded: this assertion has already been
    # broken twice by the blueprint rewording its own headings ("CRITICAL
    # FACTS", then "NONNEGOTIABLE PRODUCTION RULES"), which tested the
    # copywriting instead of the upgrade plan. What matters is that *some*
    # mid-prompt content survives into new_content, whatever it says.
    marker = source_prompt[1000:1200]
    assert len(marker) == 200, "fixture prompt is too short for the mid-prompt probe"
    assert marker in new_prompt, "the part that was missing must be visible"


@pytest.mark.asyncio
async def test_a_very_long_new_version_is_truncated(db_session, scenario, payload):
    """A plan across several workspaces must not become a payload problem."""
    from packages.core.blueprints.upgrade import PREVIEW_CHARS

    huge = copy.deepcopy(payload)
    huge["embedded"]["skills"][0]["system_prompt"] = "x" * (PREVIEW_CHARS * 3)

    result = await plan(db_session, workspace=scenario["workspace"], payload=huge)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert len(item["new_content"]["system_prompt"]) <= PREVIEW_CHARS + 1


@pytest.mark.asyncio
async def test_an_unchanged_item_offers_nothing_to_read(db_session, scenario, payload):
    scenario["skill"].system_prompt = payload["embedded"]["skills"][0]["system_prompt"]
    await db_session.flush()

    result = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert item["new_content"] == {}


def test_the_dialog_shows_the_new_version():
    """The content has to reach the person confirming, not just the API."""
    from pathlib import Path

    body = Path(
        "apps/web/src/components/blueprints/BlueprintUpgradeDialog.tsx"
    ).read_text(encoding="utf-8")
    assert "item.new_content" in body
    assert "upgrade_show_new" in body


@pytest.mark.asyncio
async def test_the_plan_includes_stale_blueprint_workflows(
    db_session, workflow_scenario, opc_payload,
):
    result = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )
    item = next(
        entry for entry in result["items"]
        if entry["kind"] == "workflow"
        and entry["slug"] == workflow_scenario["spec"]["slug"]
    )
    assert item["action"] == UpgradeAction.UPDATE.value
    assert any("workflow graph" in change for change in item["changes"])
    assert '"prefill"' in item["new_content"]["steps"]


@pytest.mark.asyncio
async def test_an_edited_blueprint_workflow_is_kept(
    db_session, workflow_scenario, opc_payload,
):
    workflow_scenario["definition"].revision = 4
    await db_session.flush()

    result = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )
    item = next(
        entry for entry in result["items"]
        if entry["kind"] == "workflow"
        and entry["slug"] == workflow_scenario["spec"]["slug"]
    )
    assert item["action"] == UpgradeAction.KEEP_YOURS.value


@pytest.mark.asyncio
async def test_external_to_internal_workflow_removes_and_reverts_blueprint_binding(
    db_session, scenario, payload,
):
    internal_payload = copy.deepcopy(payload)
    workflow_spec = internal_payload["recipe"]["workflows"][0]
    workflow_spec["internal"] = True
    bindings = list((await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == scenario["workspace"].id,
        )
    )).scalars().all())
    binding = next(
        row for row in bindings
        if (row.config or {}).get("workspace_blueprint_workflow_slug")
        == workflow_spec["slug"]
    )
    binding_id = binding.id

    preview = await plan(
        db_session,
        workspace=scenario["workspace"],
        payload=internal_payload,
    )
    item = next(
        entry for entry in preview["items"]
        if entry["kind"] == "workflow" and entry["slug"] == workflow_spec["slug"]
    )
    assert item["action"] == UpgradeAction.UPDATE.value
    assert item["binding_remove"] is True
    assert item["changes"] == ["removes obsolete Workspace binding"]

    await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=internal_payload,
    )
    assert await db_session.get(WorkflowBinding, binding_id) is None

    await revert(db_session, workspace=scenario["workspace"])
    restored = await db_session.get(WorkflowBinding, binding_id)
    assert restored is not None
    assert restored.workspace_id == scenario["workspace"].id
    assert restored.config["workspace_blueprint_workflow_slug"] == workflow_spec["slug"]
    assert restored.config["source"] == "blueprint"


@pytest.mark.asyncio
async def test_a_new_blueprint_workflow_is_installed_and_bound_on_upgrade(
    db_session, payload,
):
    """Shipping a new Flow must reach existing Blueprint workspaces too."""
    entity_id = "01TESTENTITY0000000000000H"
    workflow_payload = copy.deepcopy(payload)
    workflow_payload["embedded"]["skills"] = []
    workflow_payload["embedded"]["agents"] = []
    workflow_payload["embedded"]["knowledge_packs"] = []
    workflow_payload["recipe"]["workflows"] = [
        copy.deepcopy(payload["recipe"]["workflows"][0])
    ]
    older = copy.deepcopy(workflow_payload)
    older["recipe"]["workflows"] = []
    workspace = await _workspace(
        db_session,
        entity_id,
        installed_from=older,
    )

    preview = await plan(
        db_session,
        workspace=workspace,
        payload=workflow_payload,
    )
    item = next(
        entry
        for entry in preview["items"]
        if entry["kind"] == "workflow"
    )
    assert item["action"] == UpgradeAction.MISSING.value
    assert item["changes"] == ["installs Blueprint Flow and Workspace binding"]
    assert '"youtube_visibility"' in item["new_content"]["steps"]

    result = await apply(
        db_session,
        workspace=workspace,
        payload=workflow_payload,
        current_version="1.0.21",
    )

    definition = (await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.name == "Create Stickman Video → YouTube",
        )
    )).scalar_one()
    binding = (await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workspace.id,
            WorkflowBinding.workflow_id == definition.id,
        )
    )).scalar_one()
    installation = (await db_session.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.workflow_id == definition.id,
        )
    )).scalar_one()
    assert binding.name == "Create Stickman Video → YouTube"
    assert binding.trigger_type == "mcp"
    assert binding.variables["youtube_visibility"] == "public"
    assert binding.config["workspace_blueprint_workflow_slug"] == (
        "stickman-video-to-youtube-v1"
    )
    from packages.core.blueprints.installer import (
        blueprint_workflow_installation_source_id,
    )

    assert installation.template_id == blueprint_workflow_installation_source_id(
        f"builtin:{SLUG}", workspace.id,
    )
    assert result["updated"] == [{
        "kind": "workflow",
        "name": "Create Stickman Video → YouTube",
        "changes": ["installs Blueprint Flow and Workspace binding"],
    }]

    reverted = await revert(db_session, workspace=workspace)
    assert reverted["reverted"] == [{
        "kind": "workflow",
        "name": "Create Stickman Video → YouTube",
    }]
    assert await db_session.get(WorkflowDefinition, definition.id) is None
    assert await db_session.get(WorkflowBinding, binding.id) is None
    assert await db_session.get(WorkflowTemplateInstallation, installation.id) is None


# ── Applying ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_applying_brings_the_item_to_the_blueprint(db_session, scenario, payload):
    await apply(db_session, workspace=scenario["workspace"], payload=payload, by_user_id="u1")
    assert scenario["skill"].system_prompt == payload["embedded"]["skills"][0]["system_prompt"]
    assert len(scenario["skill"].system_prompt) > len(STALE_PROMPT)


@pytest.mark.asyncio
async def test_applying_uses_locked_workspace_settings_instead_of_stale_preview(
    monkeypatch,
    payload,
):
    from packages.core.blueprints import upgrade
    from packages.core.blueprints import installer
    from packages.core.services import marketplace_resource_links

    blueprint_record = {
        BLUEPRINT_ID_KEY: f"builtin:{SLUG}",
        "blueprint_slug": SLUG,
    }
    stale_workspace = SimpleNamespace(
        id="workspace-1",
        entity_id="entity-1",
        settings={BLUEPRINT_SETTINGS_KEY: blueprint_record},
    )
    locked_workspace = SimpleNamespace(
        id=stale_workspace.id,
        entity_id=stale_workspace.entity_id,
        settings={
            BLUEPRINT_SETTINGS_KEY: dict(blueprint_record),
            "concurrent_setting": "preserve-me",
        },
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=locked_workspace),
        flush=AsyncMock(),
    )

    async def empty_plan(*_args, **_kwargs):
        return {"items": []}

    monkeypatch.setattr(upgrade, "plan", empty_plan)
    sync_live_contract = AsyncMock()
    monkeypatch.setattr(
        installer,
        "sync_workspace_live_setup_requirements",
        sync_live_contract,
    )
    rekey_link = AsyncMock()
    monkeypatch.setattr(
        marketplace_resource_links,
        "rekey_marketplace_resource_link",
        rekey_link,
    )

    await apply(db, workspace=stale_workspace, payload=payload)

    sync_live_contract.assert_awaited_once_with(
        db,
        workspace=locked_workspace,
        payload=payload,
    )

    db.get.assert_awaited_once_with(
        Workspace,
        stale_workspace.id,
        populate_existing=True,
        with_for_update=True,
    )
    assert locked_workspace.settings["concurrent_setting"] == "preserve-me"
    assert (
        blueprint_freshness(locked_workspace.settings, payload)
        is BlueprintFreshness.CURRENT
    )
    rekey_link.assert_awaited_once()


@pytest.mark.asyncio
async def test_applying_rejects_a_plan_with_unmaterialized_components(
    db_session,
    scenario,
    payload,
):
    incomplete = copy.deepcopy(payload)
    missing_skill = copy.deepcopy(payload["embedded"]["skills"][0])
    missing_skill["slug"] = "missing-blueprint-skill"
    missing_skill["name"] = "Missing Blueprint Skill"
    incomplete["embedded"]["skills"].append(missing_skill)

    with pytest.raises(BlueprintUpgradeIncompleteError, match="missing"):
        await apply(
            db_session,
            workspace=scenario["workspace"],
            payload=incomplete,
            require_complete_workspace=True,
        )

    assert scenario["skill"].system_prompt == STALE_PROMPT
    assert blueprint_freshness(
        scenario["workspace"].settings,
        incomplete,
    ) is BlueprintFreshness.UPDATE_AVAILABLE


@pytest.mark.asyncio
async def test_applying_updates_blueprint_workflow_inputs(
    db_session, workflow_scenario, opc_payload,
):
    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )
    bindings = list((await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
        )
    )).scalars().all())
    binding = next(
        row for row in bindings
        if (row.config or {}).get("workspace_blueprint_workflow_slug")
        == workflow_scenario["spec"]["slug"]
    )
    definition = await db_session.get(WorkflowDefinition, binding.workflow_id)
    assert definition is not None
    assert definition.id != workflow_scenario["definition"].id
    assert definition.workspace_id == workflow_scenario["workspace"].id
    start = next(
        step for step in definition.steps
        if step["type"] == "trigger"
    )
    run_inputs = start["config"]["run_inputs"]
    assert run_inputs[0]["hidden"] is True
    topic_input = next(item for item in run_inputs if item["key"] == "topic_brief")
    assert topic_input["prefill"]["workflow_slug"] == "opc-generate-topic-from-knowledge-v1"


@pytest.mark.asyncio
async def test_upgrading_a_legacy_shared_flow_isolates_the_target_workspace(
    db_session, workflow_scenario, opc_payload,
):
    target = workflow_scenario["workspace"]
    shared_definition = workflow_scenario["definition"]
    shared_steps = copy.deepcopy(shared_definition.steps)
    target_binding = (await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == target.id,
            WorkflowBinding.workflow_id == shared_definition.id,
        )
    )).scalar_one()
    other = Workspace(
        entity_id=target.entity_id,
        name="Other legacy Workspace",
        settings=copy.deepcopy(target.settings),
    )
    db_session.add(other)
    await db_session.flush()
    other_binding = WorkflowBinding(
        entity_id=target.entity_id,
        workspace_id=other.id,
        workflow_id=shared_definition.id,
        name=target_binding.name,
        trigger_type=target_binding.trigger_type,
        config=copy.deepcopy(target_binding.config),
        enabled=True,
        status="active",
    )
    db_session.add(other_binding)
    await db_session.flush()

    await apply(db_session, workspace=target, payload=opc_payload)
    await db_session.refresh(target_binding)
    await db_session.refresh(other_binding)

    assert target_binding.workflow_id != shared_definition.id
    assert other_binding.workflow_id == shared_definition.id
    assert shared_definition.steps == shared_steps
    isolated = await db_session.get(
        WorkflowDefinition, target_binding.workflow_id,
    )
    assert isolated is not None
    assert isolated.workspace_id == target.id
    assert isolated.steps != shared_steps

    await revert(db_session, workspace=target)
    await db_session.refresh(other_binding)
    assert isolated.steps == shared_steps
    assert other_binding.workflow_id == shared_definition.id


@pytest.mark.asyncio
async def test_applying_leaves_an_edited_item_alone(db_session, scenario, payload):
    scenario["skill"].revision = 4
    await db_session.flush()

    result = await apply(db_session, workspace=scenario["workspace"], payload=payload)
    assert scenario["skill"].system_prompt == STALE_PROMPT
    assert result["updated"] == []
    assert [i["slug"] for i in result["kept_yours"]] == [scenario["skill"].slug]


@pytest.mark.asyncio
async def test_applying_can_use_blueprint_for_an_explicitly_resolved_conflict(
    db_session, scenario, payload,
):
    scenario["skill"].revision = 4
    await db_session.flush()
    preview = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    conflict = next(
        item for item in preview["items"]
        if item["slug"] == scenario["skill"].slug
    )

    result = await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=payload,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
        conflict_resolutions=[{
            "kind": conflict["kind"],
            "slug": conflict["slug"],
            "resolution": "use_blueprint",
            "expected_revision": conflict["revision"],
        }],
    )

    assert scenario["skill"].system_prompt == payload["embedded"]["skills"][0]["system_prompt"]
    assert [item["name"] for item in result["updated"]] == [scenario["skill"].name]
    assert result["kept_yours"] == []
    assert result["can_revert"] is True


@pytest.mark.asyncio
async def test_conflict_overwrite_requires_resource_edit_permission(
    db_session,
    scenario,
    payload,
    monkeypatch,
):
    from packages.core.services import resource_access

    scenario["skill"].revision = 4
    await db_session.flush()
    preview = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    conflict = next(
        item for item in preview["items"]
        if item["slug"] == scenario["skill"].slug
    )

    async def deny_resource_edit(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        resource_access,
        "user_can_access_resource",
        deny_resource_edit,
    )
    actor = type("Actor", (), {"id": "workspace-owner", "role": "member"})()

    with pytest.raises(BlueprintUpgradeAccessDeniedError):
        await apply(
            db_session,
            workspace=scenario["workspace"],
            payload=payload,
            by_user_id=actor.id,
            actor=actor,
            expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
            conflict_resolutions=[{
                "kind": conflict["kind"],
                "slug": conflict["slug"],
                "resolution": "use_blueprint",
                "expected_revision": conflict["revision"],
            }],
        )

    assert scenario["skill"].system_prompt == STALE_PROMPT


@pytest.mark.asyncio
async def test_workflow_conflict_overwrite_requires_resource_edit_permission(
    db_session,
    workflow_scenario,
    opc_payload,
    monkeypatch,
):
    from packages.core.services import resource_access

    workflow_scenario["definition"].revision = 4
    await db_session.flush()
    preview = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )
    conflict = next(
        item for item in preview["items"]
        if item["kind"] == "workflow"
        and item["slug"] == workflow_scenario["spec"]["slug"]
    )
    original_steps = copy.deepcopy(workflow_scenario["definition"].steps)

    async def deny_resource_edit(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        resource_access,
        "user_can_access_resource",
        deny_resource_edit,
    )
    actor = type("Actor", (), {"id": "workspace-owner", "role": "member"})()

    with pytest.raises(BlueprintUpgradeAccessDeniedError):
        await apply(
            db_session,
            workspace=workflow_scenario["workspace"],
            payload=opc_payload,
            by_user_id=actor.id,
            actor=actor,
            expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
            conflict_resolutions=[{
                "kind": conflict["kind"],
                "slug": conflict["slug"],
                "resolution": "use_blueprint",
                "expected_revision": conflict["revision"],
            }],
        )

    assert workflow_scenario["definition"].steps == original_steps


@pytest.mark.asyncio
async def test_missing_workflow_does_not_claim_an_unmapped_same_name_definition(
    db_session,
    workflow_scenario,
    opc_payload,
):
    workflow_payload = copy.deepcopy(opc_payload)
    workflow_payload["embedded"] = {
        "agents": [],
        "skills": [],
        "knowledge_packs": [],
    }
    workflow_payload["recipe"]["workflows"] = [
        copy.deepcopy(workflow_scenario["spec"])
    ]
    binding = (await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
            WorkflowBinding.workflow_id == workflow_scenario["definition"].id,
        )
    )).scalar_one()
    await db_session.delete(workflow_scenario["installation"])
    await db_session.delete(binding)
    await db_session.flush()

    preview = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=workflow_payload,
    )
    missing = next(
        item for item in preview["items"]
        if item["kind"] == "workflow"
        and item["slug"] == workflow_scenario["spec"]["slug"]
    )
    assert missing["action"] == UpgradeAction.MISSING.value
    original_steps = copy.deepcopy(workflow_scenario["definition"].steps)
    actor = type("Actor", (), {"id": "workspace-owner", "role": "member"})()

    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=workflow_payload,
        by_user_id=actor.id,
        actor=actor,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
    )

    assert workflow_scenario["definition"].steps == original_steps
    installation = (await db_session.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id
            == workflow_scenario["workspace"].entity_id,
            WorkflowTemplateInstallation.component_key
            == workflow_scenario["spec"]["slug"],
        )
    )).scalar_one()
    assert installation.workflow_id != workflow_scenario["definition"].id

    blueprint_workflow_id = installation.workflow_id
    await revert(db_session, workspace=workflow_scenario["workspace"])

    assert await db_session.get(
        WorkflowDefinition, workflow_scenario["definition"].id,
    ) is workflow_scenario["definition"]
    assert workflow_scenario["definition"].steps == original_steps
    assert await db_session.get(WorkflowDefinition, blueprint_workflow_id) is None


@pytest.mark.asyncio
async def test_upgrade_materializes_new_subworkflow_before_updating_parent(
    db_session,
    workflow_scenario,
    opc_payload,
):
    workflow_payload = copy.deepcopy(opc_payload)
    workflow_payload["embedded"] = {
        "agents": [],
        "skills": [],
        "knowledge_packs": [],
    }
    parent_spec = copy.deepcopy(workflow_scenario["spec"])
    child_slug = "new-internal-child-flow"
    parent_spec["steps"] = [{
        "id": "run-new-child",
        "type": "subworkflow",
        "config": {"source_workflow_key": child_slug},
    }]
    child_spec = {
        "slug": child_slug,
        "internal": True,
        "steps": [{"id": "child-work", "kind": "agent_call"}],
    }
    # Parent intentionally precedes the newly introduced child. Upgrade must
    # not depend on declaration order.
    workflow_payload["recipe"]["workflows"] = [parent_spec, child_spec]
    original_parent_steps = copy.deepcopy(workflow_scenario["definition"].steps)

    preview = await plan(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=workflow_payload,
    )
    items = {
        item["slug"]: item
        for item in preview["items"]
        if item["kind"] == "workflow"
    }
    assert items[parent_spec["slug"]]["action"] == UpgradeAction.UPDATE.value
    assert items[child_slug]["action"] == UpgradeAction.MISSING.value

    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=workflow_payload,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
    )

    child_installation = (await db_session.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id
            == workflow_scenario["workspace"].entity_id,
            WorkflowTemplateInstallation.component_key == child_slug,
        )
    )).scalar_one()
    child = await db_session.get(
        WorkflowDefinition,
        child_installation.workflow_id,
    )
    assert child is not None
    parent_binding = (await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
            WorkflowBinding.config["workspace_blueprint_workflow_slug"].astext
            == parent_spec["slug"],
        )
    )).scalar_one()
    upgraded_parent = await db_session.get(
        WorkflowDefinition,
        parent_binding.workflow_id,
    )
    assert upgraded_parent is not None
    [subworkflow] = [
        step
        for step in upgraded_parent.steps
        if step["type"] == "subworkflow"
    ]
    assert subworkflow["config"]["source_workflow_key"] == child_slug
    assert subworkflow["config"]["workflow_id"] == child.id
    child_bindings = list((await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
            WorkflowBinding.workflow_id == child.id,
        )
    )).scalars())
    assert child_bindings == []

    await revert(db_session, workspace=workflow_scenario["workspace"])

    assert workflow_scenario["definition"].steps == original_parent_steps
    assert await db_session.get(WorkflowDefinition, child.id) is None


@pytest.mark.asyncio
async def test_missing_mapped_workflow_restores_definition_mapping_and_binding(
    db_session,
    workflow_scenario,
    opc_payload,
):
    workflow_payload = copy.deepcopy(opc_payload)
    workflow_payload["embedded"] = {
        "agents": [],
        "skills": [],
        "knowledge_packs": [],
    }
    workflow_payload["recipe"]["workflows"] = [
        copy.deepcopy(workflow_scenario["spec"])
    ]
    installation = workflow_scenario["installation"]
    installation.installed_version = "0.9.0"
    installation.installation_metadata = {"source_workflow_key": "legacy-key"}
    binding = (await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
            WorkflowBinding.workflow_id == workflow_scenario["definition"].id,
        )
    )).scalar_one()
    await db_session.delete(binding)
    await db_session.flush()
    original_steps = copy.deepcopy(workflow_scenario["definition"].steps)

    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=workflow_payload,
        current_version="1.0.7",
    )

    created_binding = (await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
        )
    )).scalar_one()
    isolated = await db_session.get(
        WorkflowDefinition, created_binding.workflow_id,
    )
    assert isolated is not None
    assert isolated.id != workflow_scenario["definition"].id
    assert isolated.steps != original_steps
    isolated_installation = (await db_session.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.workflow_id == isolated.id,
        )
    )).scalar_one()
    assert isolated_installation.installed_version == "1.0.7"
    assert installation.installed_version == "0.9.0"

    await revert(db_session, workspace=workflow_scenario["workspace"])

    assert workflow_scenario["definition"].steps == original_steps
    assert isolated.steps == original_steps
    assert await db_session.get(WorkflowTemplateInstallation, installation.id) is installation
    assert installation.installed_version == "0.9.0"
    assert installation.installation_metadata == {"source_workflow_key": "legacy-key"}
    assert await db_session.get(WorkflowBinding, created_binding.id) is None


@pytest.mark.asyncio
async def test_applying_can_explicitly_keep_a_resolved_conflict(
    db_session, scenario, payload,
):
    scenario["skill"].revision = 4
    await db_session.flush()
    preview = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    conflict = next(
        item for item in preview["items"]
        if item["slug"] == scenario["skill"].slug
    )

    result = await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=payload,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
        conflict_resolutions=[{
            "kind": conflict["kind"],
            "slug": conflict["slug"],
            "resolution": "keep_yours",
            "expected_revision": conflict["revision"],
        }],
    )

    assert scenario["skill"].system_prompt == STALE_PROMPT
    assert result["updated"] == []
    assert [item["slug"] for item in result["kept_yours"]] == [scenario["skill"].slug]


@pytest.mark.asyncio
async def test_conflict_resolution_rejects_an_item_edited_after_preview(
    db_session, scenario, payload,
):
    scenario["skill"].revision = 4
    await db_session.flush()
    preview = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    conflict = next(
        item for item in preview["items"]
        if item["slug"] == scenario["skill"].slug
    )
    scenario["skill"].revision = 5
    await db_session.flush()

    with pytest.raises(BlueprintUpgradePlanChangedError):
        await apply(
            db_session,
            workspace=scenario["workspace"],
            payload=payload,
            expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
            conflict_resolutions=[{
                "kind": conflict["kind"],
                "slug": conflict["slug"],
                "resolution": "use_blueprint",
                "expected_revision": conflict["revision"],
            }],
        )

    assert scenario["skill"].system_prompt == STALE_PROMPT


@pytest.mark.asyncio
async def test_conflict_resolution_rechecks_revision_after_recomputing_plan(
    db_session,
    scenario,
    payload,
    monkeypatch,
):
    from packages.core.blueprints import upgrade as upgrade_module

    scenario["skill"].revision = 4
    await db_session.flush()
    preview = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    conflict = next(
        item for item in preview["items"]
        if item["slug"] == scenario["skill"].slug
    )
    original_plan = upgrade_module.plan

    async def plan_then_edit(*args, **kwargs):
        intended = await original_plan(*args, **kwargs)
        scenario["skill"].revision = 5
        return intended

    monkeypatch.setattr(upgrade_module, "plan", plan_then_edit)
    with pytest.raises(BlueprintUpgradePlanChangedError):
        await apply(
            db_session,
            workspace=scenario["workspace"],
            payload=payload,
            expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
            conflict_resolutions=[{
                "kind": conflict["kind"],
                "slug": conflict["slug"],
                "resolution": "use_blueprint",
                "expected_revision": conflict["revision"],
            }],
        )

    assert scenario["skill"].system_prompt == STALE_PROMPT


@pytest.mark.asyncio
async def test_conflict_resolution_rejects_a_blueprint_changed_after_preview(
    db_session, scenario, payload,
):
    scenario["skill"].revision = 4
    await db_session.flush()
    preview = await plan(db_session, workspace=scenario["workspace"], payload=payload)
    newer = copy.deepcopy(payload)
    newer["embedded"]["skills"][0]["system_prompt"] += " newer"

    with pytest.raises(BlueprintUpgradePlanChangedError):
        await apply(
            db_session,
            workspace=scenario["workspace"],
            payload=newer,
            expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
            conflict_resolutions=[],
        )

    assert scenario["skill"].system_prompt == STALE_PROMPT


@pytest.mark.asyncio
async def test_the_badge_goes_quiet_after_applying(db_session, scenario, payload):
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    assert blueprint_freshness(
        scenario["workspace"].settings, payload,
    ) is BlueprintFreshness.CURRENT


@pytest.mark.asyncio
async def test_applying_bumps_the_revision(db_session, scenario, payload):
    """So a later upgrade treats this as the workspace's content, not a
    fresh install it may overwrite again."""
    before = scenario["skill"].revision
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    assert scenario["skill"].revision > before


@pytest.mark.asyncio
async def test_a_second_upgrade_still_works(db_session, scenario, payload):
    """Applying bumps the revision, which is also the signal for "the
    workspace edited this". Without care, one upgrade would make an item
    permanently ineligible for the next one."""
    await apply(db_session, workspace=scenario["workspace"], payload=payload)

    newer = copy.deepcopy(payload)
    newer["embedded"]["skills"][0]["system_prompt"] = "a later correction, longer still " * 30

    result = await plan(db_session, workspace=scenario["workspace"], payload=newer)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert item["action"] == UpgradeAction.UPDATE.value, (
        "an upgrade must not lock the item out of the next one"
    )


@pytest.mark.asyncio
async def test_an_edit_after_an_upgrade_is_still_yours(db_session, scenario, payload):
    """The distinction has to survive an upgrade, not just precede it."""
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    scenario["skill"].revision += 1          # the operator edits it afterwards
    await db_session.flush()

    newer = copy.deepcopy(payload)
    newer["embedded"]["skills"][0]["system_prompt"] = "a later correction " * 40

    result = await plan(db_session, workspace=scenario["workspace"], payload=newer)
    item = next(i for i in result["items"] if i["slug"] == scenario["skill"].slug)
    assert item["action"] == UpgradeAction.KEEP_YOURS.value


# ── Reverting ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revert_puts_the_old_content_back(db_session, scenario, payload):
    await apply(db_session, workspace=scenario["workspace"], payload=payload, by_user_id="u1")
    assert scenario["skill"].system_prompt != STALE_PROMPT

    result = await revert(db_session, workspace=scenario["workspace"], by_user_id="u1")
    assert scenario["skill"].system_prompt == STALE_PROMPT
    assert len(result["reverted"]) == 1


@pytest.mark.asyncio
async def test_revert_restores_the_previous_workflow_graph(
    db_session, workflow_scenario, opc_payload,
):
    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )
    bindings = list((await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workflow_scenario["workspace"].id,
        )
    )).scalars().all())
    binding = next(
        row for row in bindings
        if (row.config or {}).get("workspace_blueprint_workflow_slug")
        == workflow_scenario["spec"]["slug"]
    )
    isolated = await db_session.get(WorkflowDefinition, binding.workflow_id)
    assert isolated is not None
    assert isolated.id != workflow_scenario["definition"].id
    assert workflow_scenario["definition"].steps == workflow_scenario["older_steps"]
    assert isolated.steps != workflow_scenario["older_steps"]

    await revert(db_session, workspace=workflow_scenario["workspace"])
    assert isolated.steps == workflow_scenario["older_steps"]


@pytest.mark.asyncio
async def test_revert_rejects_an_item_edited_after_upgrade(
    db_session,
    scenario,
    payload,
):
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    operator_prompt = "Operator edit after Blueprint upgrade"
    scenario["skill"].system_prompt = operator_prompt
    scenario["skill"].revision += 1
    await db_session.flush()

    with pytest.raises(BlueprintUpgradePlanChangedError, match="changed after upgrade"):
        await revert(db_session, workspace=scenario["workspace"])

    assert scenario["skill"].system_prompt == operator_prompt
    assert RESTORE_POINT_KEY in (
        scenario["workspace"].settings[BLUEPRINT_SETTINGS_KEY]
    )


@pytest.mark.asyncio
async def test_revert_requires_resource_edit_permission_before_writing(
    db_session,
    scenario,
    payload,
    monkeypatch,
):
    from packages.core.services import resource_access

    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    blueprint_prompt = scenario["skill"].system_prompt

    async def deny_resource_edit(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        resource_access,
        "user_can_access_resource",
        deny_resource_edit,
    )
    actor = type("Actor", (), {"id": "workspace-owner", "role": "member"})()

    with pytest.raises(BlueprintUpgradeAccessDeniedError):
        await revert(
            db_session,
            workspace=scenario["workspace"],
            by_user_id=actor.id,
            actor=actor,
        )

    assert scenario["skill"].system_prompt == blueprint_prompt
    assert RESTORE_POINT_KEY in (
        scenario["workspace"].settings[BLUEPRINT_SETTINGS_KEY]
    )


@pytest.mark.asyncio
async def test_the_badge_comes_back_after_reverting(db_session, scenario, payload):
    """The workspace really is behind again. A revert that left it looking
    current would hide the state the operator just chose."""
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    await revert(db_session, workspace=scenario["workspace"])
    assert blueprint_freshness(
        scenario["workspace"].settings, payload,
    ) is BlueprintFreshness.UPDATE_AVAILABLE


@pytest.mark.asyncio
async def test_reverting_twice_is_not_a_second_undo(db_session, scenario, payload):
    """One step back, and the restore point is spent."""
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    await revert(db_session, workspace=scenario["workspace"])

    again = await revert(db_session, workspace=scenario["workspace"])
    assert again["reverted"] == []
    assert scenario["skill"].system_prompt == STALE_PROMPT


@pytest.mark.asyncio
async def test_nothing_to_revert_before_any_upgrade(db_session, scenario):
    result = await revert(db_session, workspace=scenario["workspace"])
    assert result["reverted"] == []


@pytest.mark.asyncio
async def test_the_restore_point_holds_only_what_was_overwritten(db_session, scenario, payload):
    """A snapshot of the workspace would be the easy way and the wrong size."""
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    point = scenario["workspace"].settings[BLUEPRINT_SETTINGS_KEY][RESTORE_POINT_KEY]

    assert len(point["items"]) == 1
    assert set(point["items"][0]["before"]) <= {"system_prompt", "tools", "status"}
    assert point["items"][0]["before"]["system_prompt"] == STALE_PROMPT


@pytest.mark.asyncio
async def test_a_plan_reports_whether_undo_is_available(db_session, scenario, payload):
    assert (await plan(db_session, workspace=scenario["workspace"], payload=payload))["can_revert"] is False
    await apply(db_session, workspace=scenario["workspace"], payload=payload)
    assert (await plan(db_session, workspace=scenario["workspace"], payload=payload))["can_revert"] is True


# ── Not from a blueprint ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_workspace_with_no_blueprint_plans_nothing(db_session, payload):
    ws = Workspace(entity_id="01TESTENTITY0000000000000B", name="Plain", settings={})
    db_session.add(ws)
    await db_session.flush()

    result = await plan(db_session, workspace=ws, payload=payload)
    assert result["items"] == []


# ── Versioned installs ────────────────────────────────────────────────
#
# Freshness compares versions BEFORE fingerprints (a version survives a
# payload the reader cannot fetch), so an install that records a version is
# judged by it alone. apply() originally advanced only the fingerprint —
# on staging, the stickman workspace took the 7445-character skill and the
# badge stayed on forever, with the next plan offering nothing to update.


@pytest.mark.asyncio
async def test_applying_moves_the_installed_version_too(db_session, scenario, payload):
    """The badge is version-first, so a successful apply must move the
    version — matching fingerprints alone leave it on forever."""
    from packages.core.blueprints.freshness import BLUEPRINT_VERSION_KEY

    ws = scenario["workspace"]
    settings = dict(ws.settings)
    settings[BLUEPRINT_SETTINGS_KEY] = {
        **settings[BLUEPRINT_SETTINGS_KEY], BLUEPRINT_VERSION_KEY: "1.0.0",
    }
    ws.settings = settings

    assert blueprint_freshness(
        ws.settings, payload, current_version="1.0.2",
    ) is BlueprintFreshness.UPDATE_AVAILABLE

    await apply(db_session, workspace=ws, payload=payload, current_version="1.0.2")

    record = ws.settings[BLUEPRINT_SETTINGS_KEY]
    assert record[BLUEPRINT_VERSION_KEY] == "1.0.2"
    assert blueprint_freshness(
        ws.settings, payload, current_version="1.0.2",
    ) is BlueprintFreshness.CURRENT


@pytest.mark.asyncio
async def test_confirming_already_matching_content_advances_only_the_version(
    db_session, scenario, payload,
):
    """A published version can move after the installed rows were already
    reconciled by another idempotent path. The confirmation must clear that
    stale version marker without inventing an update or an undo point."""
    from packages.core.blueprints.freshness import BLUEPRINT_VERSION_KEY

    ws = scenario["workspace"]
    matching_payload = copy.deepcopy(payload)
    # This fixture materialises only the embedded skill. Keep the plan scoped
    # to that installed surface so the assertion is specifically about a
    # version-only confirmation rather than missing agent/Flow installation.
    matching_payload["embedded"]["agents"] = []
    matching_payload["recipe"]["workflows"] = []
    scenario["skill"].system_prompt = matching_payload["embedded"]["skills"][0]["system_prompt"]
    settings = dict(ws.settings)
    settings[BLUEPRINT_SETTINGS_KEY] = {
        **settings[BLUEPRINT_SETTINGS_KEY],
        BLUEPRINT_VERSION_KEY: "1.0.1",
        UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
            blueprint_upgrade_unsupported_fingerprint(matching_payload)
        ),
    }
    ws.settings = settings
    await db_session.flush()

    preview = await plan(db_session, workspace=ws, payload=matching_payload)
    assert {item["action"] for item in preview["items"]} == {
        UpgradeAction.UNCHANGED.value,
    }

    result = await apply(
        db_session,
        workspace=ws,
        payload=matching_payload,
        current_version="1.0.2",
    )

    assert result["updated"] == []
    assert result["kept_yours"] == []
    assert result["can_revert"] is False
    assert RESTORE_POINT_KEY not in ws.settings[BLUEPRINT_SETTINGS_KEY]
    assert blueprint_freshness(
        ws.settings, matching_payload, current_version="1.0.2",
    ) is BlueprintFreshness.CURRENT


@pytest.mark.asyncio
async def test_upgrade_materializes_and_can_revert_legacy_inline_knowledge(
    db_session, scenario, payload,
):
    """Legacy installs have the Knowledge group but only todo placeholders."""
    knowledge_payload = copy.deepcopy(payload)
    knowledge_payload["embedded"]["skills"] = []
    knowledge_payload["embedded"]["agents"] = []
    knowledge_payload["recipe"]["workflows"] = []
    pack = knowledge_payload["embedded"]["knowledge_packs"][0]
    group = DocumentGroup(
        entity_id=scenario["entity_id"],
        workspace_id=scenario["workspace"].id,
        name=pack["title"],
        settings={"mode": "inline_text", "installed_from_blueprint_slug": pack["slug"]},
    )
    db_session.add(group)
    await db_session.flush()

    preview = await plan(
        db_session,
        workspace=scenario["workspace"],
        payload=knowledge_payload,
    )
    additions = [
        item for item in preview["items"]
        if item["kind"] == "knowledge_document" and item["action"] == "update"
    ]
    assert len(additions) == len(pack["starter_documents"])

    result = await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=knowledge_payload,
        require_complete_workspace=True,
    )
    assert len(result["updated"]) == len(pack["starter_documents"])
    assert result["fully_synchronized"] is False
    rows = list((await db_session.execute(
        select(Document)
        .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
        .where(DocumentGroupMember.group_id == group.id)
    )).scalars().all())
    assert {row.name for row in rows} == {
        document["path"] for document in pack["starter_documents"]
    }
    assert all(row.metadata_.get("content_text") for row in rows)

    reverted = await revert(db_session, workspace=scenario["workspace"])
    assert len(reverted["reverted"]) == len(pack["starter_documents"])
    remaining = list((await db_session.execute(
        select(Document)
        .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
        .where(DocumentGroupMember.group_id == group.id)
    )).scalars().all())
    assert remaining == []


@pytest.mark.asyncio
async def test_upgrade_binds_live_knowledge_template_without_overwriting_document(
    db_session, scenario, payload,
):
    """A Blueprint update may attach a renderer, but never replace operator text."""

    knowledge_payload = copy.deepcopy(payload)
    knowledge_payload["embedded"]["skills"] = []
    knowledge_payload["embedded"]["agents"] = []
    knowledge_payload["recipe"]["workflows"] = []
    pack = next(
        item for item in knowledge_payload["embedded"]["knowledge_packs"]
        if item["slug"] == "solo-stickman-studio-ops"
    )
    template_document = next(
        item for item in pack["starter_documents"]
        if item["path"] == "topic-ledger/ledger.md"
    )
    pack["starter_documents"] = [template_document]
    group = DocumentGroup(
        entity_id=scenario["entity_id"],
        workspace_id=scenario["workspace"].id,
        name=pack["title"],
        settings={"mode": "inline_text", "installed_from_blueprint_slug": pack["slug"]},
    )
    db_session.add(group)
    await db_session.flush()
    legacy = Document(
        entity_id=scenario["entity_id"],
        name="topic-ledger/ledger.md",
        source="blueprint",
        metadata_={"content_text": "# Operator-owned Ledger notes"},
    )
    db_session.add(legacy)
    await db_session.flush()
    db_session.add(DocumentGroupMember(document_id=legacy.id, group_id=group.id))
    await db_session.flush()

    preview = await plan(
        db_session,
        workspace=scenario["workspace"],
        payload=knowledge_payload,
    )
    ledger_item = next(
        item for item in preview["items"]
        if item["kind"] == "knowledge_document"
    )
    assert ledger_item["action"] == "update"
    assert ledger_item["changes"] == ["updates live Knowledge template binding"]

    await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=knowledge_payload,
    )
    assert legacy.metadata_["content_text"] == "# Operator-owned Ledger notes"
    assert legacy.metadata_["blueprint_template"] == template_document["template"]
    assert legacy.metadata_["blueprint_starter_path"] == "topic-ledger/ledger.md"

    await revert(db_session, workspace=scenario["workspace"])
    assert legacy.metadata_["content_text"] == "# Operator-owned Ledger notes"
    assert "blueprint_template" not in legacy.metadata_


@pytest.mark.asyncio
async def test_upgrade_reverts_only_new_shared_document_membership(
    db_session, scenario, payload,
):
    knowledge_payload = copy.deepcopy(payload)
    knowledge_payload["embedded"]["skills"] = []
    knowledge_payload["embedded"]["agents"] = []
    knowledge_payload["recipe"]["workflows"] = []
    shared_document = {
        "key": "shared-upgrade-playbook",
        "path": "shared.md",
        "body_md": "# Shared",
    }
    knowledge_payload["embedded"]["knowledge_packs"] = [
        {
            "slug": "operations",
            "title": "Operations",
            "mode": "inline_text",
            "folder_structure": [],
            "starter_documents": [dict(shared_document)],
        },
        {
            "slug": "support",
            "title": "Support",
            "mode": "inline_text",
            "folder_structure": [],
            "starter_documents": [dict(shared_document)],
        },
    ]
    operations = DocumentGroup(
        entity_id=scenario["entity_id"],
        workspace_id=scenario["workspace"].id,
        name="Operations",
        settings={"mode": "inline_text", "installed_from_blueprint_slug": "operations"},
    )
    support = DocumentGroup(
        entity_id=scenario["entity_id"],
        workspace_id=scenario["workspace"].id,
        name="Support",
        settings={"mode": "inline_text", "installed_from_blueprint_slug": "support"},
    )
    shared = Document(
        entity_id=scenario["entity_id"],
        name="shared.md",
        source="blueprint",
        metadata_={
            "content_text": "# Shared",
            "blueprint_document_key": "shared-upgrade-playbook",
            "origin": {"workspace_id": scenario["workspace"].id},
        },
    )
    db_session.add_all([operations, support, shared])
    await db_session.flush()
    db_session.add(DocumentGroupMember(
        document_id=shared.id,
        group_id=operations.id,
    ))
    await db_session.flush()

    preview = await plan(
        db_session,
        workspace=scenario["workspace"],
        payload=knowledge_payload,
    )
    support_item = next(
        item
        for item in preview["items"]
        if item["kind"] == "knowledge_document"
        and item["knowledge_pack_slug"] == "support"
    )
    assert support_item["action"] == UpgradeAction.UPDATE.value

    await apply(
        db_session,
        workspace=scenario["workspace"],
        payload=knowledge_payload,
    )
    memberships = list((await db_session.execute(
        select(DocumentGroupMember).where(
            DocumentGroupMember.document_id == shared.id,
        )
    )).scalars().all())
    assert {membership.group_id for membership in memberships} == {
        operations.id,
        support.id,
    }

    await revert(db_session, workspace=scenario["workspace"])
    assert await db_session.get(Document, shared.id) is not None
    memberships = list((await db_session.execute(
        select(DocumentGroupMember).where(
            DocumentGroupMember.document_id == shared.id,
        )
    )).scalars().all())
    assert [membership.group_id for membership in memberships] == [operations.id]


@pytest.mark.asyncio
async def test_reverting_restores_the_installed_version(db_session, scenario, payload):
    from packages.core.blueprints.freshness import BLUEPRINT_VERSION_KEY

    ws = scenario["workspace"]
    settings = dict(ws.settings)
    settings[BLUEPRINT_SETTINGS_KEY] = {
        **settings[BLUEPRINT_SETTINGS_KEY], BLUEPRINT_VERSION_KEY: "1.0.0",
    }
    ws.settings = settings

    await apply(db_session, workspace=ws, payload=payload, current_version="1.0.2")
    await revert(db_session, workspace=ws)

    record = ws.settings[BLUEPRINT_SETTINGS_KEY]
    assert record[BLUEPRINT_VERSION_KEY] == "1.0.0"
    assert blueprint_freshness(
        ws.settings, payload, current_version="1.0.2",
    ) is BlueprintFreshness.UPDATE_AVAILABLE


@pytest.mark.asyncio
async def test_reverting_a_preversion_restore_point_falls_back_to_fingerprints(
    db_session, scenario, payload,
):
    """A restore point written before versions were recorded has no
    from_version. Revert must not leave the post-apply version behind — that
    would claim currency over content it just rolled back."""
    from packages.core.blueprints.freshness import BLUEPRINT_VERSION_KEY
    from packages.core.blueprints.upgrade import RESTORE_POINT_KEY as _RP

    ws = scenario["workspace"]
    await apply(db_session, workspace=ws, payload=payload, current_version="1.0.2")

    # Simulate the old restore point shape: no from_version recorded.
    settings = dict(ws.settings)
    record = dict(settings[BLUEPRINT_SETTINGS_KEY])
    point = dict(record[_RP])
    point.pop("from_version", None)
    record[_RP] = point
    settings[BLUEPRINT_SETTINGS_KEY] = record
    ws.settings = settings

    await revert(db_session, workspace=ws)

    record = ws.settings[BLUEPRINT_SETTINGS_KEY]
    assert BLUEPRINT_VERSION_KEY not in record
    assert blueprint_freshness(
        ws.settings, payload, current_version="1.0.2",
    ) is BlueprintFreshness.UPDATE_AVAILABLE
