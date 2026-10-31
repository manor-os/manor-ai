"""Sandbox work runs by default (product decision, 2026-10-06).

E2E on the unified approval engine: every sandbox command classified to a
``sandbox.*`` action key fell through to impact ``unknown``, and unknown
impact asks in every mode, ``automatic`` included. A PPT request therefore
stopped at its first ``sandbox exec`` until someone approved it. A sandbox is
an isolated, disposable container, so its actions are reversible effects:
they run under ``risk_based`` and ``automatic``, while ``confirm_changes`` and
an explicit user rule still ask.
"""

from __future__ import annotations

import pytest

from approval_policy_test_utils import evaluate_runtime_action
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.governance.approval_facts import SANDBOX_TOOL_ACTION_KEYS, action_facts

# Every agent-visible sandbox call shape the classifier distinguishes.
SANDBOX_CALLS = [
    ("sandbox", {"action": "create"}),
    ("sandbox", {"action": "exec", "params": {"sandbox_id": "sbx", "command": "python build.py"}}),
    ("sandbox", {"action": "exec", "params": {"sandbox_id": "sbx", "command": "ls /skill"}}),
    ("sandbox", {"action": "exec", "params": {"sandbox_id": "sbx", "command": "echo hi > /tmp/a && mv /tmp/a /tmp/b"}}),
    ("sandbox", {"action": "write_file", "params": {"sandbox_id": "sbx", "path": "/skill/a.txt", "content": "x"}}),
    ("sandbox", {"action": "respond", "params": {"sandbox_id": "sbx", "execution_id": "e", "event_id": "v", "payload": {}}}),
    ("sandbox", {"action": "cancel", "params": {"sandbox_id": "sbx", "execution_id": "e"}}),
    ("sandbox", {"action": "status", "params": {"sandbox_id": "sbx"}}),
    ("sandbox", {"action": "read_file", "params": {"sandbox_id": "sbx", "path": "/skill/a.txt"}}),
    ("sandbox", {"action": "save_result", "params": {"sandbox_id": "sbx", "path": "/skill/a.txt", "filename": "a.txt"}}),
    ("sandbox", {"action": "destroy", "params": {"sandbox_id": "sbx"}}),
]


def _sandbox_actions():
    actions = []
    for tool, args in SANDBOX_CALLS:
        classification = classify_runtime_tool(tool, args)
        if classification.action is not None:
            actions.append((args["action"], classification.action))
    return actions


def test_every_classified_sandbox_call_is_a_sandbox_action() -> None:
    from packages.core.ai.runtime.composite_tools import SandboxToolAction

    # Every action the Sandbox tool exposes has a call shape here.
    assert {args["action"] for _, args in SANDBOX_CALLS} == set(SandboxToolAction.values())
    actions = _sandbox_actions()
    assert {name for name, _ in actions} >= {"exec", "write_file", "respond", "cancel"}
    # save_result reaches the user's files as a workspace file create, not a
    # sandbox effect; every other classified call is a Sandbox tool action.
    sandbox_keys = {action.action_key for name, action in actions if name != "save_result"}
    assert sandbox_keys == SANDBOX_TOOL_ACTION_KEYS


def test_sandbox_namespace_actions_outside_the_tool_keep_the_generic_rule(monkeypatch) -> None:
    # Undeclared sandbox-namespace keys stay unknown (production; the suite's
    # strict contract would raise on them instead).
    monkeypatch.delenv("MANOR_STRICT_EFFECT_CONTRACT", raising=False)
    for key in ("sandbox", "sandbox.new_capability"):
        assert key not in SANDBOX_TOOL_ACTION_KEYS
        assert action_facts(action_key=key).impact == "unknown", key


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "destroy"])
async def test_opening_or_releasing_a_sandbox_never_reaches_the_approval_policy(monkeypatch, action) -> None:
    # Product decision: sandbox creation is allowed in every situation (any
    # mode, any rule), like release. The permission boundary still applies.
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from packages.core import database
    from packages.core.ai.runtime import approval_service, envelope
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.governance import approvals
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization.domain import PermissionDecision

    arguments = {"action": action, "params": {"sandbox_id": "owned"}}
    classification = classify_runtime_tool("sandbox", arguments)
    assert classification.effect is RuntimeToolEffect.CONTROL and classification.action is None

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    monkeypatch.setattr(database, "async_session", session)
    permission = AsyncMock(return_value=PermissionDecision.allow())
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", permission)
    monkeypatch.setattr(envelope, "runtime_effective_tool_scope",
                        lambda **kwargs: SimpleNamespace(searchable_tool_names=lambda: {"sandbox"}))
    policy = AsyncMock()
    monkeypatch.setattr(approvals, "_policy_decision", policy)

    assert await approval_service.guard_runtime_tool_action(
        name="sandbox", arguments=arguments, entity_id="entity", user_id="user",
        workspace_id=None, conversation_id="conversation", classification=classification,
    ) is None
    permission.assert_awaited_once()
    policy.assert_not_awaited()


@pytest.mark.parametrize("mode", ["risk_based", "automatic"])
def test_sandbox_work_runs_by_default(mode) -> None:
    for name, action in _sandbox_actions():
        decision = evaluate_runtime_action(action, mode=mode)
        assert decision.outcome == "allow", (name, action.action_key, decision)


def test_confirm_changes_and_explicit_rules_still_ask() -> None:
    for name, action in _sandbox_actions():
        assert evaluate_runtime_action(action, mode="confirm_changes").outcome == "ask", name
        ruled = evaluate_runtime_action(action, rules=[{
            "id": "ask-sandbox", "effect": "ask",
            "selector": {"kind": "action", "action_key": action.action_key},
        }])
        assert ruled.outcome == "ask", name


def test_other_unrecognised_actions_still_ask(monkeypatch) -> None:
    # The sandbox exception does not widen the unknown-impact safety rule
    # (production behaviour; the suite's strict contract raises instead).
    monkeypatch.delenv("MANOR_STRICT_EFFECT_CONTRACT", raising=False)
    for key in ("custom.operation", "sandboxed.thing", "unclassified.action"):
        assert action_facts(action_key=key).impact == "unknown", key
