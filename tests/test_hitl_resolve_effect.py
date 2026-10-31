"""Resolving a card has the effect of the operation the card guards (I2).

The chain: a Workspace change is drafted behind a review card; any later user
message lets an agent relay "approve" through resolve_hitl. Approving that
card applies the draft, so it is judged as ``workspace.operation.apply`` (its
own classified high risk), rejecting it as ``workspace.operation.discard``.
An unresolvable target stays undeclared and asks. The user's mode decides.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from packages.core import database
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Message
from test_effect_contract_default_policy import _Session, _scope


async def _review_card(db, conversation_id):
    draft_id = generate_ulid()
    card = Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant",
        content="Review workspace runtime changes", author_kind="agent", message_kind="hitl_request",
        pending_action={
            "kind": "workspace_operation_review", "draft_id": draft_id,
            "approval_token": f"workspace_operation:{draft_id}:fp",
            "action": "workspace.operation.apply", "tool": "workspace_operation",
            "operation": {"kind": "workspace_operation_review", "draft_id": draft_id,
                          "patches": [{"op": "rule.add", "payload": {"description": "x"}}]},
        },
    )
    db.add(card)
    await db.commit()
    return card


async def _resolve(db, monkeypatch, *, mode, decision, target="card", rules=()):
    monkeypatch.setattr(database, "async_session", lambda: _Session(db))
    task, thread, envelope = await _scope(db, mode=mode, rules=rules)
    card = await _review_card(db, thread.id)
    envelope = replace(envelope, allowed_tool_names=("workspace_resolve_hitl",))
    args = {"action": decision, "message_id": card.id if target == "card" else generate_ulid()}
    result = await guard_runtime_tool_action(
        name="workspace_resolve_hitl", arguments=args, entity_id=envelope.entity_id,
        user_id=envelope.user_id, workspace_id=envelope.workspace_id, conversation_id=thread.id,
        task_id=task.id, runtime_envelope=envelope, operation_id=f"runtime:{task.id}:call:resolve",
    )
    requests = list((await db.execute(select(HitlRequest).where(
        HitlRequest.entity_id == envelope.entity_id))).scalars().all())
    return (None if result is None else json.loads(result).get("error")), requests


@pytest.mark.asyncio
async def test_relaying_approve_is_judged_as_applying_the_draft(db_session, monkeypatch):
    blocked, requests = await _resolve(db_session, monkeypatch, mode="risk_based", decision="approve")

    assert blocked == "approval_required"
    assert len(requests) == 1 and requests[0].action_key == "workspace.hitl.resolve"
    facts = requests[0].context["approval_facts"]
    assert (facts["impact"], facts["category"], facts["risk_level"]) == ("reversible", "workspace", "high")


@pytest.mark.asyncio
async def test_relaying_approve_follows_an_automatic_mode(db_session, monkeypatch):
    blocked, requests = await _resolve(db_session, monkeypatch, mode="automatic", decision="approve")

    assert blocked is None and requests == []


@pytest.mark.asyncio
async def test_an_explicit_rule_on_the_guarded_operation_wins(db_session, monkeypatch):
    from tests.approval_settings_test_utils import action_rule

    blocked, _requests = await _resolve(
        db_session, monkeypatch, mode="automatic", decision="approve",
        rules=(action_rule("workspace.operation.apply", effect="deny"),),
    )

    assert blocked == "blocked_by_governance"


@pytest.mark.asyncio
@pytest.mark.parametrize(("rule", "blocked"), [
    # An allow on the relay's own key never approves applying the draft.
    (("workspace.hitl.resolve", "allow"), "approval_required"),
    (("workspace.hitl.resolve", "deny"), "blocked_by_governance"),
    (("workspace.operation.apply", "allow"), None),
])
async def test_only_the_guarded_operations_rules_can_allow_the_relay(db_session, monkeypatch, rule, blocked):
    from tests.approval_settings_test_utils import action_rule

    result, _requests = await _resolve(
        db_session, monkeypatch, mode="risk_based", decision="approve",
        rules=(action_rule(rule[0], effect=rule[1]),),
    )

    assert result == blocked


@pytest.mark.asyncio
async def test_relaying_reject_discards_and_follows_risk_based(db_session, monkeypatch):
    blocked, requests = await _resolve(db_session, monkeypatch, mode="risk_based", decision="reject")

    assert blocked is None and requests == []


@pytest.mark.asyncio
async def test_an_unresolvable_card_is_undeclared_and_asks_by_risk(db_session, monkeypatch):
    blocked, requests = await _resolve(db_session, monkeypatch, mode="risk_based", decision="approve",
                                       target="missing")

    assert blocked == "approval_required"
    assert requests[0].context["approval_facts"]["impact"] == "unknown"


@pytest.mark.asyncio
async def test_an_unresolvable_card_follows_an_automatic_mode(db_session, monkeypatch):
    # An unnamed effect follows the user's mode (#1027 / aa873d444).
    blocked, requests = await _resolve(db_session, monkeypatch, mode="automatic", decision="approve",
                                       target="missing")

    assert blocked is None and requests == []


def test_hitl_resolve_is_resolved_at_the_gate_not_declared():
    from packages.core.governance.action_effects import GATE_RESOLVED_ACTION_KEYS, declared_action_effect

    assert "workspace.hitl.resolve" in GATE_RESOLVED_ACTION_KEYS
    assert declared_action_effect("workspace.hitl.resolve") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm_changes", "risk_based", "automatic"])
async def test_relaying_the_users_own_answer_is_human_input(db_session, monkeypatch, mode):
    """respond only delivers the user's reply to an input card (the handler
    refuses decision cards), so it is the user's own input: leaf_only."""
    blocked, requests = await _resolve(db_session, monkeypatch, mode=mode, decision="respond")

    assert blocked is None and requests == []


def test_respond_facts_are_the_human_input_declaration():
    from packages.core.governance.approval_policy import ActionFacts, ApprovalScope, evaluate_policy
    from packages.core.governance.approval_policy_service import build_policy_chain
    from packages.core.governance.action_effects import declared_action_effect

    human_input = declared_action_effect("human.input.request")
    facts = ActionFacts(action_key="workspace.hitl.resolve", impact=human_input.impact,
                        category=human_input.category, gate=human_input.gate.value)
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="a", entity_id="e"),
                               root={"revision": 1, "policy": {"mode": "confirm_changes", "rules": []}})
    # A known leaf_only fact: even Confirm changes does not ask for it.
    assert evaluate_policy(chain, facts).outcome == "allow"
