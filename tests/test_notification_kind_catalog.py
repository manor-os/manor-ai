"""Every kind routed through notify() is in the catalog.

The notification settings page lists EVENT_CATALOG; a kind that is sent but
not catalogued reaches people with no setting to turn it down. This walks the
source (the call sites are the contract being checked) and resolves each
call's kind from a literal, a module-level constant, a dict payload, or the
attention delivery registry.
"""
from __future__ import annotations

import ast
from pathlib import Path

from packages.core.constants.notification_types import EVENTS_BY_KIND

ROOTS = ("packages", "apps/api")
# The agent's notify_user tool passes the kind it was asked for.
DYNAMIC_SENDERS = {"packages/core/ai/runtime/notification_actions.py"}


def _attention_kinds(tree: ast.Module) -> set[str]:
    """Attention batches choose only types declared by their KindDelivery registry."""
    kinds = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "KindDelivery":
            continue
        for keyword in node.keywords:
            if keyword.arg == "notification_type":
                assert isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str), (
                    "attention notification types must remain statically auditable"
                )
                kinds.add(keyword.value.value)
    assert kinds, "attention delivery must declare its routed notification types"
    return kinds


def _routed_kinds() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for root in ROOTS:
        for path in Path(root).rglob("*.py"):
            tree = ast.parse(path.read_text())
            consts = {
                target.id: node.value.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                for target in node.targets
                if isinstance(target, ast.Name)
            }

            def value(node, consts=consts):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value
                if isinstance(node, ast.Name):
                    return consts.get(node.id)
                return None

            payloads: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                    for key, item in zip(node.value.keys, node.value.values):
                        if isinstance(key, ast.Constant) and key.value == "type" and value(item):
                            for target in node.targets:
                                if isinstance(target, ast.Name):
                                    payloads[target.id] = value(item)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if not name.endswith("notify"):
                    continue
                kind = None
                for keyword in node.keywords:
                    if keyword.arg == "type":
                        kind = value(keyword.value)
                    elif keyword.arg is None and isinstance(keyword.value, ast.Name):
                        kind = kind or payloads.get(keyword.value.id)
                if kind is None and len(node.args) >= 3:
                    kind = value(node.args[2])
                if kind is None and str(path) == "packages/core/attention/delivery.py":
                    for attention_kind in _attention_kinds(tree):
                        found.setdefault(attention_kind, []).append(f"{path}:{node.lineno}")
                    continue
                if kind is None and str(path) in DYNAMIC_SENDERS:
                    continue
                found.setdefault(kind or f"<unresolved {path}:{node.lineno}>", []).append(
                    f"{path}:{node.lineno}"
                )
    return found


def test_every_routed_notification_kind_is_catalogued():
    kinds = _routed_kinds()
    # The walk must actually see the producers it guards.
    assert {"proposal", "agent_initiative_update", "channel_inbound_message",
            "task_comment_mention", "workflow", "task_hitl_reminder",
            "integration_account_issue", "workspace_setup_required"} <= set(kinds)
    missing = {kind: sites for kind, sites in kinds.items() if kind not in EVENTS_BY_KIND}
    assert not missing, f"sent through notify() but not in EVENT_CATALOG: {missing}"
