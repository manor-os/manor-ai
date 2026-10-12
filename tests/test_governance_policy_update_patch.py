"""Generic Workspace patches can change hard limits, not approval ownership."""
from __future__ import annotations

import pytest

from packages.core.governance.approval_policy import ApprovalPolicyBinding, ApprovalPolicyOverride, ApprovalRule, ActionSelector
from packages.core.services.workspace_operation_service import _apply_single_patch, _resolved_governance_policy


def _approval():
    return ApprovalPolicyBinding(root_entity_id='entity', policy=ApprovalPolicyOverride(mode='confirm_changes', rules=[
        ApprovalRule(id='operator-rule', effect='ask', selector=ActionSelector(action_key='external_message.send'))])).model_dump()


def test_governance_policy_update_merges_hard_limits_and_preserves_approval():
    state = {'operating_model': {}, 'governance_policy': {'approval': _approval(), 'max_risk_level': 'high'}}
    result = _apply_single_patch(state, {'op': 'governance_policy.update', 'governance_policy': {'budget_caps_per_kind': {'action': 500}}})
    assert result['governance_policy']['approval'] == _approval()
    assert result['governance_policy']['max_risk_level'] == 'high'
    assert result['operating_model']['governance']['budget_caps_per_kind'] == {'action': 500}


def test_governance_update_alias_accepts_flat_hard_limit_payload():
    result = _apply_single_patch({'operating_model': {}, 'governance_policy': {}}, {'op': 'governance.update', 'max_risk_level': 'low'})
    assert result['governance_policy']['max_risk_level'] == 'low'


@pytest.mark.parametrize('key,value', [
    ('approval', {'mode': 'automatic', 'rules': []}),
    ('never_allow_actions', []), ('hitl_required_actions', []), ('auto_approve_actions', []),
    ('never_allow_capabilities', []), ('hitl_required_capabilities', []), ('auto_approve_capabilities', []),
    ('automated_runs_always_allow', False),
])
@pytest.mark.parametrize('op', ['governance_policy.update', 'governance.update', 'operating_model.update'])
def test_generic_patches_reject_both_new_approval_subtree_and_retired_fields(key, value, op):
    state = {'operating_model': {}, 'governance_policy': {'approval': _approval()}}
    patch = {'op': op, 'governance_policy': {key: value}}
    if op == 'operating_model.update':
        patch = {'op': op, 'operating_model': {'governance': {key: value}}}
    with pytest.raises(ValueError, match='Execution & approvals'):
        _apply_single_patch(state, patch)


def test_rule_rebuild_preserves_operator_override_and_hard_limits():
    policy = _resolved_governance_policy({'governance_policy': {'approval': _approval(), 'max_risk_level': 'medium'}, 'operating_model': {}},
        operating_rules=[], rules_touched=True, edited_governance_keys=set())
    assert policy.approval.root_entity_id == 'entity'
    assert policy.approval.policy.mode == 'confirm_changes'
    assert [rule.id for rule in policy.approval.policy.rules] == ['operator-rule']
    assert policy.max_risk_level == 'medium'


def test_unknown_op_still_raises():
    with pytest.raises(ValueError, match='unsupported workspace operation patch'):
        _apply_single_patch({'operating_model': {}}, {'op': 'totally.bogus'})
