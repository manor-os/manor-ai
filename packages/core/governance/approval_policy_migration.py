"""Explicit offline/import conversion. Never imported by online policy loading.

Unrepresentable historical selectors stop a live-policy conversion. Template
imports may conservatively replace such selectors with a full review gate;
that mode deliberately grants no automatic execution authority.
"""
from __future__ import annotations

import hashlib
import json

from packages.core.governance.approval_policy import (
    ApprovalCategory, ApprovalPolicyBinding, ApprovalPolicyOverride, ApprovalRule,
    ActionSelector, CategorySelector,
)
from typing import get_args

LEGACY_POLICY_KEYS = frozenset({
    'never_allow_actions', 'hitl_required_actions', 'auto_approve_actions',
    'never_allow_capabilities', 'hitl_required_capabilities', 'auto_approve_capabilities',
    'automated_runs_always_allow',
})


class LegacyApprovalConversionError(ValueError):
    def __init__(self, blockers: list[dict]):
        self.blockers = blockers
        super().__init__('Legacy approval settings require explicit resolution: ' + '; '.join(item['reason'] for item in blockers))


def source_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _rule_id(key: str, value: str) -> str:
    return 'migration-' + source_digest([key, value])[:32]


def normalize_legacy_workspace_policy(raw: dict | None, *, root_entity_id: str | None = None,
                                      safe_import: bool = False) -> dict:
    """Return current policy JSON, or fail with selector-level blockers.

    ``None`` means the *historical* DEFAULT_POLICY, not an empty opinion.
    A safe Blueprint import always requires confirmation for old automatic or
    unrepresentable configuration; live owners require an explicit resolution.
    """
    if raw is None:
        raw = {'auto_approve_capabilities': ['file.write', 'manor.composite']}
    raw = dict(raw)
    if 'approval' in raw:
        result = {key: value for key, value in raw.items() if key not in LEGACY_POLICY_KEYS}
        if any(raw.get(key) for key in LEGACY_POLICY_KEYS):
            raise LegacyApprovalConversionError([{'field': 'approval', 'reason': 'mixed legacy and unified configuration'}])
        if root_entity_id is not None:
            binding = ApprovalPolicyBinding.model_validate(raw['approval'])
            if binding.root_entity_id != root_entity_id:
                raise LegacyApprovalConversionError([{'field': 'approval', 'reason': 'persisted root binding does not match Workspace owner'}])
        elif isinstance(raw['approval'], dict) and 'root_entity_id' in raw['approval']:
            ApprovalPolicyBinding.model_validate(raw['approval'])
        else:
            ApprovalPolicyOverride.model_validate(raw['approval'])
        return result
    blockers = []
    rules = []
    global_review = False
    for field, effect in (
        ('never_allow_actions', 'deny'), ('hitl_required_actions', 'ask'), ('auto_approve_actions', 'allow'),
        ('never_allow_capabilities', 'deny'), ('hitl_required_capabilities', 'ask'), ('auto_approve_capabilities', 'allow'),
    ):
        values = raw.get(field) or []
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            blockers.append({'field': field, 'reason': f'{field} is not a string list'})
            continue
        for value in values:
            # Resource strings omit account/resource-kind evidence and cannot
            # be promoted to an account-independent exact target grant.
            if 'capabilities' in field or any(char in value for char in '*?[') or '#resource:' in value:
                if safe_import:
                    global_review = True
                else:
                    blockers.append({'field': field, 'selector': value,
                        'reason': f'{field}: {value!r} has no proven equivalent canonical selector'})
                continue
            try:
                selector = ActionSelector(action_key=value)
            except ValueError:
                blockers.append({'field': field, 'selector': value, 'reason': f'{value!r} is not a canonical action'})
                continue
            rules.append(ApprovalRule(id=_rule_id(field, value), effect='ask' if safe_import else effect, selector=selector))
    if raw.get('automated_runs_always_allow'):
        if safe_import:
            global_review = True
        else:
            blockers.append({'field': 'automated_runs_always_allow', 'reason': 'unbounded scheduled-run consent requires a bounded replacement'})
    # Old allow beat ask; the new policy chooses ask within the same layer.
    by_action: dict[str, set[str]] = {}
    for rule in rules:
        by_action.setdefault(rule.selector.action_key, set()).add(rule.effect)
    for action, effects in by_action.items():
        if effects == {'ask', 'allow'}:
            blockers.append({'field': 'rules', 'selector': action, 'reason': f'{action!r} has conflicting legacy allow/ask precedence'})
    if blockers:
        raise LegacyApprovalConversionError(blockers)
    if global_review:
        # Reads are allowed by every mode; a review gate never covers them.
        rules.extend(ApprovalRule(id=_rule_id('template_review', category), effect='ask', selector=CategorySelector(category=category))
            for category in get_args(ApprovalCategory) if category != 'read')
    override = ApprovalPolicyOverride(rules=rules)
    approval = ApprovalPolicyBinding(root_entity_id=root_entity_id, policy=override).model_dump() if root_entity_id else override.model_dump()
    return {**{key: value for key, value in raw.items() if key not in LEGACY_POLICY_KEYS}, 'approval': approval}
