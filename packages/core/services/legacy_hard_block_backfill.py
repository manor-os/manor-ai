"""Downgrade legacy hard-block rules the user never asked for.

Owner decision (2026-09-07): a hard block (``deny`` / ``draft_only`` /
``never_allow`` / ``block``) may exist only because the user asked for a
prohibition in their own words. From that date on the architect refuses to
persist one without a verified quote — but nothing touched the rules that
already existed. Prod task FQ2881 (2026-09-14) ran into exactly such a rule:
"帖子只出草稿" had been turned into ``draft_only`` in August, which derives a
``never_allow`` on ``social_post.publish`` that no approval can override.

This module is the one-time transform (used by migration ``20260914_02``):

* A hard-block rule is **kept** when any signal says the user asked for it:
  it was typed on the rules page (``source == "operator"``), it carries the
  post-fix verified-quote marker (``enforcement_scope ==
  "all_matching_actions"``), it carries ``hard_block_requested_by_user``, or
  its own wording is a prohibition per ``USER_PROHIBITION_MARKERS`` — the
  same test the server applies to a new rule. Keeping a rule whose wording
  merely *reads* like a prohibition is deliberate: when the evidence is
  ambiguous the migration does not rewrite an operator's policy; the denial
  surfaces name the rule and offer the one-click soften instead.
* Every other hard block becomes ``approval_required`` with an audit trail
  (``downgraded_from`` / ``downgraded_by`` / ``downgraded_at``), so the gate
  still asks a person but no longer forbids.
* The derived governance lists are rebuilt the same way an operation-draft
  apply rebuilds them: patterns the old rules implied and the new rules no
  longer imply leave ``never_allow_*``, and the retyped rules re-add them
  under ``hitl_required_*``. A pattern another surviving hard block still
  claims stays blocked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

LEGACY_HARD_BLOCK_DOWNGRADE_MARKER = "20260914_02"

_DERIVED_POLICY_KEYS: tuple[str, ...] = (
    "never_allow_actions",
    "never_allow_capabilities",
    "hitl_required_actions",
    "hitl_required_capabilities",
    "auto_approve_actions",
    "auto_approve_capabilities",
)

_WORDING_FIELDS: tuple[str, ...] = ("description", "text", "title", "user_request_quote")


def hard_block_was_requested(rule: dict[str, Any]) -> bool:
    """Whether any persisted signal says the USER asked for this prohibition."""
    from packages.core.services.workspace_setup_service import user_requested_hard_block

    if str(rule.get("source") or "").strip().casefold() == "operator":
        return True
    if rule.get("hard_block_requested_by_user") is True:
        return True
    if str(rule.get("enforcement_scope") or "").strip() == "all_matching_actions":
        return True
    wording = " ".join(str(rule.get(field) or "") for field in _WORDING_FIELDS)
    return user_requested_hard_block(wording)


def downgrade_unrequested_hard_blocks(
    rules: Any,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(rules_after, downgraded)``; the input list is not mutated.

    ``downgraded`` holds the retyped rules (already in ``rules_after``) so a
    caller can report which keys changed. Idempotent: a rule that already
    carries the marker, or is not a hard block, or was requested, passes
    through unchanged.
    """
    from packages.core.services.workspace_setup_service import (
        _HARD_BLOCK_RULE_TYPES,
        _enrich_operating_rules,
    )

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    after: list[dict[str, Any]] = []
    downgraded: list[dict[str, Any]] = []
    for raw in rules if isinstance(rules, list) else []:
        if not isinstance(raw, dict):
            continue
        rule = dict(raw)
        rule_type = str(rule.get("rule_type") or "").strip()
        if (
            rule_type in _HARD_BLOCK_RULE_TYPES
            and not rule.get("downgraded_by")
            and not hard_block_was_requested(rule)
        ):
            rule["rule_type"] = "approval_required"
            rule["downgraded_from"] = rule_type
            rule["downgraded_by"] = LEGACY_HARD_BLOCK_DOWNGRADE_MARKER
            rule["downgraded_at"] = stamp
            # Re-derived below from the new rule_type.
            rule.pop("runtime_enforced", None)
            downgraded.append(rule)
        after.append(rule)
    if not downgraded:
        return after, []
    enriched = _enrich_operating_rules(after)
    downgraded_keys = {r.get("rule_key") for r in downgraded}
    return enriched, [r for r in enriched if r.get("rule_key") in downgraded_keys]


def rebuild_governance_after_retype(
    previous_rules: Any,
    rules_after: Any,
    stored_policy: Any,
) -> dict[str, Any]:
    """Recompute the derived governance lists after rules were retyped.

    This frozen migration transform deliberately writes the historical schema.
    It must run before the unified approval migration on a fresh installation;
    importing the current runtime policy compiler would reject its old rows.
    """
    previous = _legacy_derived_lists(previous_rules)
    following = _legacy_derived_lists(rules_after)
    raw = dict(stored_policy) if isinstance(stored_policy, dict) else {}
    for key in _DERIVED_POLICY_KEYS:
        before = set(previous[key])
        now_implied = set(following[key])
        removed = before - now_implied
        existing = raw.get(key) if isinstance(raw.get(key), list) else []
        raw[key] = list(dict.fromkeys(
            [value for value in existing if value not in removed] + following[key]
        ))
    raw.setdefault("max_risk_level", "high")
    raw.setdefault("budget_caps_per_kind", {})
    return raw


def _legacy_derived_lists(rules: Any) -> dict[str, list[str]]:
    """Historical six-list projection, only for migration 20260914_02."""
    from packages.core.ai.runtime import runtime_capability_id_for_action_key

    result: dict[str, list[str]] = {key: [] for key in _DERIVED_POLICY_KEYS}
    prefixes = {
        "deny": "never_allow", "never_allow": "never_allow",
        "block": "never_allow", "draft_only": "never_allow",
        "approval_required": "hitl_required", "hitl_required": "hitl_required",
        "require_approval": "hitl_required", "review_required": "hitl_required",
        "auto_approve": "auto_approve", "allow": "auto_approve", "exception": "auto_approve",
    }

    def patterns(rule: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
        for key in keys:
            value = rule.get(key)
            values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
            normalized = [str(item).strip() for item in values if str(item or "").strip()]
            if normalized:
                return list(dict.fromkeys(normalized))
        return []

    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        prefix = prefixes.get(str(rule.get("rule_type") or "").strip().lower())
        if prefix is None:
            continue
        actions = patterns(rule, ("action_patterns", "actions", "patterns"))
        capabilities = patterns(rule, ("capability_patterns", "capabilities", "capability_ids"))
        if not capabilities:
            capabilities = [
                capability for action in actions
                if (capability := runtime_capability_id_for_action_key(action))
            ]
        for suffix, values in (("actions", actions), ("capabilities", capabilities)):
            key = f"{prefix}_{suffix}"
            result[key] = list(dict.fromkeys(result[key] + values))
    return result


def downgrade_workspace_state(
    rules: Any,
    stored_policy: Any,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """One call for a (rules, policy) pair: returns ``(rules, policy, keys)``.

    ``keys`` is empty when nothing needed downgrading; callers then leave the
    row untouched.
    """
    rules_after, downgraded = downgrade_unrequested_hard_blocks(rules, now=now)
    if not downgraded:
        return rules_after, (dict(stored_policy) if isinstance(stored_policy, dict) else {}), []
    policy = rebuild_governance_after_retype(rules, rules_after, stored_policy)
    return rules_after, policy, [str(r.get("rule_key")) for r in downgraded]
