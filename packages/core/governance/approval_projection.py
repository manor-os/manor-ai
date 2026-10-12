"""Compile Workspace sources and portable configuration into the one policy.

These helpers never decide whether an operation may execute. Runtime decisions
belong to ``approval_policy.evaluate_policy`` with the persisted account chain.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from packages.core.governance.approval_policy import (
    ApprovalPolicyBinding, ApprovalPolicyOverride, ApprovalRule, RuleDraft,
)


def policy_override(policy: Any) -> ApprovalPolicyOverride:
    if policy is None:
        return ApprovalPolicyOverride()
    approval = policy.get("approval") if isinstance(policy, dict) else policy.approval
    if approval is None:
        return ApprovalPolicyOverride()
    if isinstance(approval, ApprovalPolicyBinding):
        return approval.policy.model_copy(deep=True)
    if isinstance(approval, ApprovalPolicyOverride):
        return approval.model_copy(deep=True)
    if "root_entity_id" in approval:
        return ApprovalPolicyBinding.model_validate(approval).policy
    return ApprovalPolicyOverride.model_validate(approval)


def with_approval_override(policy: Any, override: ApprovalPolicyOverride):
    approval = policy.approval
    if isinstance(approval, ApprovalPolicyBinding):
        approval = approval.model_copy(update={"policy": override})
    else:
        approval = override
    return replace(policy, approval=approval)


def selector_label(rule: ApprovalRule) -> str:
    selector = rule.selector
    if selector.kind == "category":
        return selector.category
    if selector.kind == "resource":
        return f"{selector.action_key}: {', '.join(selector.resource_ids)}"
    return selector.action_key


def approval_summary(policy: Any) -> list[str]:
    approval = policy_override(policy)
    lines = [f"Approval mode: {approval.mode or 'follow account default'}"]
    for rule in approval.rules:
        lines.append(f"{rule.effect}: {selector_label(rule)}")
    return lines


def compile_workspace_rules(rules: list[Any]) -> list[ApprovalRule]:
    """Rebuild only source-owned rules; exact source identity is retained.

    A glob or capability is not a canonical action. Reject an unrepresentable
    source rather than silently broadening permission or narrowing a ban.
    New sources can use typed ``approval_selectors`` to express a category.
    """
    effects = {
        "auto_approve": "allow", "allow": "allow", "exception": "allow",
        "approval_required": "ask", "hitl_required": "ask",
        "require_approval": "ask", "review_required": "ask",
        "deny": "deny", "never_allow": "deny", "block": "deny", "draft_only": "deny",
    }
    result: list[ApprovalRule] = []
    for index, source in enumerate(rules):
        if not isinstance(source, dict):
            continue
        effect = effects.get(str(source.get("rule_type") or "").strip().lower())
        if effect is None:
            continue
        selectors = source.get("approval_selectors")
        if selectors is None:
            actions = source.get("action_patterns") or source.get("actions") or source.get("patterns") or []
            if isinstance(actions, str):
                actions = [actions]
            capabilities = source.get("capability_patterns") or source.get("capabilities") or source.get("capability_ids") or []
            if capabilities and not actions:
                raise ValueError("Use explicit approval_selectors for capability-based Workspace rules")
            selectors = [{"kind": "action", "action_key": action} for action in actions]
        if not isinstance(selectors, list):
            raise ValueError("approval_selectors must be a list")
        source_id = str(source.get("rule_key") or source.get("id") or f"rule_{index + 1}")
        for selector in selectors:
            draft = RuleDraft(effect=effect, selector=selector)
            digest = hashlib.sha256(json.dumps(
                draft.selector.model_dump(), sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()[:20]
            result.append(ApprovalRule(
                id=f"source:{hashlib.sha256(source_id.encode()).hexdigest()[:16]}:{digest}",
                effect=draft.effect, selector=draft.selector,
                source_kind="workspace_rule", source_id=source_id,
            ))
    return result


def replace_workspace_rules(policy: Any, rules: list[Any]):
    override = policy_override(policy)
    override.rules = [rule for rule in override.rules if rule.source_kind != "workspace_rule"] + compile_workspace_rules(rules)
    return with_approval_override(policy, override)


def portable_governance(raw: dict[str, Any]) -> dict[str, Any]:
    """Export safe configuration without local bindings or reusable consent."""
    override = policy_override(raw)
    portable_rules = []
    exported_ids: set[str] = set()
    for rule in override.rules:
        # Business rules travel in operating_model.rules and are compiled on
        # install. Exporting their projection as user rules duplicates ownership.
        if rule.source_kind == "workspace_rule" or rule.selector.kind == "resource" or rule.effect == "allow":
            continue
        copy = rule.model_copy(update={"source_kind": "user", "source_id": None})
        copy.id = "portable:" + hashlib.sha256(json.dumps(
            {"effect": copy.effect, "selector": copy.selector.model_dump()}, sort_keys=True,
        ).encode()).hexdigest()[:24]
        if copy.id not in exported_ids:
            portable_rules.append(copy)
            exported_ids.add(copy.id)
    portable = ApprovalPolicyOverride(
        mode=override.mode if override.mode != "automatic" else None,
        rules=portable_rules,
    )
    return {
        "approval": portable.model_dump(),
        "max_risk_level": raw.get("max_risk_level", "high"),
        "budget_caps_per_kind": dict(raw.get("budget_caps_per_kind") or {}),
    }


def import_blueprint_governance(raw: dict[str, Any], rules: list[Any], *, preserve_hard_blocks: bool = False):
    """Explicit import boundary for historical packages, never runtime fallback."""
    from copy import deepcopy
    from packages.core.governance.approval_policy_migration import normalize_legacy_workspace_policy
    from packages.core.governance.policy import policy_from_dict

    converted = normalize_legacy_workspace_policy(raw, safe_import=not preserve_hard_blocks)
    sources = deepcopy(rules)
    for source in sources:
        if not isinstance(source, dict) or source.get("approval_selectors") is not None:
            continue
        kind = str(source.get("rule_type") or "").lower()
        field = (
            "never_allow" if kind in {"deny", "never_allow", "block", "draft_only"}
            else "hitl_required" if kind in {"approval_required", "hitl_required", "require_approval", "review_required"}
            else "auto_approve" if kind in {"auto_approve", "allow", "exception"} else None
        )
        if field is None:
            continue
        actions = source.get("action_patterns") or source.get("actions") or source.get("patterns") or []
        capabilities = source.get("capability_patterns") or source.get("capabilities") or source.get("capability_ids") or []
        source_policy = normalize_legacy_workspace_policy({
            f"{field}_actions": [actions] if isinstance(actions, str) else actions,
            f"{field}_capabilities": [capabilities] if isinstance(capabilities, str) else capabilities,
        }, safe_import=not preserve_hard_blocks)
        source["approval_selectors"] = [rule.selector.model_dump() for rule in policy_override(source_policy).rules]
        if not preserve_hard_blocks:
            source["rule_type"] = "approval_required"
        for key in ("action_patterns", "actions", "patterns", "capability_patterns", "capabilities", "capability_ids"):
            source.pop(key, None)
    return policy_from_dict(converted), sources
