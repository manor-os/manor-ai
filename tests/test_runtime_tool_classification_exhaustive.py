"""Every advertised runtime tool and every workspace_agent / manor→workspace
action must classify to a declared execution effect. UNKNOWN is a hard block at
runtime, so a missing table entry is a production outage (2026-09-11:
manor→workspace→operation get_current)."""
from __future__ import annotations

import pytest

from packages.core.ai.runtime.approval_classifier import (
    classify_runtime_tool,
    runtime_policy_action_for_classification,
)
from packages.core.ai.runtime.composite_tools import SandboxToolAction, WorkspaceToolAction
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
from packages.core.ai.runtime.workspace_operation_actions import WORKSPACE_OPERATION_ACTIONS
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain

# Action-dispatch tools: their effect is decided by ``action``, so every action
# in the implementation's own vocabulary is a case. Add a tool here (with the
# enum/tuple the implementation exposes) when the guard fails for a dispatcher;
# never add a skip list.
_ACTION_DISPATCH_TOOLS: dict[str, tuple[str, ...]] = {
    "sandbox": tuple(SandboxToolAction.values()),
    "workspace_operation": tuple(WORKSPACE_OPERATION_ACTIONS),
}

# Tools whose effect depends on non-action arguments get one representative
# argument set here. Never add a skip list.
_ARGS_BY_TOOL: dict[str, dict] = {}


def _cases():
    for name in sorted(runtime_registered_tool_names(include_undiscoverable=True)):
        if name.startswith("mcp__"):
            continue  # MCP tools classify from the server-declared effect at call time
        if name in _ACTION_DISPATCH_TOOLS:
            for action in _ACTION_DISPATCH_TOOLS[name]:
                yield name, {"action": action}
            continue
        yield name, _ARGS_BY_TOOL.get(name, {})
    for action in WorkspaceToolAction.values():
        if action == "operation":
            for operation in WORKSPACE_OPERATION_ACTIONS:
                yield "workspace_agent", {"action": "operation", "params": {"action": operation}}
                yield "manor", {"action": "workspace", "params": {"action": "operation", "params": {"action": operation}}}
            continue
        yield "workspace_agent", {"action": action, "params": {}}
        yield "manor", {"action": "workspace", "params": {"action": action, "params": {}}}


CASES = list(_cases())


def _case_id(case) -> str:
    name, args = case
    inner = args.get("params") if isinstance(args.get("params"), dict) else {}
    return f"{name}:{args.get('action', '')}:{inner.get('action', '')}"


@pytest.mark.parametrize(("name", "args"), CASES, ids=[_case_id(case) for case in CASES])
def test_every_registered_tool_and_workspace_action_has_a_declared_effect(name, args):
    classification = classify_runtime_tool(name, args)
    assert classification.effect is not RuntimeToolEffect.UNKNOWN, (name, args, classification.reason)


def test_all_valid_discovery_modes_are_read_only_by_default_and_explicit_rules_still_apply():
    for arguments in (
        {"mode": "semantic", "query": "email messages"},
        {"mode": "provider", "provider_key": "gmail"},
        {"mode": "exact", "exact_names": ["mcp__gmail__get_message"]},
    ):
        action, impact = runtime_policy_action_for_classification(
            classify_runtime_tool("search_tools", arguments)
        )
        facts = action_facts(
            action_key=action.action_key,
            capability_id=action.capability_id,
            resource_kind=action.resource_kind,
            resource_id=action.resource_id,
            risk_level=action.risk_level,
            operation=action.operation,
            impact=impact,
        )
        assert impact == "read"
        for effect in ("allow", "ask", "deny"):
            rules = () if effect == "allow" else ({
                "id": "explicit-discovery",
                "effect": effect,
                "selector": {"kind": "action", "action_key": "tool.search_tools.read"},
            },)
            chain = build_policy_chain(
                scope=ApprovalScope(kind="workspace", id="workspace", entity_id="entity"),
                root={"revision": 1, "policy": {"mode": "confirm_changes", "rules": []}},
                local={"root_entity_id": "entity", "revision": 1, "policy": {"mode": None, "rules": list(rules)}},
            )
            assert evaluate_policy(chain, facts).outcome == effect
