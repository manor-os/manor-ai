"""Approval exceptions use the inherited evaluator and canonical action facts."""
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import packages.core.governance.service as gov
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ActionSelector, ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance.policy import PolicyDecision


def _evaluate(rules, *, action="workspace.file.modify", category="files", mode="confirm_changes"):
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="entity", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": mode, "rules": rules}})
    return evaluate_policy(chain, action_facts(action_key=action, category=category, risk_level="low"))


def _rule(effect, *, action=None, category=None, id="rule"):
    return {"id": id, "effect": effect, "selector":
        {"kind": "action", "action_key": action} if action else {"kind": "category", "category": category}}


def test_category_exception_allows_matching_file_action():
    assert _evaluate([_rule("allow", category="files")]).outcome == "allow"


def test_exact_action_does_not_authorize_sibling_action():
    rules = [_rule("allow", action="workspace.file.create")]
    assert _evaluate(rules, action="workspace.file.create").outcome == "allow"
    assert _evaluate(rules).outcome == "ask"


def test_action_selector_rejects_unbounded_wildcard():
    with pytest.raises(ValidationError):
        ActionSelector(action_key="workspace.file.*")


def test_deny_always_wins_over_matching_allow():
    assert _evaluate([_rule("allow", category="files"),
        _rule("deny", action="workspace.file.modify", id="deny")]).outcome == "deny"


def test_unknown_effect_follows_the_chosen_mode(monkeypatch):
    # Production fallback for an undeclared key (tests run the strict contract).
    monkeypatch.delenv("MANOR_STRICT_EFFECT_CONTRACT", raising=False)
    # The user's approval mode decides; an unnamed effect is no exception.
    assert _evaluate([], action="unknown.action", category="unknown", mode="automatic").outcome == "allow"
    assert _evaluate([], action="unknown.action", category="unknown", mode="confirm_changes").outcome == "ask"
    # "Confirm by risk" cannot show an unnamed effect routine; explicit rules still win.
    assert _evaluate([], action="unknown.action", category="unknown", mode="risk_based").outcome == "ask"
    assert _evaluate([_rule("ask", action="unknown.action")], action="unknown.action",
                     category="unknown", mode="automatic").outcome == "ask"
    assert _evaluate([_rule("deny", action="unknown.action")], action="unknown.action",
                     category="unknown", mode="automatic").outcome == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_workspace_auto_approve_adapter_delegates_current_gate(monkeypatch, allowed):
    gate = AsyncMock(return_value=PolicyDecision(allowed))
    monkeypatch.setattr(gov, "check_step_policy", gate)
    assert await gov.workspace_policy_auto_approves(None, workspace_id="ws", action_key="workspace.file.modify") is allowed
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_workspace_cannot_create_an_implicit_grant(monkeypatch):
    gate = AsyncMock()
    monkeypatch.setattr(gov, "check_step_policy", gate)
    assert await gov.workspace_policy_auto_approves(None, workspace_id="", action_key="workspace.file.modify") is False
    gate.assert_not_awaited()
