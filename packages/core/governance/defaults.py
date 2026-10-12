"""Safe creation/Blueprint projection for the unified approval policy."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, get_args

from packages.core.governance.policy import WorkspacePolicy
from packages.core.governance.approval_policy import ApprovalCategory, ApprovalRule
from packages.core.governance.approval_projection import (
    policy_override, replace_workspace_rules, selector_label, with_approval_override,
)


def normalize_default_workspace_governance(
    rules: list[Any], policy: WorkspacePolicy, *, preserve_hard_blocks: bool = False,
) -> tuple[list[Any], WorkspacePolicy, list[str]]:
    """Project author configuration, never reusable operator permission.

    Local resource bindings and allows are not portable authority. Explicit
    operator prohibitions remain available through the existing opt-in.
    """
    sources = deepcopy(rules)
    projected = deepcopy(policy)
    override = policy_override(projected)
    override.mode = None if override.mode == 'automatic' else override.mode
    override.rules = [r for r in override.rules if r.selector.kind != 'resource' and r.effect != 'allow']
    moved = []
    for rule in override.rules:
        if rule.effect == 'deny' and not preserve_hard_blocks:
            moved.append(selector_label(rule))
            rule.effect = 'ask'
    for source in sources:
        if not isinstance(source, dict):
            continue
        rule_type = str(source.get('rule_type') or '').strip().lower()
        if rule_type in {'auto_approve', 'allow', 'exception'} or (
            not preserve_hard_blocks and rule_type in {'deny', 'never_allow', 'block', 'draft_only'}
        ):
            source['rule_type'] = 'approval_required'
    if projected.max_risk_level != 'high' and not preserve_hard_blocks:
        projected.max_risk_level = 'high'
        # Template review requirements must survive follow_default on the
        # next install. A local mode is operator configuration and gets reset
        # by that preset, so represent this requirement as portable rules.
        # Basic browser preparation is low-risk; the old risk ceiling did not
        # restrict it. Explicit browser review rules above remain intact.
        reviewed = {rule.selector.category for rule in override.rules
                    if rule.effect == 'ask' and rule.selector.kind == 'category'}
        override.rules.extend(ApprovalRule(
            id=f'author-risk-review:{category}', effect='ask',
            selector={'kind': 'category', 'category': category},
        ) for category in get_args(ApprovalCategory)
            if category not in {'read', 'browser'} and category not in reviewed)
    projected = with_approval_override(projected, override)
    return sources, replace_workspace_rules(projected, sources), moved
