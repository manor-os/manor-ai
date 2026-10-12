"""Creative Blueprint upgrades change configuration, not production history."""
from copy import deepcopy
import difflib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from packages.core.blueprints import component_upgrade, reconfigure
from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.governance.approval_projection import import_blueprint_governance, portable_governance
from packages.core.governance.defaults import normalize_default_workspace_governance
from packages.core.governance.policy import policy_to_dict
from packages.core.governance.presets import apply_preset
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.user import Entity
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Workspace
from scripts.blueprints.creative_studios import STUDIOS
from test_practical_workspace_blueprints import _checker


ROOT = Path(__file__).resolve().parents[1]


def _expected_installed_export(source):
    """Only the source crosses the import boundary; compare actual output intact."""
    expected = deepcopy(source)
    operating_model = expected["recipe"]["operating_model"]
    policy, rules = import_blueprint_governance(
        expected["policy"]["governance"], operating_model.get("rules") or [],
    )
    rules, policy, _ = normalize_default_workspace_governance(
        rules, apply_preset(policy, "follow_default"),
    )
    expected["policy"]["governance"] = portable_governance(policy_to_dict(policy))
    if "rules" in operating_model or rules:
        operating_model["rules"] = rules
    return expected


def _assert_runtime_parity(expected, actual):
    expected = _expected_installed_export(expected)
    # These frozen legacy studios must import as review requirements, never as
    # operator consent. Keep an independent assertion on the actual export.
    # Reads are allowed by every mode, so the review never covers them.
    approval = actual["policy"]["governance"]["approval"]
    assert approval["mode"] is None
    assert len(approval["rules"]) == 9
    assert {rule["selector"]["category"] for rule in approval["rules"]} == {
        "browser", "files", "communication", "publishing", "deletion", "purchases",
        "automation", "workspace", "unknown",
    }
    assert all(rule["effect"] == "ask" and rule["selector"]["kind"] == "category"
        and rule["source_kind"] == "user" and rule.get("source_id") is None
        for rule in approval["rules"])
    assert "root_entity_id" not in approval
    checker = _checker("compare_roundtrip")
    left = checker._normalize(checker._runtime_projection(expected))
    right = checker._normalize(checker._runtime_projection(actual))
    difference = "\n".join(difflib.unified_diff(
        json.dumps(left, ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
        json.dumps(right, ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
        fromfile="expected", tofile="actual", lineterm="",
    ))
    assert checker.compare(expected, actual) == [], difference


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
def test_creative_expected_export_projects_legacy_authority_without_mutating_source(spec):
    source = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{spec.slug}.json").read_text())
    original = deepcopy(source)
    _assert_runtime_parity(source, _expected_installed_export(source))
    assert source == original


@pytest.mark.parametrize("change", ["allow", "automatic", "budget", "source_rule"])
def test_creative_runtime_parity_still_rejects_governance_and_source_drift(change):
    source = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{STUDIOS[0].slug}.json").read_text())
    source["recipe"]["operating_model"]["rules"] = [{
        "rule_key": "review-publish", "rule_type": "approval_required",
        "approval_selectors": [{"kind": "action", "action_key": "social_post.publish"}],
    }]
    actual = _expected_installed_export(source)
    if change == "allow":
        actual["policy"]["governance"]["approval"]["rules"][0]["effect"] = "allow"
    elif change == "automatic":
        actual["policy"]["governance"]["approval"]["mode"] = "automatic"
    elif change == "budget":
        actual["policy"]["governance"]["budget_caps_per_kind"] = {"llm_tokens": 100}
    else:
        actual["recipe"]["operating_model"]["rules"][0]["rule_key"] = "different-source"
    with pytest.raises(AssertionError):
        _assert_runtime_parity(source, actual)


@pytest.mark.parametrize("spec", STUDIOS, ids=lambda spec: spec.key)
async def test_reviewed_creative_upgrade_and_undo_preserve_production_history(db_session, spec):
    source = json.loads((ROOT / f"packages/core/blueprints/configs/creative/{spec.slug}.json").read_text())
    entity = Entity(id=generate_ulid(), name="Isolated upgrade fixture", plan_id="plan_enterprise")
    db_session.add(entity)
    await db_session.flush()
    blueprint_id = f"builtin:{spec.slug}"
    installed = await install_blueprint(
        db_session, entity_id=entity.id, payload=source, mode=InstallMode.LIVE,
        blueprint_id=blueprint_id, blueprint_slug=spec.slug, workspace_name="My retained production studio",
    )
    assert not installed.todos
    workspace = await db_session.get(Workspace, installed.workspace_id)
    components = await component_upgrade.capture_components(db_session, workspace=workspace, payload=source)
    component_ids = {key: value["id"] for key, value in components.items()}
    flow_id = next(value["id"] for key, value in components.items() if key.startswith("workflow:"))
    flow = await db_session.get(WorkflowDefinition, flow_id)
    original_flow_version = source["recipe"]["workflows"][0]["version"]
    assert flow.version == original_flow_version
    history = WorkflowRun(
        entity_id=entity.id, workspace_id=workspace.id, workflow_id=flow_id, status="completed",
        definition_snapshot={"steps": deepcopy(source["recipe"]["workflows"][0]["steps"])},
        step_results={"review": {"choice": "accept"}, "done": {"receipt": "historical-verification.json"}},
    )
    # Persistence test only: this row stands for a retained receipt, not generated media.
    receipt = Document(
        entity_id=entity.id, name="historical-verification.json", file_type="json",
        metadata_={"workspace_id": workspace.id, "test_fixture": True, "accepted": True},
    )
    db_session.add_all([history, receipt])
    await db_session.flush()
    historical_snapshot, historical_results = deepcopy(history.definition_snapshot), deepcopy(history.step_results)
    receipt_metadata = deepcopy(receipt.metadata_)
    updated = deepcopy(source)
    updated["embedded"]["agents"][0]["system_prompt"] += "\nRetain the operator's chosen terminology in revisions."
    produce = next(step for step in updated["recipe"]["workflows"][0]["steps"] if step["id"] == "produce")
    produce["config"]["input"] += "\nRetain the operator's chosen terminology in revisions."
    reviewed = await reconfigure.plan(
        db_session, workspace=workspace, payload=updated, source_payload=updated,
        current_version="test-creative-upgrade", source_blueprint_id=blueprint_id,
    )
    assert not reviewed["active_runs"]
    assert {item["kind"] for item in reviewed["items"] if item["action"] != "unchanged"} >= {"agent", "workflow"}
    result = await reconfigure.apply(
        db_session, workspace=workspace, payload=updated, source_payload=updated,
        current_version="test-creative-upgrade", source_blueprint_id=blueprint_id,
        expected_blueprint_fingerprint=reviewed["blueprint_fingerprint"],
        expected_plan_fingerprint=reviewed["plan_fingerprint"],
        configuration_resolutions=[{"key": item["key"], "resolution": "use_blueprint"} for item in reviewed["items"]],
        by_user_id=None, actor=None,
    )
    assert result["synchronization_status"] == "current"
    await db_session.refresh(flow)
    # Editing a graph advances its runtime version even when the source
    # Blueprint retains the author's original portable version label.
    assert flow.version == original_flow_version + 1
    exported = await export_workspace(db_session, workspace.id, title=spec.title,
                                      context=ExportContext(include_knowledge_packs=False))
    expected_updated = deepcopy(updated)
    expected_updated["recipe"]["workflows"][0]["version"] = original_flow_version + 1
    _assert_runtime_parity(expected_updated, exported)
    current = await component_upgrade.capture_components(db_session, workspace=workspace, payload=updated)
    assert {key: value["id"] for key, value in current.items()} == component_ids
    await db_session.refresh(history)
    await db_session.refresh(receipt)
    assert history.status == "completed"
    assert history.definition_snapshot == historical_snapshot and history.step_results == historical_results
    assert receipt.metadata_ == receipt_metadata and receipt.is_trashed is False
    await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    await db_session.refresh(flow)
    assert flow.version == original_flow_version
    reverted = await export_workspace(db_session, workspace.id, title=spec.title,
                                      context=ExportContext(include_knowledge_packs=False))
    _assert_runtime_parity(source, reverted)
    await db_session.refresh(history)
    await db_session.refresh(receipt)
    assert history.status == "completed"
    assert history.definition_snapshot == historical_snapshot and history.step_results == historical_results
    assert receipt.metadata_ == receipt_metadata and receipt.is_trashed is False
    assert workspace.id == installed.workspace_id and workspace.name == "My retained production studio"
    assert (await db_session.execute(select(Workspace.id).where(Workspace.entity_id == entity.id))).scalars().all() == [workspace.id]
