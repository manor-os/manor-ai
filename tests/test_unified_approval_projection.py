"""Configuration sources preserve user exceptions and portable boundaries."""
import pytest

from packages.core.governance.approval_policy import ApprovalPolicyBinding, ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approval_projection import (
    compile_workspace_rules, import_blueprint_governance, policy_override,
    portable_governance, replace_workspace_rules,
)
from packages.core.governance.defaults import normalize_default_workspace_governance
from packages.core.governance.policy import WorkspacePolicy, policy_to_dict
from packages.core.services.workspace_operation_service import _resolved_governance_policy

pytestmark = pytest.mark.unit


def user_rule(effect="allow"):
    return ApprovalRule(id="my-rule", effect=effect, selector={"kind": "action", "action_key": "workspace.file.modify"})


def source(key="review", effect="approval_required"):
    return {"rule_key": key, "rule_type": effect, "approval_selectors": [{"kind": "category", "category": "publishing"}]}


def test_source_rebuild_preserves_user_mode_rule_and_account_binding():
    original = WorkspacePolicy(approval=ApprovalPolicyBinding(root_entity_id="team", policy=ApprovalPolicyOverride(mode="automatic", rules=[user_rule()])))
    compiled = replace_workspace_rules(original, [source()])
    replaced = _resolved_governance_policy({"governance_policy": policy_to_dict(compiled)},
        operating_rules=[source("new-review")], rules_touched=True, edited_governance_keys=set())
    assert replaced.approval.root_entity_id == "team"
    assert replaced.approval.policy.mode == "automatic"
    assert user_rule() in replaced.approval.policy.rules
    assert [r.source_id for r in replaced.approval.policy.rules if r.source_kind == "workspace_rule"] == ["new-review"]


def test_clearing_sources_does_not_clear_user_exceptions():
    policy = replace_workspace_rules(WorkspacePolicy(approval=ApprovalPolicyOverride(rules=[user_rule()])), [source()])
    assert policy_override(replace_workspace_rules(policy, [])).rules == [user_rule()]


def test_source_identity_is_stable_and_globs_are_not_guessed():
    assert compile_workspace_rules([source()]) == compile_workspace_rules([source()])
    with pytest.raises(ValueError):
        compile_workspace_rules([{"rule_key": "ban", "rule_type": "deny", "action_patterns": ["provider.*"]}])


def test_export_removes_root_local_targets_and_reusable_permission():
    resource = ApprovalRule(id="target-rule", effect="ask", selector={"kind": "resource", "action_key": "content.publish", "resource_kind": "post", "resource_ids": ["private-post"], "account_id": "private-account"})
    deny = user_rule("deny")
    deny.id = "deny-rule"
    policy = WorkspacePolicy(approval=ApprovalPolicyBinding(root_entity_id="private-team", policy=ApprovalPolicyOverride(mode="automatic", rules=[user_rule(), resource, deny])))
    exported = portable_governance(policy_to_dict(policy))
    assert exported["approval"]["mode"] is None
    assert len(exported["approval"]["rules"]) == 1
    assert exported["approval"]["rules"][0]["effect"] == "deny"
    assert "private-" not in str(exported)


def test_historical_template_import_recompiles_sources_as_review():
    policy, rules = import_blueprint_governance({"auto_approve_capabilities": ["file.write"]},
        [{"rule_key": "publish", "rule_type": "deny", "action_patterns": ["provider.*"]}])
    rules, projected, _ = normalize_default_workspace_governance(rules, policy)
    assert all(rule.effect == "ask" for rule in policy_override(projected).rules)
    assert rules[0]["approval_selectors"]
    assert "action_patterns" not in rules[0]
    assert policy_override(replace_workspace_rules(projected, rules)) == policy_override(projected)


def test_explicit_hard_block_import_cannot_silently_drop_a_glob():
    with pytest.raises(ValueError):
        import_blueprint_governance({"never_allow_actions": ["provider.*"]}, [], preserve_hard_blocks=True)


def test_legacy_setting_writes_and_blind_always_are_not_offered():
    from apps.api.routers.governance import PolicyBody
    from packages.core.services.hitl_options import approval_options
    with pytest.raises(ValueError):
        PolicyBody(auto_approve_actions=["content.publish"])
    assert approval_options(["approve", "always_approve", "reject"]) == ["approve", "reject"]
