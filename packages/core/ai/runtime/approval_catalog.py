from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal


ApprovalMode = Literal["allow", "approval", "deny"]


@dataclass(frozen=True)
class RuntimeApprovalCatalogRule:
    """Default approval rule independent of any concrete MCP/tool name."""

    mode: ApprovalMode
    action_pattern: str | None = None
    capability_pattern: str | None = None
    operation: str | None = None
    min_risk_level: str | None = None
    reason: str = ""


_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


# Global runtime defaults used when there is no narrower user/workspace policy.
# Concrete tools are normalized into action_key/capability/operation before
# these rules run, so the catalog stays stable as MCP tool names evolve.
GLOBAL_DIRECT_CHAT_APPROVAL_RULES: tuple[RuntimeApprovalCatalogRule, ...] = (
    RuntimeApprovalCatalogRule(
        mode="approval",
        operation="delete",
        reason="Destructive actions need explicit confirmation in direct chat.",
    ),
    RuntimeApprovalCatalogRule(
        mode="approval",
        operation="send",
        reason="External sends need explicit confirmation in direct chat.",
    ),
    RuntimeApprovalCatalogRule(
        mode="approval",
        operation="publish",
        reason="External publishing needs explicit confirmation in direct chat.",
    ),
    RuntimeApprovalCatalogRule(
        mode="approval",
        action_pattern="workspace.automation.*",
        reason="Automation changes can run later without the user present.",
    ),
    RuntimeApprovalCatalogRule(
        mode="approval",
        min_risk_level="high",
        reason="High-risk actions need explicit confirmation in direct chat.",
    ),
)


def direct_chat_default_approval_mode(
    *,
    action_key: str | None,
    capability_id: str | None = None,
    operation: str | None = None,
    risk_level: str | None = None,
) -> ApprovalMode:
    """Return the global direct-chat default for a normalized runtime action."""

    for rule in GLOBAL_DIRECT_CHAT_APPROVAL_RULES:
        if _matches_rule(
            rule,
            action_key=action_key,
            capability_id=capability_id,
            operation=operation,
            risk_level=risk_level,
        ):
            return rule.mode
    return "allow"


def _matches_rule(
    rule: RuntimeApprovalCatalogRule,
    *,
    action_key: str | None,
    capability_id: str | None,
    operation: str | None,
    risk_level: str | None,
) -> bool:
    if rule.action_pattern and not fnmatch.fnmatchcase(str(action_key or ""), rule.action_pattern):
        return False
    if rule.capability_pattern and not fnmatch.fnmatchcase(str(capability_id or ""), rule.capability_pattern):
        return False
    if rule.operation and str(operation or "").strip().lower() != rule.operation:
        return False
    if rule.min_risk_level:
        actual = _RISK_RANK.get(str(risk_level or "low").strip().lower(), 0)
        minimum = _RISK_RANK.get(rule.min_risk_level, 0)
        if actual < minimum:
            return False
    return True
