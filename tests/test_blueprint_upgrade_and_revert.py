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

import pytest
from sqlalchemy import select

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    CONTENT_FINGERPRINT_KEY,
    BlueprintFreshness,
    blueprint_content_fingerprint,
    blueprint_freshness,
)
from packages.core.blueprints.solo_company import get_solo_company_blueprint
from packages.core.blueprints.upgrade import (
    RESTORE_POINT_KEY,
    UpgradeAction,
    apply,
    plan,
    revert,
)
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.skill import Skill
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import Workspace

SLUG = "solo-faceless-stickman-studio-v1"
STALE_PROMPT = "You are a professional AI video producer specialising in stickman videos."


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
            }
        },
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


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
    db_session.add(WorkflowBinding(
        entity_id=entity_id,
        workspace_id=ws.id,
        workflow_id=definition.id,
        name=workflow_spec["name"],
        trigger_type=workflow_spec["trigger_type"],
        config={
            "source": "blueprint",
            "workspace_blueprint_workflow_slug": workflow_spec["slug"],
        },
        enabled=True,
        status="active",
    ))
    await db_session.flush()
    return {"entity_id": entity_id, "workspace": ws, "skill": skill, "older": older}


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
    binding = WorkflowBinding(
        entity_id=entity_id,
        workspace_id=workspace.id,
        workflow_id=definition.id,
        name=current_spec["name"],
        trigger_type="mcp",
        config={
            "source": "blueprint",
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
    }


@pytest.mark.asyncio
async def test_an_orphaned_duplicate_blueprint_id_falls_back_to_the_platform_row(
    db_session, opc_payload,
):
    """Marketplace deduplication must not strand existing workspaces."""
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

    assert resolved[workspace.id] == (opc_payload, "1.0.7", stable_id)


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

    result = await plan(db_session, workspace=workspace, payload=opc_payload)

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
    assert installation.template_id == f"builtin:{SLUG}"
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
async def test_applying_updates_blueprint_workflow_inputs(
    db_session, workflow_scenario, opc_payload,
):
    await apply(
        db_session,
        workspace=workflow_scenario["workspace"],
        payload=opc_payload,
    )
    start = next(
        step for step in workflow_scenario["definition"].steps
        if step["type"] == "trigger"
    )
    run_inputs = start["config"]["run_inputs"]
    assert run_inputs[0]["hidden"] is True
    topic_input = next(item for item in run_inputs if item["key"] == "topic_brief")
    assert topic_input["prefill"]["workflow_slug"] == "opc-generate-topic-from-knowledge-v1"


@pytest.mark.asyncio
async def test_applying_leaves_an_edited_item_alone(db_session, scenario, payload):
    scenario["skill"].revision = 4
    await db_session.flush()

    result = await apply(db_session, workspace=scenario["workspace"], payload=payload)
    assert scenario["skill"].system_prompt == STALE_PROMPT
    assert result["updated"] == []
    assert [i["slug"] for i in result["kept_yours"]] == [scenario["skill"].slug]


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
    assert workflow_scenario["definition"].steps != workflow_scenario["older_steps"]

    await revert(db_session, workspace=workflow_scenario["workspace"])
    assert workflow_scenario["definition"].steps == workflow_scenario["older_steps"]


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
        **settings[BLUEPRINT_SETTINGS_KEY], BLUEPRINT_VERSION_KEY: "1.0.1",
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
    )
    assert len(result["updated"]) == len(pack["starter_documents"])
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
