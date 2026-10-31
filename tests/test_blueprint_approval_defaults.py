"""Every Blueprint creation path materializes reviewable default governance."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import select

from packages.core.blueprints.configuration_upgrade import (
    _targets,
    apply_configuration,
    capture_configuration,
    plan_configuration,
)
from packages.core.blueprints.installer import InstallMode, author_hard_blocks, install_blueprint
from packages.core.governance.policy import policy_from_dict, policy_to_dict
from packages.core.governance.approval_policy import ActionFacts, ApprovalPolicyBinding, ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import account_defaults, build_policy_chain
from packages.core.governance.approval_projection import policy_override, replace_workspace_rules
from packages.core.models.user import Entity, User
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.governance import GovernancePolicy
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_setup_service import (
    DEFAULT_FIELDS,
    _build_governance_policy_from_rules,
)


async def _approval_entity(db_session) -> str:
    entity_id = generate_ulid()
    db_session.add(Entity(id=entity_id, name="Blueprint approval test account",
                          settings={"approval_defaults": account_defaults()}))
    await db_session.flush()
    return entity_id


def _payload(source: str = "both") -> dict:
    rules = []
    governance = {}
    if source in {"both", "rule_only", "category_only"}:
        rule = {
            "rule_key": "author_review_default",
            "rule_type": "draft_only",
            "description": "Review the content before acting.",
            "approval_selectors": [{"kind": "category", "category": "publishing"}],
        }
        if source != "category_only":
            rule["approval_selectors"] = [{"kind": "action", "action_key": "social_post.publish"}]
        rules.append(rule)
    if source in {"both", "policy_only"}:
        governance = {
            "approval": {"mode": None, "rules": [{"id": "author-publish", "effect": "deny",
                "selector": {"kind": "action", "action_key": "social_post.publish"}}]},
        }
    return {
        "manifest": {
            "blueprint_version": "1.1", "title": "Approval defaults",
            "kind": "operations", "description": "Review before action.",
        },
        "contract": {
            "variables": [], "channels": [], "sessions": [],
            "requires": {"manor_min_version": None, "tools": [], "mcp_servers": [], "skills": [], "agents": []},
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {"rules": rules}, "strategist": None,
            "prompts": [], "subscriptions": [], "scheduled_jobs": [],
            "workflows": [], "goals": [], "task_categories": [],
            "custom_fields": [], "sla_policies": [], "escalation_rules": [],
        },
        "policy": {"governance": governance, "post_install_checks": [], "expected_baseline": None},
    }


def _decision(policy, action_key="social_post.publish", category="publishing"):
    chain = build_policy_chain(scope=ApprovalScope(kind="workspace", id="w", entity_id="e"),
        root=account_defaults(), local={"revision": 1, **ApprovalPolicyBinding(
            root_entity_id="e", policy=policy_override(policy)).model_dump()})
    return evaluate_policy(chain, ActionFacts(action_key=action_key, category=category, impact="external"))


def _assert_reviewable(policy, rules):
    assert all(rule.effect != "deny" for rule in policy_override(policy).rules)
    assert all(rule["rule_type"] == "approval_required" for rule in rules if isinstance(rule, dict))
    for candidate in (policy, _build_governance_policy_from_rules(rules, policy_to_dict(policy))):
        assert _decision(candidate).outcome == "ask"


def test_automatic_is_the_default_install_choice():
    from apps.api.routers.blueprints import InstallBlueprintRequest
    from packages.core.governance.presets import list_presets

    assert InstallBlueprintRequest().governance_preset == "automatic"
    assert InstallBlueprintRequest(governance_preset="automatic").governance_preset == "automatic"
    assert [preset.key for preset in list_presets()] == ["automatic", "follow_default", "confirm_changes", "risk_based"]


@pytest.mark.parametrize("source", ["both", "rule_only", "policy_only", "category_only"])
@pytest.mark.parametrize("preset", ["automatic", "follow_default", "confirm_changes", "risk_based"])
def test_upgrade_projects_source_rules_and_policy_together(source, preset):
    payload = _payload(source)
    original = copy.deepcopy(payload)
    targets = _targets(payload, "workspace", {"_blueprint": {"governance_preset": preset}})
    rules = targets["workspace_configuration:operating_model/rules"]["source_content"]["value"]
    policy = policy_from_dict(targets["governance:policy"]["source_content"])
    _assert_reviewable(policy, rules)
    assert payload == original


@pytest.mark.asyncio
async def test_follow_default_install_and_upgrade_never_copy_template_consent(db_session):
    payload = _payload("policy_only")
    payload["policy"]["governance"]["approval"]["mode"] = "automatic"
    payload["policy"]["governance"]["approval"]["rules"].append({
        "id": "author-grant", "effect": "allow", "selector": {"kind": "category", "category": "files"}})
    installed = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=payload, governance_preset="follow_default")
    workspace = await db_session.get(Workspace, installed.workspace_id)
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == installed.workspace_id))).scalar_one()
    active = policy_override(row.policy)
    assert active.mode is None
    assert not any(rule.effect == "allow" for rule in active.rules)
    assert workspace.settings["_blueprint"]["governance_preset"] == "follow_default"
    newer = copy.deepcopy(payload)
    newer["policy"]["governance"]["approval"]["rules"].append({
        "id": "author-send", "effect": "ask", "selector": {"kind": "action", "action_key": "external_message.send"}})
    desired = policy_from_dict(_targets(newer, installed.workspace_id, workspace.settings)["governance:policy"]["source_content"])
    assert policy_override(desired).mode is None
    assert not any(rule.effect == "allow" for rule in policy_override(desired).rules)
    assert _decision(desired, "external_message.send", "communication").outcome == "ask"


@pytest.mark.parametrize("preset,expected", [("automatic", "automatic"), ("follow_default", None)])
def test_install_choice_applies_after_template_consent_is_removed(preset, expected):
    payload = _payload("none")
    payload["policy"]["governance"] = {"approval": {"mode": "automatic", "rules": [
        {"id": "author-grant", "effect": "allow", "selector": {"kind": "category", "category": "files"}},
    ]}}
    desired = policy_from_dict(_targets(payload, "workspace", {
        "_blueprint": {"governance_preset": preset},
    })["governance:policy"]["source_content"])
    assert policy_override(desired).mode == expected
    assert policy_override(desired).rules == []
    assert _decision(desired).outcome == ("allow" if preset == "automatic" else "ask")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [InstallMode.LIVE, InstallMode.SIMULATE])
async def test_automatic_install_persists_operator_choice_and_upgrade_preserves_it(db_session, mode):
    payload = _payload("none")
    installed = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=payload, mode=mode)
    workspace = await db_session.get(Workspace, installed.workspace_id)
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == installed.workspace_id))).scalar_one()
    assert workspace.settings["_blueprint"]["governance_preset"] == "automatic"
    assert policy_override(row.policy).mode == "automatic"
    assert _decision(policy_from_dict(row.policy)).outcome == "allow"
    baseline = await capture_configuration(db_session, workspace=workspace, payload=payload)
    updated = copy.deepcopy(payload)
    updated["recipe"]["operating_model"]["primary_work"] = "Prepare release files"
    plan = await plan_configuration(db_session, workspace=workspace, payload=updated, baseline=baseline)
    await apply_configuration(db_session, workspace=workspace, payload=updated, plan=plan)
    await db_session.refresh(row)
    assert policy_override(row.policy).mode == "automatic"

    from packages.core.governance.approval_projection import portable_governance
    assert portable_governance(row.policy)["approval"]["mode"] is None


@pytest.mark.parametrize("risk_level", ["low", "medium"])
def test_template_risk_review_survives_follow_default_roundtrip(risk_level):
    from packages.core.governance.approval_projection import (
        import_blueprint_governance, portable_governance,
    )
    from packages.core.governance.defaults import normalize_default_workspace_governance
    from packages.core.governance.presets import apply_preset

    raw = {"max_risk_level": risk_level}
    first_export = None
    for _ in range(3):
        imported, sources = import_blueprint_governance(raw, [])
        sources, installed, _ = normalize_default_workspace_governance(
            sources, apply_preset(imported, "follow_default"),
        )
        override = policy_override(installed)
        assert override.mode is None
        assert installed.max_risk_level == "high"
        assert len(override.rules) == 8
        assert {rule.selector.category for rule in override.rules} == {
            "files", "communication", "publishing", "deletion", "purchases",
            "automation", "workspace", "unknown",
        }
        root = account_defaults()
        root["policy"]["mode"] = "automatic"
        chain = build_policy_chain(
            scope=ApprovalScope(kind="workspace", id="w", entity_id="e"), root=root,
            local={"revision": 1, **ApprovalPolicyBinding(root_entity_id="e", policy=override).model_dump()},
        )
        for rule in override.rules:
            assert evaluate_policy(chain, ActionFacts(
                action_key="tool.change", category=rule.selector.category, impact="reversible",
            )).outcome == "ask"
        assert evaluate_policy(chain, ActionFacts(
            action_key="tool.read", category="read", impact="read",
        )).outcome == "allow"
        # Re-normalizing the same configuration never duplicates requirements.
        assert normalize_default_workspace_governance(sources, installed)[1] == installed
        raw = portable_governance(policy_to_dict(installed))
        if first_export is None:
            first_export = copy.deepcopy(raw)
        assert raw == first_export

    # Explicit operator hard-block installation keeps the original risk cap.
    imported, sources = import_blueprint_governance(
        {"max_risk_level": risk_level}, [], preserve_hard_blocks=True,
    )
    _, preserved, _ = normalize_default_workspace_governance(
        sources, imported, preserve_hard_blocks=True,
    )
    assert preserved.max_risk_level == risk_level
    assert policy_override(preserved).rules == []


def test_upgrade_preserves_explicit_author_hard_block_opt_in():
    payload = _payload()
    targets = _targets(payload, "workspace", {"_blueprint": {"author_hard_blocks_installed": True}})
    rules = targets["workspace_configuration:operating_model/rules"]["source_content"]["value"]
    assert rules == payload["recipe"]["operating_model"]["rules"]
    policy = policy_from_dict(targets["governance:policy"]["source_content"])
    assert _decision(policy).outcome == "deny"


@pytest.mark.parametrize("opt_in", [False, True])
def test_upgrade_preserves_legacy_blueprint_text_rules(opt_in):
    from packages.core.blueprints.payload import validate_payload

    payload = _payload("policy_only")
    payload["recipe"]["operating_model"]["rules"] = [
        "One primary audience and one clear takeaway per video.",
        "Never publish without review.",
    ]
    original = copy.deepcopy(payload)
    validate_payload(payload)
    targets = _targets(payload, "workspace", {"_blueprint": {"author_hard_blocks_installed": opt_in}})
    rules = targets["workspace_configuration:operating_model/rules"]["source_content"]["value"]
    assert rules == original["recipe"]["operating_model"]["rules"]
    policy = policy_from_dict(targets["governance:policy"]["source_content"])
    assert _decision(policy).outcome == ("deny" if opt_in else "ask")
    assert payload == original


@pytest.mark.parametrize("source", ["rule_only", "category_only"])
def test_setup_preview_includes_rule_only_author_blocks(source):
    assert author_hard_blocks(_payload(source)) == (["publishing"] if source == "category_only" else ["social_post.publish"])


@pytest.mark.asyncio
async def test_installer_agent_preview_and_persisted_policy_are_identical(db_session, monkeypatch):
    from packages.core.blueprints import installer

    payload = _payload("category_only")
    payload["embedded"]["agents"] = [{
        "slug": "reviewer", "name": "Reviewer", "system_prompt": "Review before acting.",
    }]
    captured = []

    async def capture_agent(_db, **kwargs):
        captured.append(copy.deepcopy(kwargs["final_policy"]))
        return None

    monkeypatch.setattr(installer, "_install_embedded_agent", capture_agent)
    result = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=payload)
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == result.workspace_id,
    ))).scalar_one()
    assert [policy_override(policy) for policy in captured] == [policy_override(row.policy)]
    assert _decision(captured[0]).outcome == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", [False, True])
async def test_author_payload_cannot_install_unattended_run_consent(db_session, opt_in):
    payload = _payload("policy_only")
    payload["policy"]["governance"]["approval"]["mode"] = "automatic"
    result = await install_blueprint(
        db_session, entity_id=await _approval_entity(db_session), payload=payload,
        install_author_hard_blocks=opt_in, governance_preset="follow_default",
    )
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == result.workspace_id,
    ))).scalar_one()
    assert policy_override(row.policy).mode is None
    assert payload["policy"]["governance"]["approval"]["mode"] == "automatic"


@pytest.mark.asyncio
async def test_configuration_upgrade_keeps_later_manual_hard_blocks(db_session):
    payload = _payload()
    installed = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=payload)
    workspace = await db_session.get(Workspace, installed.workspace_id)
    baseline = await capture_configuration(db_session, workspace=workspace, payload=payload)
    manual_rule = {"rule_key": "manual_email_ban", "rule_type": "deny", "approval_selectors": [{"kind": "action", "action_key": "external_message.send"}]}
    workspace.operating_model = {**workspace.operating_model, "rules": [*workspace.operating_model["rules"], manual_rule]}
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == workspace.id,
    ))).scalar_one()
    row.policy = policy_to_dict(replace_workspace_rules(policy_from_dict(row.policy), workspace.operating_model["rules"]))
    await db_session.flush()
    newer = copy.deepcopy(payload)
    newer["recipe"]["operating_model"]["rules"].append({
        "rule_key": "new_author_default", "rule_type": "deny", "approval_selectors": [{"kind": "action", "action_key": "workspace.file.delete"}],
    })
    planned = await plan_configuration(db_session, workspace=workspace, payload=newer, baseline=baseline)
    rule_item = next(item for item in planned["items"] if item["key"] == "workspace_configuration:operating_model/rules")
    governance_item = next(item for item in planned["items"] if item["key"] == "governance:policy")
    assert rule_item["action"] == governance_item["action"] == "keep_yours"
    await apply_configuration(
        db_session, workspace=workspace, payload=newer, plan=planned,
        choices={item["key"]: "keep_yours" if item["conflict"] else "use_blueprint" for item in planned["items"]},
    )
    assert workspace.operating_model["rules"][-1] == manual_rule
    await db_session.refresh(row)
    assert _decision(policy_from_dict(row.policy), "external_message.send", "communication").outcome == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["both", "rule_only", "policy_only", "category_only"])
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_installer_materializes_default_rules_and_policy_together(db_session, source, mode):
    payload = _payload(source)
    original = copy.deepcopy(payload)
    result = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=payload, mode=mode)
    workspace = await db_session.get(Workspace, result.workspace_id)
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == workspace.id,
    ))).scalar_one()
    _assert_reviewable(policy_from_dict(row.policy), workspace.operating_model["rules"])
    assert workspace.settings["_blueprint"]["author_hard_blocks_installed"] is False
    assert payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_text_rules_survive_install_and_configuration_upgrade(db_session, mixed, mode):
    payload = _payload("both" if mixed else "policy_only")
    payload["recipe"]["operating_model"]["rules"].insert(0, "One primary audience per video.")
    original = copy.deepcopy(payload)
    installed = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=payload, mode=mode)
    workspace = await db_session.get(Workspace, installed.workspace_id)
    assert workspace.operating_model["rules"][0] == original["recipe"]["operating_model"]["rules"][0]
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == workspace.id,
    ))).scalar_one()
    _assert_reviewable(
        policy_from_dict(row.policy),
        [rule for rule in workspace.operating_model["rules"] if isinstance(rule, dict)],
    )
    baseline = await capture_configuration(db_session, workspace=workspace, payload=payload)
    newer = copy.deepcopy(payload)
    newer["recipe"]["operating_model"]["rules"][0] = "One clear takeaway per video."
    newer["policy"]["governance"]["approval"]["rules"].append({"id": "new-author", "effect": "deny", "selector": {"kind": "action", "action_key": "external_message.send"}})
    newer_original = copy.deepcopy(newer)
    planned = await plan_configuration(db_session, workspace=workspace, payload=newer, baseline=baseline)
    await apply_configuration(
        db_session, workspace=workspace, payload=newer, plan=planned,
        choices={item["key"]: "use_blueprint" for item in planned["items"]},
    )
    assert workspace.operating_model["rules"][0] == "One clear takeaway per video."
    await db_session.refresh(row)
    policy = policy_from_dict(row.policy)
    _assert_reviewable(policy, [rule for rule in workspace.operating_model["rules"] if isinstance(rule, dict)])
    assert _decision(policy, "external_message.send", "communication").outcome == "ask"
    assert payload == original
    assert newer == newer_original


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["both", "rule_only", "policy_only", "category_only", "empty"])
async def test_draft_application_uses_same_default_projection(db_session, monkeypatch, source):
    from packages.core.services import workspace_draft_service

    payload = _payload(source)
    original = copy.deepcopy(payload)
    entity_id, user_id = generate_ulid(), generate_ulid()
    blueprint = WorkspaceBlueprint(
        id=generate_ulid(), entity_id=entity_id, slug=f"approval-{entity_id}",
        title="Approval defaults", payload=payload, payload_version="1.1", status="published",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, fields={
            **copy.deepcopy(DEFAULT_FIELDS),
            "rules": copy.deepcopy(payload["recipe"]["operating_model"]["rules"]),
            "_explicit_hard_block_rules": {"author_review_default": "old-confirmation"},
        },
        messages=[], missing=[], ready=True, status="ready",
    )
    db_session.add_all([
        Entity(id=entity_id, name="Approval account", settings={"approval_defaults": account_defaults()}),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="unused"),
        blueprint, draft,
    ])
    await db_session.flush()

    async def verified(*_args, **_kwargs):
        return True

    monkeypatch.setattr(workspace_draft_service, "_refresh_missing_from_lint", verified)
    applied = await workspace_draft_service.apply_blueprint(
        db_session, draft_id=draft.id, entity_id=entity_id,
        blueprint_id=blueprint.id, user_id=user_id,
    )
    rules = applied.fields.get("rules") or []
    if rules:
        assert "_explicit_hard_block_rules" not in applied.fields
    policy = _build_governance_policy_from_rules(rules, applied.fields.get("governance_policy"))
    if source == "empty":
        assert policy is None
    else:
        _assert_reviewable(policy, rules)
    assert applied.fields["_blueprint_source_payload"] == original
    assert blueprint.payload == original

    applied.fields = {**applied.fields, "heartbeat_enabled": False}
    applied.ready = True
    applied.status = "ready"
    workspace_id, _ = await workspace_draft_service.finalize_draft(
        db_session, draft_id=applied.id, entity_id=entity_id, user_id=user_id,
    )
    workspace = await db_session.get(Workspace, workspace_id)
    from packages.core.governance import get_policy

    installed_policy = await get_policy(db_session, workspace_id)
    if source == "empty":
        assert policy_override(installed_policy).mode is None
        assert policy_override(installed_policy).rules == []
    else:
        _assert_reviewable(installed_policy, workspace.operating_model["rules"])
    assert workspace.settings["_blueprint"]["author_hard_blocks_installed"] is False


@pytest.mark.asyncio
async def test_default_governance_roundtrips_and_survives_live_promotion(db_session):
    from packages.core.blueprints.exporter import export_workspace
    from packages.core.blueprints.promote import promote_workspace

    first = await install_blueprint(db_session, entity_id=await _approval_entity(db_session), payload=_payload())
    promoted = await promote_workspace(db_session, first.workspace_id)
    assert promoted.promoted is True
    exported = await export_workspace(db_session, first.workspace_id, title="Approval defaults")
    _assert_reviewable(
        policy_from_dict(exported["policy"]["governance"]),
        exported["recipe"]["operating_model"]["rules"],
    )
    second = await install_blueprint(
        db_session, entity_id=await _approval_entity(db_session), payload=exported, mode=InstallMode.LIVE,
    )
    roundtripped = await export_workspace(db_session, second.workspace_id, title="Approval defaults")
    assert roundtripped["policy"]["governance"] == exported["policy"]["governance"]
    assert roundtripped["recipe"]["operating_model"]["rules"] == exported["recipe"]["operating_model"]["rules"]


def test_no_builtin_blueprint_imports_as_a_review_of_everything():
    """A selector the import cannot represent (a wildcard, a capability), or a risk
    ceiling below high, turns into an ask rule on every category: file writes then
    wait for a person even in automatic mode, and background work stalls on them."""
    import json
    from pathlib import Path
    from typing import get_args

    from packages.core.governance.approval_policy import ApprovalCategory
    from packages.core.governance.approval_policy_migration import _rule_id, normalize_legacy_workspace_policy

    blanket = {_rule_id("template_review", category) for category in get_args(ApprovalCategory)}
    configs = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs"
    offenders = {}
    for path in sorted(configs.rglob("*.json")):
        governance = (json.loads(path.read_text()).get("policy") or {}).get("governance")
        if not isinstance(governance, dict):
            continue
        rules = normalize_legacy_workspace_policy(governance, safe_import=True)["approval"]["rules"]
        if blanket & {rule["id"] for rule in rules}:
            offenders[path.name] = governance
        # A risk ceiling below high installs as author-risk-review asks on every
        # category: the same review of everything, and it reached a metric
        # assessment's record writes.
        if governance.get("max_risk_level", "high") != "high":
            offenders[path.name] = {"max_risk_level": governance["max_risk_level"]}
    assert not offenders, offenders
