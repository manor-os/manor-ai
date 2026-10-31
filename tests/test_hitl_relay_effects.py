"""Every card an agent decides or answers for the user is judged by what it guards.

Owner decision (2026-10-07): an agent relaying the user's decision or answer to
a HITL card is judged by the guarded operation's declared effect under the
user's own mode and explicit rules (automatic runs it; risk_based /
confirm_changes follow the guarded operation; explicit deny/ask rules win).
The only exception is the approval of Strategist-generated plans/proposals,
which keeps its own flow unchanged. Who may relay is not widened.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from packages.core import database
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from tests.approval_settings_test_utils import action_rule
from test_effect_contract_default_policy import _Session, _scope


async def _blocker(db, envelope, task, *, origin_kind="lease", action_key="post_video", provider="tiktok",
                   pending_kind="needs_confirmation", risk_level="low", context=None, entity_id=None,
                   workspace_id=None):
    plan_id, step_id = generate_ulid(), generate_ulid()
    workspace_id = workspace_id or envelope.workspace_id
    db.add(ExecutionPlan(id=plan_id, entity_id=envelope.entity_id, workspace_id=workspace_id,
                         task_id=task.id, status="running", execution_mode="live",
                         approval_required=False, plan_dag={"steps": []}))
    db.add(ExecutionStep(id=step_id, plan_id=plan_id, entity_id=envelope.entity_id,
                         workspace_id=workspace_id, step_key="post", kind="action",
                         provider=provider, action_key=action_key, params={"caption": "hi"},
                         depends_on=[], step_status="waiting_human", risk_level=risk_level,
                         attempt_count=0, max_attempts=1))
    request = HitlRequest(
        id=generate_ulid(), entity_id=entity_id or envelope.entity_id, workspace_id=workspace_id,
        origin_kind=origin_kind, origin_step_id=step_id, origin_plan_id=plan_id,
        origin_task_id=task.id, action_key=action_key, risk_level=risk_level, status="pending",
        dedup_key=f"lease:{generate_ulid()}", context={"pending_kind": pending_kind, **(context or {})},
    )
    db.add(request)
    await db.commit()
    return request


async def _relay(db, monkeypatch, *, mode=None, rules=(), args_for, setup=None):
    monkeypatch.setattr(database, "async_session", lambda: _Session(db))
    task, thread, envelope = await _scope(db, mode=mode, rules=rules)
    envelope = replace(envelope, allowed_tool_names=("answer_task_blocker", "workspace_resolve_hitl"))
    target = await setup(db, envelope, task, thread) if setup else None
    name, args = args_for(target)
    result = await guard_runtime_tool_action(
        name=name, arguments=dict(args), entity_id=envelope.entity_id, user_id=envelope.user_id,
        workspace_id=envelope.workspace_id, conversation_id=thread.id, task_id=task.id,
        runtime_envelope=envelope, operation_id=f"runtime:{task.id}:call:relay",
    )
    cards = list((await db.execute(select(HitlRequest).where(
        HitlRequest.entity_id == envelope.entity_id,
        HitlRequest.action_key.in_(["workspace.task.update", "workspace.hitl.resolve"]),
    ))).scalars().all())
    return (None if result is None else json.loads(result).get("error")), cards


def _confirm(request):
    return "answer_task_blocker", {"request_id": request.id, "confirm": True}


async def _confirmation(db, envelope, task, _thread):
    return await _blocker(db, envelope, task)


# ── answer_task_blocker: confirming a step's destructive/external action ──


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "rules", "blocked"), [
    ("automatic", (), None),                                       # the user's mode runs it
    ("risk_based", (), "approval_required"),                       # social_post.publish: external
    ("automatic", (action_rule("social_post.publish", effect="deny"),), "blocked_by_governance"),
    ("automatic", (action_rule("social_post.publish", effect="ask"),), "approval_required"),
    ("automatic", (action_rule("post_video", effect="deny"),), "blocked_by_governance"),  # raw step key
    ("automatic", (action_rule("workspace.task.update", effect="deny"),), "blocked_by_governance"),  # relay key
])
async def test_confirming_a_step_is_judged_as_the_step_action(db_session, monkeypatch, mode, rules, blocked):
    error, cards = await _relay(db_session, monkeypatch, mode=mode, rules=rules,
                                setup=_confirmation, args_for=_confirm)
    assert error == blocked
    if blocked == "approval_required":
        facts = cards[0].context["approval_facts"]
        assert (facts["impact"], facts["category"], facts["risk_level"]) == ("external", "publishing", "high")


async def _destructive_confirmation(db, envelope, task, _thread):
    return await _blocker(db, envelope, task, action_key="workspace.file.delete", provider=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("rules", "blocked"), [
    # A rule on the relay's generic key can only restrict a guarded relay:
    # a past "always approve" on task updates never approves the deletion.
    ((action_rule("workspace.task.update", effect="allow"),), "approval_required"),
    ((action_rule("workspace.task.update", effect="deny"),), "blocked_by_governance"),
    ((action_rule("workspace.task.update", effect="ask"),), "approval_required"),
    # Allows come from the guarded operation's own key.
    ((action_rule("workspace.file.delete", effect="allow"),), None),
    ((action_rule("workspace.file.delete", effect="allow"),
      action_rule("workspace.task.update", effect="deny")), "blocked_by_governance"),
])
async def test_only_the_guarded_operations_rules_can_allow_a_relay(db_session, monkeypatch, rules, blocked):
    error, cards = await _relay(db_session, monkeypatch, mode="risk_based", rules=rules,
                                setup=_destructive_confirmation, args_for=_confirm)
    assert error == blocked
    if blocked == "approval_required":
        facts = cards[0].context["approval_facts"]
        assert (facts["impact"], facts["category"]) == ("destructive", "deletion")


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "blocked"), [
    ("automatic", None), ("risk_based", "approval_required"), ("confirm_changes", "approval_required"),
])
async def test_reconciling_an_effect_is_judged_as_the_step_action(db_session, monkeypatch, mode, blocked):
    """Deciding whether an external action already happened can lead to a replay."""
    async def setup(db, envelope, task, _thread):
        return await _blocker(db, envelope, task, pending_kind="effect_reconciliation")

    error, cards = await _relay(db_session, monkeypatch, mode=mode, setup=setup,
                                args_for=lambda r: ("answer_task_blocker", {"request_id": r.id, "answer": "it did not post"}))
    assert error == blocked
    if blocked:
        facts = cards[0].context["approval_facts"]
        assert (facts["impact"], facts["category"]) == ("external", "publishing")


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "blocked"), [("automatic", None), ("risk_based", "approval_required")])
async def test_a_blocker_in_another_workspace_is_judged_as_its_guarded_action(db_session, monkeypatch, mode, blocked):
    """The gate reads the row the handler acts on (scoped by entity, as the handler is)."""
    from packages.core.models.workspace import Workspace

    async def setup(db, envelope, task, _thread):
        other = Workspace(id=generate_ulid(), entity_id=envelope.entity_id, name="Other",
                          status="active", operating_model={})
        db.add(other)
        await db.flush()
        return await _blocker(db, envelope, task, workspace_id=other.id)

    error, cards = await _relay(db_session, monkeypatch, mode=mode, setup=setup, args_for=_confirm)
    assert error == blocked
    if blocked:
        assert cards[0].context["approval_facts"]["category"] == "publishing"


@pytest.mark.asyncio
async def test_refusing_runs_nothing_and_follows_the_relays_own_effect(db_session, monkeypatch):
    error, cards = await _relay(db_session, monkeypatch, mode="risk_based", setup=_confirmation,
                                args_for=lambda r: ("answer_task_blocker", {"request_id": r.id, "refuse": True}))
    assert error is None and cards == []


# ── answer_task_blocker: answering an input request is the user's own input ──


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind", ["human_input", "needs_input"])
@pytest.mark.parametrize("mode", ["confirm_changes", "risk_based"])
async def test_answering_an_input_blocker_is_human_input(db_session, monkeypatch, pending_kind, mode):
    async def setup(db, envelope, task, _thread):
        return await _blocker(db, envelope, task, pending_kind=pending_kind)

    error, cards = await _relay(db_session, monkeypatch, mode=mode, setup=setup,
                                args_for=lambda r: ("answer_task_blocker", {"request_id": r.id, "answer": "Tuesday"}))
    assert error is None and cards == []


@pytest.mark.asyncio
async def test_answering_a_task_question_is_human_input(db_session, monkeypatch):
    async def setup(db, envelope, task, _thread):
        return await _blocker(db, envelope, task, origin_kind="task", action_key="task.request_input",
                              provider=None, pending_kind="human_input")

    error, cards = await _relay(db_session, monkeypatch, mode="confirm_changes", setup=setup,
                                args_for=lambda r: ("answer_task_blocker", {"request_id": r.id, "answer": "yes"}))
    assert error is None and cards == []


# ── delegated (coordinator-owned) child approvals ──


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "blocked"), [("automatic", None), ("risk_based", "approval_required")])
async def test_granting_a_delegated_child_approval_is_judged_as_the_child_call(db_session, monkeypatch, mode, blocked):
    async def setup(db, envelope, task, _thread):
        # The delegated child's own runtime request, judged when it was minted.
        nested = HitlRequest(
            id=generate_ulid(), entity_id=envelope.entity_id, workspace_id=envelope.workspace_id,
            origin_kind="tool_call", action_key="email.send", risk_level="high", status="pending",
            dedup_key=f"tool:{generate_ulid()}", context={"approval_facts": {
                "action_key": "email.send", "impact": "external", "category": "communication",
                "gate": "policy", "risk_level": "high",
            }},
        )
        db.add(nested)
        # The parent's lease blocker carries only a binding to that request
        # (and the parent's own step, which is not what the grant runs).
        return await _blocker(
            db, envelope, task, action_key="workspace.service.delegate", provider=None,
            pending_kind="human_input", context={"coordinator_nested_approval": {"request_id": nested.id}})

    error, cards = await _relay(db_session, monkeypatch, mode=mode, setup=setup,
                                args_for=lambda r: ("answer_task_blocker", {"request_id": r.id, "answer": "go"}))
    assert error == blocked
    if blocked:
        assert cards[0].context["approval_facts"]["category"] == "communication"


# ── relays that cannot be resolved, or are not the caller's ──


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "blocked"), [("automatic", None), ("risk_based", "approval_required")])
async def test_an_unresolvable_blocker_is_unknown(db_session, monkeypatch, mode, blocked):
    error, _cards = await _relay(db_session, monkeypatch, mode=mode,
                                 args_for=lambda _r: ("answer_task_blocker", {"request_id": generate_ulid(), "confirm": True}))
    assert error == blocked


@pytest.mark.asyncio
async def test_another_entitys_request_is_never_resolved_for_this_caller(db_session, monkeypatch):
    async def setup(db, envelope, task, _thread):
        return await _blocker(db, envelope, task, entity_id=generate_ulid())

    error, cards = await _relay(db_session, monkeypatch, mode="risk_based", setup=setup, args_for=_confirm)
    # The foreign row is not loaded: the relay is unknown (asks) and the
    # handler's own entity check still refuses it.
    assert error == "approval_required"
    assert cards[0].context["approval_facts"]["impact"] == "unknown"


def test_every_surface_naming_the_blocker_relay_is_resolved():
    from packages.core.ai.runtime.approval_service import _relayed_blocker_call

    params = {"request_id": "r1", "confirm": True}
    assert _relayed_blocker_call("answer_task_blocker", params) == params
    assert _relayed_blocker_call("workspace_agent", {"action": "answer_task_blocker", "params": params}) == params
    assert _relayed_blocker_call("manor", {"action": "workspace", "params": {
        "action": "answer_task_blocker", "params": params}}) == params
    assert _relayed_blocker_call("workspace_update_task_runtime", params) is None


# ── the Strategist exception ──


@pytest.mark.asyncio
async def test_strategist_proposal_approval_keeps_its_own_flow(db_session):
    """Approving Strategist plans/proposals is not a relay: its facts stay the
    proposal cohort's (external/automation), no agent relay path accepts it,
    and canonicalisation leaves its subject untouched."""
    from packages.core.governance.approvals import ApprovalSubject, canonical_step_subject, subject_policy_facts
    from packages.core.proposals.constants import STRATEGIST_ACTION_KEYS
    from packages.core.services.task_blockers import answer_task_blocker

    for key in STRATEGIST_ACTION_KEYS:
        subject = ApprovalSubject(entity_id="e", workspace_id="w", action_key=key, resource_kind="proposal",
                                  resource_id="p1", risk_level="medium")
        assert canonical_step_subject(subject) is subject
        facts = subject_policy_facts(subject)
        assert (facts.impact, facts.category, facts.gate) == ("external", "automation", "policy")

    request = HitlRequest(id=generate_ulid(), entity_id=generate_ulid(), origin_kind="operation",
                          action_key="workspace.proposal.task", risk_level="medium", status="pending",
                          dedup_key=f"proposal:{generate_ulid()}", context={"plane": "strategist_proposal"})
    db_session.add(request)
    await db_session.flush()
    result = await answer_task_blocker(db_session, entity_id=request.entity_id, user_id="u",
                                       request_id=request.id, confirm=True)
    assert result["resolved"] is False and result["reason"] == "not_a_task_blocker"


@pytest.mark.asyncio
async def test_resolve_hitl_cannot_relay_a_proposal_card(db_session, monkeypatch):
    from packages.core.models.task import Message

    async def setup(db, envelope, task, thread):
        card = Message(id=generate_ulid(), conversation_id=thread.id, role="assistant", content="Approve plan?",
                       author_kind="agent", message_kind="hitl_request",
                       pending_action={"kind": "approve_proposals", "proposal_id": "p1"})
        db.add(card)
        await db.commit()
        return card

    error, cards = await _relay(db_session, monkeypatch, mode="risk_based", setup=setup,
                                args_for=lambda c: ("workspace_resolve_hitl", {"action": "approve", "message_id": c.id}))
    # Not a relayable card kind: the gate judges it unknown (asks) and the
    # handler only ever touches Workspace operation reviews.
    assert error == "approval_required"
    assert cards[0].context["approval_facts"]["impact"] == "unknown"

def test_rules_on_a_restrict_only_key_can_deny_or_ask_but_never_allow():
    from packages.core.governance.approval_policy import ActionFacts, ApprovalScope, evaluate_policy
    from packages.core.governance.approval_policy_service import build_policy_chain

    facts = ActionFacts(action_key="workspace.task.update", impact="destructive", category="deletion",
                        aliases=["workspace.file.delete"], restrict_only_keys=["workspace.task.update"])

    def outcome(*rules):
        chain = build_policy_chain(scope=ApprovalScope(kind="account", id="a", entity_id="e"),
                                   root={"revision": 1, "policy": {"mode": "risk_based", "rules": [
                                       {"id": f"r{index}", **rule} for index, rule in enumerate(rules)]}})
        return evaluate_policy(chain, facts).outcome

    assert outcome(action_rule("workspace.task.update", effect="allow")) == "ask"
    assert outcome(action_rule("workspace.task.update", effect="deny")) == "deny"
    assert outcome(action_rule("workspace.task.update", effect="ask")) == "ask"
    assert outcome(action_rule("workspace.file.delete", effect="allow")) == "allow"
    assert outcome({"effect": "allow", "selector": {"kind": "category", "category": "deletion"}}) == "allow"
