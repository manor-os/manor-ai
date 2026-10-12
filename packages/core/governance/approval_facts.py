"""Normalize server-owned action metadata for the single approval evaluator."""
from __future__ import annotations

from packages.core.governance.approval_policy import ActionFacts, ApprovalTarget
from packages.core.ai.runtime.chrome_tool_catalog import CHROME_READ_ACTIONS


_BASIC_CHROME_READ_ACTIONS = CHROME_READ_ACTIONS | {"send_cdp"}


def action_facts(
    *, action_key: str | None, capability_id: str | None = None,
    resource_kind: str | None = None, resource_id: str | None = None,
    resource_ids: tuple[str, ...] = (), account_id: str | None = None,
    operation: str | None = None, risk_level: str = "medium",
    impact: str | None = None, category: str | None = None,
) -> ActionFacts:
    """Classify known semantic actions; uncertain effects always remain unknown.

    Tool names alone never establish a read-only effect. The runtime classifier
    supplies ``impact=read`` after its typed authorization check.
    """
    key = str(action_key or "").strip() or "unclassified.action"
    verb = str(operation or key.rsplit(".", 1)[-1]).strip().lower()
    capability = str(capability_id or "")
    if category is None:
        chrome_read_action = key.removeprefix("tool.mcp__chrome__").removesuffix(".read")
        if key == "chrome.browser.interact" or (
            key.startswith("tool.mcp__chrome__")
            and key.endswith(".read")
            and chrome_read_action in _BASIC_CHROME_READ_ACTIONS
        ) or (
            key == "tool.search_tools.read"
            and capability == "runtime.discovery"
            and resource_kind == "browser"
            and resource_id == "chrome"
        ):
            category = "browser"
        elif verb in {"delete", "remove", "destroy", "purge"}:
            category = "deletion"
        elif verb in {"publish", "post", "submit_publication"}:
            category = "publishing"
        elif verb in {"send", "reply", "forward", "message"}:
            category = "communication"
        elif verb in {"purchase", "buy", "pay", "refund"}:
            category = "purchases"
        elif capability in {"automation.manage", "initiative.manage"} or key.startswith(("workspace.automation.", "agent.initiative.")):
            category = "automation"
        elif key.startswith("workspace.file.") or capability == "file.write" or resource_kind in {"file", "workspace_file", "room_file", "sandbox_file"}:
            category = "files"
        elif key.startswith("workspace."):
            category = "workspace"
        elif impact == "read":
            category = "read"
        else:
            category = "unknown"
    if impact is None:
        if category == "browser":
            impact = "read" if verb == "read" else "reversible" if verb == "modify" else "unknown"
        elif category == "deletion":
            impact = "destructive"
        elif category in {"publishing", "communication", "purchases", "automation"}:
            impact = "external"
        elif category == "files" and verb in {"create", "write", "modify", "edit", "replace", "generate", "patch"}:
            impact = "reversible"
        elif key in {"workspace.operation.draft", "workspace.draft.start", "workspace.draft.update", "workspace.goal.update", "workspace.stat.record"}:
            impact = "reversible"
        else:
            impact = "unknown"
    targets = [
        ApprovalTarget(account_id=account_id, resource_kind=resource_kind or "resource", resource_id=target)
        for target in dict.fromkeys((resource_id, *resource_ids)) if target
    ]
    return ActionFacts(action_key=key, category=category, impact=impact, risk_level=risk_level, targets=targets)
