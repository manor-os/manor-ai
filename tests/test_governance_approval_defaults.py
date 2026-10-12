"""Author defaults are safe projections, never copied operator consent."""
from copy import deepcopy

import pytest

from packages.core.governance.approval_policy import (
    ActionFacts, ApprovalPolicyBinding, ApprovalScope, evaluate_policy,
)
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance.approval_projection import policy_override
from packages.core.governance.defaults import normalize_default_workspace_governance
from packages.core.governance.policy import WorkspacePolicy, decide, policy_from_dict
from packages.core.governance.presets import apply_preset


def policy_with(*rules, mode=None, **hard):
    return policy_from_dict({'approval': {'mode': mode, 'rules': list(rules)}, **hard})


def rule(effect, selector, rule_id='author'):
    return {'id': rule_id, 'effect': effect, 'selector': selector}


def decision(policy, action='social_post.publish', category='publishing', impact='external'):
    chain = build_policy_chain(
        scope=ApprovalScope(kind='workspace', id='workspace', entity_id='account'),
        root={'revision': 1, 'policy': {'mode': 'automatic', 'rules': []}},
        local={'revision': 1, **ApprovalPolicyBinding(
            root_entity_id='account', policy=policy_override(policy)).model_dump()},
    )
    return evaluate_policy(chain, ActionFacts(action_key=action, category=category, impact=impact))


@pytest.mark.parametrize('rule_type', ['deny', 'draft_only', 'never_allow', 'block', ' deny '])
@pytest.mark.parametrize('selector', [
    {'kind': 'action', 'action_key': 'social_post.publish'},
    {'kind': 'category', 'category': 'publishing'},
])
def test_source_and_policy_remain_reviewable_after_rule_rebuild(rule_type, selector):
    from packages.core.services.workspace_setup_service import _build_governance_policy_from_rules

    source = [{'rule_key': 'review_posts', 'rule_type': rule_type, 'approval_selectors': [selector]}]
    original = deepcopy(source)
    rules, policy, _ = normalize_default_workspace_governance(source, WorkspacePolicy())
    assert source == original
    assert rules[0]['rule_type'] == 'approval_required'
    rebuilt = _build_governance_policy_from_rules(rules, {})
    for candidate in (policy, rebuilt):
        assert all(item.effect == 'ask' for item in policy_override(candidate).rules)
        assert decision(candidate).outcome == 'ask'


def test_unscoped_text_remains_guidance_without_inventing_action_scope():
    source = ['Never publish without review.', {'rule_type': 'deny', 'description': 'Avoid controversial topics'}]
    original = deepcopy(source)
    rules, policy, moved = normalize_default_workspace_governance(source, WorkspacePolicy())
    assert source == original
    assert rules[0] == source[0]
    assert rules[1]['rule_type'] == 'approval_required'
    assert 'approval_selectors' not in rules[1]
    assert not policy_override(policy).rules and moved == []


@pytest.mark.parametrize('preserve', [False, True])
def test_author_policy_never_transports_automatic_or_local_resource_consent(preserve):
    original = policy_with(
        rule('allow', {'kind': 'category', 'category': 'communication'}, 'allow-send'),
        rule('deny', {'kind': 'category', 'category': 'publishing'}, 'deny-publish'),
        rule('ask', {'kind': 'resource', 'action_key': 'communication.send', 'account_id': 'private',
                     'resource_kind': 'contact', 'resource_ids': ['private-contact']}, 'local'),
        mode='automatic',
    )
    before = deepcopy(original)
    _, projected, moved = normalize_default_workspace_governance([], original, preserve_hard_blocks=preserve)
    approval = policy_override(projected)
    assert original == before
    assert approval.mode is None
    assert all(item.effect != 'allow' and item.selector.kind != 'resource' for item in approval.rules)
    assert decision(projected).outcome == ('deny' if preserve else 'ask')
    assert moved == ([] if preserve else ['publishing'])


