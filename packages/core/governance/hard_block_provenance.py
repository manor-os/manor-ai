"""Name the rule behind a hard block, and hand back the fix.

Every never_allow denial used to carry only the glob that matched
(``matched_rule='social_post.*'``). The rule that *produced* that glob — the
one the operator can actually change — was recomputable (the derived lists
are rebuilt from rules) but never recomputed at denial time, so the agent
could only say "the rules forbid this" and the operator could only guess
(prod task FQ2881, 2026-09-14: the chat agent reported "实时规则仍硬性禁止"
and stopped; the fix was one rule retype away).

This module walks the workspace's rules the same way the lint does
(``rule_enforcement_conflicts``), finds the hard-block rules whose patterns
cover the denied action / capability, and returns:

* ``blocking_rules`` — rule_key, rule_type, description, the pattern hit;
* ``fix`` — the exact ``workspace_operation`` patch that softens each rule
  to ``approval_required`` (raises a review card; nothing is applied
  silently) plus the rules-page link with the one-click equivalent;
* ``message`` — one sentence an agent can relay verbatim.

Deterministic and read-only. A block with no owning rule (a pattern written
straight into the governance policy) still gets the rules-page link.
"""
from __future__ import annotations

from typing import Any

SOFTEN_RULE_TYPE = "approval_required"


def hard_block_rules_for(
    rules: Any,
    *,
    action_key: str | None = None,
    capability_id: str | None = None,
    matched_rule: str | None = None,
) -> list[dict[str, Any]]:
    """Name source rules using the same compiled selectors as the policy gate.

    Resource-scoped provenance needs the matched rule ID from the gate; an
    action name alone does not prove that its target matched a resource rule.
    Unsupported legacy sources are never reinterpreted as typed rules here.
    """
    from packages.core.governance.approval_facts import action_facts
    from packages.core.governance.approval_projection import compile_workspace_rules, selector_label

    facts = action_facts(action_key=action_key, capability_id=capability_id)
    matched = str(matched_rule or "").strip()
    found: list[dict[str, Any]] = []
    for raw in rules if isinstance(rules, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            compiled = compile_workspace_rules([raw])
        except ValueError:
            continue  # Source errors cannot change a denial or invent its owner.
        for rule in compiled:
            if rule.effect != "deny":
                continue
            selector = rule.selector
            hit = rule.id == matched
            if selector.kind == "action":
                hit = hit or selector.action_key == facts.action_key
            elif selector.kind == "category":
                hit = hit or selector.category == facts.category
            if not hit:
                continue
            found.append({
                "rule_key": rule.source_id,
                "rule_type": str(raw.get("rule_type") or "").strip().lower(),
                "description": str(raw.get("description") or "").strip(),
                "kind": selector.kind,
                "pattern": selector_label(rule),
            })
            break
    return found


def soften_rule_patch(rule_key: str) -> dict[str, Any]:
    """The ``workspace_operation`` patch that turns one hard block into an approval gate."""
    return {
        "op": "rule.update",
        "payload": {"rule_key": rule_key, "rule": {"rule_type": SOFTEN_RULE_TYPE}},
    }


def rules_page_link(workspace_id: str | None) -> str | None:
    ws = str(workspace_id or "").strip()
    return f"/workspaces/{ws}?tab=rules" if ws else None


def hard_block_fix(workspace_id: str | None, blocking_rules: list[dict[str, Any]]) -> dict[str, Any]:
    keys = [r["rule_key"] for r in blocking_rules]
    fix: dict[str, Any] = {"rules_page": rules_page_link(workspace_id)}
    if keys:
        fix["summary"] = (
            "Change " + ", ".join(f"'{k}'" for k in keys)
            + f" to {SOFTEN_RULE_TYPE} so this can run once a person approves it."
        )
        fix["workspace_operation"] = {
            "action": "create_draft",
            "patches": [soften_rule_patch(k) for k in keys],
        }
    else:
        fix["summary"] = (
            "No workspace rule owns this block; the pattern is set directly in the "
            "governance policy. Remove it on the rules page."
        )
    return fix


def describe_hard_block(blocking_rules: list[dict[str, Any]], fix: dict[str, Any]) -> str:
    if blocking_rules:
        named = "; ".join(
            f"'{r['rule_key']}' ({r['rule_type']}" + (f": {r['description']}" if r["description"] else "") + ")"
            for r in blocking_rules
        )
        return (
            f"Blocked by workspace rule {named}. It can be allowed after approval: "
            f"change the rule to {SOFTEN_RULE_TYPE} (workspace_operation rule.update, which "
            "raises a review card) or use 'Change to approval required' on the rules page."
        )
    return (
        "Blocked by the workspace governance policy directly (no rule owns this pattern). "
        "Adjust it on the rules page."
    )


def hard_block_denial_context(
    rules: Any,
    *,
    workspace_id: str | None,
    action_key: str | None,
    capability_id: str | None,
    matched_rule: str | None,
) -> dict[str, Any]:
    """Pure: everything a denial should carry, from the workspace's rules."""
    blocking = hard_block_rules_for(
        rules, action_key=action_key, capability_id=capability_id, matched_rule=matched_rule,
    )
    fix = hard_block_fix(workspace_id, blocking)
    return {
        "blocking_rules": blocking,
        "fix": fix,
        "message": describe_hard_block(blocking, fix),
    }


async def governance_denial_step_error(
    db,
    *,
    workspace_id: str | None,
    action_key: str | None,
    capability_id: str | None,
    matched_rule: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """The ``step.error`` a governance hard block writes, wherever it is decided.

    Names the rule, carries the fix, and marks the failure human-required so
    the plan parks on a recovery card instead of replanning into the same
    wall. Provenance is advisory: a lookup failure still yields the error.
    """
    try:
        provenance = await load_hard_block_denial_context(
            db,
            workspace_id=workspace_id,
            action_key=action_key,
            capability_id=capability_id,
            matched_rule=matched_rule,
        )
    except Exception:  # noqa: BLE001 — provenance is advisory
        import logging

        logging.getLogger(__name__).warning("hard-block provenance lookup failed", exc_info=True)
        provenance = {}
    message = str(reason or "Workspace governance blocked this step.")
    if provenance:
        message = f"{message} {provenance['message']}"
    blockers = [
        f"{r['rule_key']}: {r['description'] or r['rule_type']}"
        for r in (provenance.get("blocking_rules") or [])
    ] or [str(matched_rule or "governance policy")]
    error: dict[str, Any] = {
        "type": "GovernancePolicy",
        "message": message,
        "matched_rule": matched_rule,
        "capability_id": capability_id,
        "failure": {
            "reason": message,
            "blockers": blockers,
            "retryable": False,
            "requires_human": True,
        },
    }
    if provenance:
        error["blocking_rules"] = provenance["blocking_rules"]
        error["fix"] = provenance["fix"]
    return error


async def load_hard_block_denial_context(
    db,
    *,
    workspace_id: str | None,
    action_key: str | None,
    capability_id: str | None,
    matched_rule: str | None,
) -> dict[str, Any]:
    """Load the workspace's rules and build the denial context; ``{}`` without a workspace."""
    ws = str(workspace_id or "").strip()
    if not ws:
        return {}
    from packages.core.models.workspace import Workspace

    workspace = await db.get(Workspace, ws)
    if workspace is None:
        return {}
    operating_model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    return hard_block_denial_context(
        operating_model.get("rules"),
        workspace_id=ws,
        action_key=action_key,
        capability_id=capability_id,
        matched_rule=matched_rule,
    )
