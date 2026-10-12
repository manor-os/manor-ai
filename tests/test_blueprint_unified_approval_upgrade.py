"""Approval upgrade/revert snapshots keep source ownership and local consent."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.blueprints import configuration_upgrade as upgrade
from packages.core.governance.approval_policy import ApprovalPolicyBinding, ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approval_projection import policy_override, portable_governance
from packages.core.governance.policy import policy_from_dict, policy_to_dict

pytestmark = pytest.mark.unit


def source(category="publishing"):
    return {"rule_key": "publication-review", "rule_type": "approval_required", "approval_selectors": [{"kind": "category", "category": category}]}


def payload(category="publishing"):
    return {"manifest": {"kind": "studio"}, "contract": {}, "embedded": {}, "policy": {},
            "recipe": {"operating_model": {"kind": "studio", "context": "Original", "primary_work": "Make videos", "heartbeat_enabled": False, "rules": [source(category)]}}}


def manual_rule():
    return ApprovalRule(id="operator-exception", effect="allow", selector={"kind": "resource", "action_key": "workspace.file.modify", "account_id": "my-account", "resource_kind": "file", "resource_ids": ["private-document"]})


class MemoryDB:
    def __init__(self, row):
        self.row = row
        self.flush = AsyncMock()

    async def execute(self, _query):
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


async def installed(monkeypatch):
    workspace = SimpleNamespace(id="workspace", entity_id="team", name="Studio", kind="studio", deleted_at=None,
        operating_context="Original", primary_work="Make videos", heartbeat_enabled=False, heartbeat_cadence=None,
        operating_model={"rules": [source()]}, operation_revision=1, settings={"_blueprint": {"governance_preset": "follow_default"}})
    initial = upgrade._targets(payload(), workspace.id, workspace.settings)["governance:policy"]["source_content"]
    policy = policy_from_dict(initial)
    policy.approval = ApprovalPolicyBinding(root_entity_id=workspace.entity_id, policy=policy_override(policy))
    row = SimpleNamespace(policy=policy_to_dict(policy), revision=1)
    db = MemoryDB(row)
    monkeypatch.setattr(upgrade, "_lock_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(upgrade, "_schedule_snapshots", AsyncMock(return_value={}))
    writes = []

    async def persist(_db, *, policy, **kwargs):
        # Mirror the real writer's server-owned root binding while exercising
        # the production plan/apply/verification/restore pipeline above it.
        writes.append(copy.deepcopy(policy))
        policy.approval = ApprovalPolicyBinding(root_entity_id=workspace.entity_id, policy=policy_override(policy))
        row.policy = policy_to_dict(policy)
        row.revision += 1
        return row

    monkeypatch.setattr("packages.core.governance.service.update_policy", persist)
    baseline = await upgrade.capture_configuration(db, workspace=workspace, payload=payload())
    return db, workspace, row, baseline, writes


@pytest.mark.asyncio
async def test_installed_binding_is_not_a_blueprint_conflict(monkeypatch):
    db, workspace, row, baseline, _ = await installed(monkeypatch)
    plan = await upgrade.plan_configuration(db, workspace=workspace, payload=payload(), baseline=baseline)
    governance = next(item for item in plan["items"] if item["kind"] == "governance")
    assert governance["action"] == "unchanged"
    assert governance["current_content"] == governance["new_content"]
    assert "root_entity_id" not in str(governance)
    assert row.policy["approval"]["root_entity_id"] == "team"


@pytest.mark.asyncio
async def test_source_upgrade_and_revert_preserve_user_mode_exact_rules_and_binding(monkeypatch):
    db, workspace, row, baseline, writes = await installed(monkeypatch)
    local = policy_override(row.policy)
    local.mode = "automatic"
    local.rules.append(manual_rule())
    row.policy["approval"]["policy"] = local.model_dump(mode="json")
    row.revision += 1
    before = copy.deepcopy(row.policy)
    incoming = payload("communication")
    plan = await upgrade.plan_configuration(db, workspace=workspace, payload=incoming, baseline=baseline)
    governance = next(item for item in plan["items"] if item["kind"] == "governance")
    assert governance["action"] == "update"
    assert governance["new_content"]["approval"]["mode"] == "automatic"
    assert "private-document" in str(governance["new_content"])
    assert "my-account" in str(governance["new_content"])

    result = await upgrade.apply_configuration(db, workspace=workspace, payload=incoming, plan=plan)
    local = policy_override(row.policy)
    assert local.mode == "automatic"
    assert manual_rule() in local.rules
    assert [rule.selector.category for rule in local.rules if rule.source_kind == "workspace_rule"] == ["communication"]
    assert row.policy["approval"]["root_entity_id"] == "team"
    assert len(writes) == 1
    # New provenance stores template content separately from preserved locals.
    entry = result["baseline"]["entries"]["governance:policy"]
    assert entry["blueprint_content"]["approval"]["mode"] is None
    assert "private-document" not in str(entry["blueprint_content"])
    unchanged = await upgrade.plan_configuration(db, workspace=workspace, payload=incoming, baseline=result["baseline"])
    assert all(item["action"] == "unchanged" for item in unchanged["items"])

    await upgrade.revert_configuration(db, workspace=workspace, restore=result["restore"])
    assert upgrade._governance_content(row.policy) == upgrade._governance_content(before)
    assert row.policy["approval"]["root_entity_id"] == "team"
    assert workspace.operating_model["rules"] == [source()]
    # Public export still excludes reusable permission/local target bindings.
    exported = portable_governance(row.policy)
    assert exported["approval"]["mode"] is None
    assert "private-document" not in str(exported)
    assert "my-account" not in str(exported)


def test_template_rule_update_cannot_overwrite_manually_edited_exception():
    old = {"approval": ApprovalPolicyOverride(rules=[ApprovalRule(id="portable-rule", effect="ask", selector={"kind": "category", "category": "files"})]).model_dump()}
    current = copy.deepcopy(old)
    current["approval"]["rules"][0]["effect"] = "deny"
    incoming = copy.deepcopy(old)
    incoming["approval"]["rules"][0]["selector"]["category"] = "communication"
    merged = upgrade._merge_governance_upgrade(current, incoming, old)
    assert merged["approval"]["rules"] == current["approval"]["rules"]
    # An unchanged template-owned rule may still be deliberately updated.
    replaced = upgrade._merge_governance_upgrade(old, incoming, old)
    assert replaced["approval"]["rules"] == incoming["approval"]["rules"]


@pytest.mark.asyncio
async def test_rule_or_mode_change_after_review_aborts_before_governance_write(monkeypatch):
    db, workspace, row, baseline, writes = await installed(monkeypatch)
    plan = await upgrade.plan_configuration(db, workspace=workspace, payload=payload("communication"), baseline=baseline)
    row.policy["approval"]["policy"]["mode"] = "automatic"
    row.revision += 1
    with pytest.raises(upgrade.ConfigurationUpgradeError, match="changed after review"):
        await upgrade.apply_configuration(db, workspace=workspace, payload=payload("communication"), plan=plan)
    assert writes == []


@pytest.mark.asyncio
async def test_wrong_or_missing_root_binding_fails_closed(monkeypatch):
    db, workspace, row, baseline, _ = await installed(monkeypatch)
    row.policy["approval"]["root_entity_id"] = "another-team"
    with pytest.raises(upgrade.ConfigurationUpgradeError, match="does not match"):
        await upgrade.plan_configuration(db, workspace=workspace, payload=payload(), baseline=baseline)
    db.row = None
    with pytest.raises(upgrade.ConfigurationUpgradeError, match="migration"):
        await upgrade.plan_configuration(db, workspace=workspace, payload=payload(), baseline=baseline)


@pytest.mark.asyncio
@pytest.mark.parametrize("keep", ["governance:policy", "workspace_configuration:operating_model/rules"])
async def test_derived_policy_follows_the_selected_business_source(monkeypatch, keep):
    db, workspace, row, baseline, _ = await installed(monkeypatch)
    local = policy_override(row.policy)
    local.mode = "automatic"
    local.rules.append(manual_rule())
    row.policy["approval"]["policy"] = local.model_dump(mode="json")
    incoming = payload("communication")
    plan = await upgrade.plan_configuration(db, workspace=workspace, payload=incoming, baseline=baseline)
    choices = {keep: "keep_yours"}
    preview = await upgrade.effective_configuration_policy(db, workspace=workspace, payload=incoming,
        plan=plan, choices=choices)
    applied = await upgrade.apply_configuration(db, workspace=workspace, payload=incoming, plan=plan, choices=choices)
    category = "publishing" if keep.endswith("/rules") else "communication"
    assert workspace.operating_model["rules"] == [source(category)]
    assert [rule.selector.category for rule in policy_override(row.policy).rules if rule.source_kind == "workspace_rule"] == [category]
    assert upgrade._governance_content(row.policy) == upgrade._governance_content(policy_to_dict(preview))
    assert policy_override(row.policy).mode == "automatic"
    assert manual_rule() in policy_override(row.policy).rules
    next_plan = await upgrade.plan_configuration(db, workspace=workspace, payload=incoming, baseline=applied["baseline"])
    assert all(item["action"] == "unchanged" for item in next_plan["items"])