@pytest.mark.parametrize('preset', ['follow_default', 'confirm_changes', 'risk_based'])
def test_source_allows_cannot_reintroduce_portable_consent(preset):
    from packages.core.services.workspace_setup_service import _build_governance_policy_from_rules

    source = [{'rule_key': 'send', 'rule_type': 'auto_approve',
               'approval_selectors': [{'kind': 'category', 'category': 'communication'}]}]
    rules, policy, _ = normalize_default_workspace_governance(source, apply_preset(WorkspacePolicy(), preset))
    rebuilt = _build_governance_policy_from_rules(rules, {})
    assert source[0]['rule_type'] == 'auto_approve'
    for candidate in (policy, rebuilt):
        assert not any(item.effect == 'allow' for item in policy_override(candidate).rules)
        assert decision(candidate, 'email.send', 'communication').outcome == 'ask'


@pytest.mark.parametrize('preset', ['safe', 'standard', 'aggressive', 'no_default_grants'])
def test_retired_presets_are_not_an_online_second_policy_model(preset):
    with pytest.raises(KeyError, match='refresh the client'):
        apply_preset(WorkspacePolicy(), preset)


def test_opted_in_hard_constraints_and_financial_caps_keep_their_contract():
    source = [{'rule_key': 'billing', 'rule_type': 'deny',
               'approval_selectors': [{'kind': 'action', 'action_key': 'billing.charge'}]}]
    original = WorkspacePolicy(max_risk_level='low', budget_caps_per_kind={'action': 20})
    rules, policy, moved = normalize_default_workspace_governance(source, original, preserve_hard_blocks=True)
    assert rules == source and moved == []
    assert policy.max_risk_level == 'low'
    assert decision(policy, 'billing.charge', 'purchases').outcome == 'deny'
    hard = decide(original, kind='action', action_key='billing.charge', risk_level='high')
    assert not hard.allowed and not hard.pause_for_hitl
    _, projected, _ = normalize_default_workspace_governance([], original)
    assert projected.max_risk_level == 'high'
    approval = policy_override(projected)
    assert approval.mode is None  # inherit the operator account's mode
    required_categories = {
        'files', 'communication', 'publishing', 'deletion', 'purchases',
        'automation', 'workspace', 'unknown',
    }
    assert {(item.effect, item.selector.kind, item.selector.category)
            for item in approval.rules} == {
        ('ask', 'category', category) for category in required_categories}
    assert len(approval.rules) == len(required_categories)
    following = apply_preset(projected, 'follow_default')
    assert policy_override(following) == approval
    for category in required_categories:
        assert decision(following, 'tool.change', category, 'reversible').outcome == 'ask'
    assert decision(following, 'tool.read', 'read', 'read').outcome == 'allow'
    assert decision(following, 'chrome.browser.interact', 'browser', 'reversible').outcome == 'allow'
    capped = decide(projected, kind='action', action_key='billing.charge', risk_level='low',
                    spent_credits_per_kind={'action': 20})
    assert not capped.allowed and capped.matched_rule == 'budget_caps_per_kind.action'
    assert original.max_risk_level == 'low'


def test_risk_ceiling_projection_preserves_explicit_browser_review():
    original = policy_with(
        rule('ask', {'kind': 'category', 'category': 'browser'}, 'review-browser'),
        max_risk_level='low',
    )
    _, projected, _ = normalize_default_workspace_governance([], original)

    assert decision(projected, 'chrome.browser.interact', 'browser', 'reversible').outcome == 'ask'
    assert [item.id for item in policy_override(projected).rules
            if item.selector.kind == 'category' and item.selector.category == 'browser'] == ['review-browser']


def test_repeated_projection_preserves_source_identity_without_duplicate_rules():
    source = [{'rule_key': 'publishing', 'rule_type': 'deny',
               'approval_selectors': [{'kind': 'category', 'category': 'publishing'}]}]
    first = normalize_default_workspace_governance(source, WorkspacePolicy())
    second = normalize_default_workspace_governance(first[0], first[1])
    assert second[:2] == first[:2]
    assert len(policy_override(second[1]).rules) == 1
    assert second[2] == []
