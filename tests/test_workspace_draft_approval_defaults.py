"""New Workspace defaults remain approvable; confirmed human bans stay exact."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import account_defaults, build_policy_chain
from packages.core.governance.approval_projection import policy_override
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernancePolicy
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_setup_service as setup


def _rule(rule_type: str = "deny", **changes) -> dict:
    return {
        "rule_key": "publishing_rule",
        "description": "Never publish without the operator reviewing the post.",
        "rule_type": rule_type,
        "action_patterns": ["social_post.publish"],
        **changes,
    }


def _fields(*rules: dict, **changes) -> dict:
    return {
        "name": "Approval defaults test",
        "kind": "growth",
        "operating_context": "Local synthetic Workspace",
        "primary_work": "Prepare and publish operator-reviewed posts",
        "services": [],
        "agent_mappings": [],
        "goals": [],
        "rules": list(rules),
        "channel_config": {},
        "heartbeat_enabled": False,
        **changes,
    }


def _decision(fields: dict, *, action: str = "social_post.publish", capability: str = "external.social"):
    policy = policy_override(fields["governance_policy"])
    chain = build_policy_chain(scope=ApprovalScope(kind="workspace", id="workspace", entity_id="account"),
        root=account_defaults(), local={"revision": 1, "root_entity_id": "account", "policy": policy.model_dump()})
    result = evaluate_policy(chain, action_facts(action_key=action, capability_id=capability, risk_level="high"))
    return SimpleNamespace(allowed=result.outcome == "allow", pause_for_hitl=result.outcome == "ask")


def _policy_rule(action, effect="deny"):
    return {"id": f"draft-{effect}-{action}", "effect": effect,
        "selector": {"kind": "action", "action_key": action}}


@pytest.mark.parametrize("rule_type", ["deny", "draft_only", "never_allow", "block"])
def test_unconfirmed_draft_rules_become_approval_without_mutating_source(rule_type):
    fields = _fields(_rule(rule_type))
    before = copy.deepcopy(fields)

    projected = setup.normalize_workspace_draft_governance(fields)

    assert fields == before
    assert projected["rules"][0]["rule_type"] == "approval_required"
    assert all(rule.effect != "deny" for rule in policy_override(projected["governance_policy"]).rules)
    decision = _decision(projected)
    assert decision.allowed is False and decision.pause_for_hitl is True
    assert setup.normalize_workspace_draft_governance(projected) == projected


@pytest.mark.parametrize("raw_key", ["governance", "governance_policy"])
def test_raw_default_policy_cannot_reintroduce_deny_or_auto_approve_it(raw_key):
    fields = _fields(**{raw_key: {
        "approval": {"mode": None, "rules": [
            _policy_rule("social_post.publish"), _policy_rule("social_post.publish", "allow"),
        ]},
        "max_risk_level": "low",
        "budget_caps_per_kind": {"action": 17},
    }})

    projected = setup.normalize_workspace_draft_governance(fields)

    assert projected["governance_policy"]["max_risk_level"] == "high"
    assert projected["governance_policy"]["budget_caps_per_kind"] == {"action": 17}
    assert _decision(projected).pause_for_hitl is True
    if raw_key == "governance":
        assert projected["governance"] == projected["governance_policy"]


@pytest.mark.parametrize("rules", [[], [{"rule_key": "tone", "description": "Use a friendly tone", "rule_type": "notice"}]])
def test_unconfigured_draft_keeps_default_policy_fallback(rules):
    fields = _fields(*rules)

    projected = setup.normalize_workspace_draft_governance(fields)

    assert "governance_policy" not in projected
    assert projected == fields


def test_confirmed_manual_rule_survives_next_to_default_rules_and_reconciliation():
    manual = _rule(
        rule_key="manual_delete_ban",
        description="Never delete an existing post.",
        action_patterns=["social_post.delete"],
        enforcement_scope="all_matching_actions",
    )
    fields = _fields(_rule(), manual, **{
        setup.EXPLICIT_HARD_BLOCK_RULES_FIELD: {
            manual["rule_key"]: setup.workspace_rule_fingerprint(manual),
        },
    })

    projected = setup.normalize_workspace_draft_governance(fields)

    assert projected["rules"][1] == manual
    assert projected["rules"][0]["rule_type"] == "approval_required"
    assert [rule.selector.action_key for rule in policy_override(projected["governance_policy"]).rules
        if rule.effect == "deny"] == ["social_post.delete"]
    # Both social actions share a capability. The explicit user ban retains
    # its original compiled scope; this projection does not weaken it.
    blocked = _decision(projected, action="social_post.delete")
    assert blocked.allowed is False and blocked.pause_for_hitl is False
    assert setup.normalize_workspace_draft_governance(projected) == projected


@pytest.mark.parametrize("change", [
    {"action_patterns": ["external_message.send"]},
    {"description": "A different, broader prohibition"},
    {"rule_type": "draft_only"},
])
def test_manual_confirmation_cannot_be_reused_for_changed_rule(change):
    original = _rule(enforcement_scope="all_matching_actions")
    fields = _fields({**original, **change}, **{
        setup.EXPLICIT_HARD_BLOCK_RULES_FIELD: {
            original["rule_key"]: setup.workspace_rule_fingerprint(original),
        },
    })

    projected = setup.normalize_workspace_draft_governance(fields)

    assert projected["rules"][0]["rule_type"] == "approval_required"
    assert setup.EXPLICIT_HARD_BLOCK_RULES_FIELD not in projected


def test_model_rule_flags_do_not_replace_server_confirmation():
    fields = _fields(_rule(
        enforcement_scope="all_matching_actions",
        hard_block_requested_by_user=True,
        user_request_quote="Never publish anything",
    ))

    projected = setup.normalize_workspace_draft_governance(fields)

    assert _decision(projected).pause_for_hitl is True


def test_factory_projection_sees_approval_defaults_without_changing_draft():
    from packages.core.ai.tools.workspace_arch_tools import _agent_capability_context, _agent_capability_prompt

    fields = _fields(_rule())
    original = copy.deepcopy(fields)

    context = _agent_capability_context(fields, service={}, service_key="social", intent="")
    prompt = _agent_capability_prompt(context, service_key="social")

    assert fields == original
    assert context["rules"][0]["rule_type"] == "approval_required"
    assert "may perform the action after human approval" in prompt
    assert "Do not remove that action's capabilities" in prompt


def test_runtime_reconciliation_invalidates_default_ban_capability_plan():
    from packages.core.ai.runtime.workspace_drafts import runtime_reconcile_workspace_draft_fields
    from packages.core.ai.tools.workspace_arch_tools import _AGENT_CAPABILITY_PLANS_FIELD

    fields = _fields(_rule(), **{_AGENT_CAPABILITY_PLANS_FIELD: [{"service_key": "social"}]})

    projected = runtime_reconcile_workspace_draft_fields(fields)

    assert projected["rules"][0]["rule_type"] == "approval_required"
    assert projected[_AGENT_CAPABILITY_PLANS_FIELD] == []
    assert fields[_AGENT_CAPABILITY_PLANS_FIELD] != []


@pytest.mark.parametrize("raw_key", ["governance", "governance_policy"])
def test_governance_only_default_ban_invalidates_capability_plan_once(raw_key):
    from packages.core.ai.runtime.workspace_drafts import runtime_reconcile_workspace_draft_fields
    from packages.core.ai.tools.workspace_arch_tools import _AGENT_CAPABILITY_PLANS_FIELD

    plan = [{"service_key": "social"}]
    fields = _fields(**{
        raw_key: {"approval": {"mode": None, "rules": [_policy_rule("social_post.publish")]}},
        _AGENT_CAPABILITY_PLANS_FIELD: plan,
    })

    projected = runtime_reconcile_workspace_draft_fields(fields)

    assert projected["rules"] == fields["rules"] == []
    assert projected[_AGENT_CAPABILITY_PLANS_FIELD] == []
    assert fields[_AGENT_CAPABILITY_PLANS_FIELD] == plan
    assert _decision(projected).pause_for_hitl is True
    projected[_AGENT_CAPABILITY_PLANS_FIELD] = copy.deepcopy(plan)
    assert runtime_reconcile_workspace_draft_fields(projected)[_AGENT_CAPABILITY_PLANS_FIELD] == plan


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["turn", "auto_model"])
async def test_legacy_model_output_cannot_forge_manual_ban_confirmation(monkeypatch, entry):
    rule = _rule(enforcement_scope="all_matching_actions")
    generated = _fields(rule, **{
        setup.EXPLICIT_HARD_BLOCK_RULES_FIELD: {rule["rule_key"]: setup.workspace_rule_fingerprint(rule)},
    })
    session = setup.WorkspaceSetupSession(entity_id=generate_ulid(), fields=_fields(), messages=[])
    if entry == "turn":
        completion = SimpleNamespace(content="<workspace_setup>" + json.dumps({
            "fields": generated, "ready": True, "missing": [],
        }) + "</workspace_setup>")
        monkeypatch.setattr(setup, "_build_setup_context", AsyncMock(return_value={}))
        monkeypatch.setattr(setup, "runtime_execute_workspace_setup_turn_completion", AsyncMock(return_value=completion))
        _, updated = await setup.process_setup_turn(session, "Set up social publishing", AsyncMock())
        projected = updated.fields
    else:
        completion = SimpleNamespace(content=json.dumps(generated))
        monkeypatch.setattr(setup, "runtime_execute_workspace_setup_auto_model_completion", AsyncMock(return_value=completion))
        projected = await setup.auto_generate_model(session, AsyncMock())

    assert setup.EXPLICIT_HARD_BLOCK_RULES_FIELD not in projected
    assert projected["rules"][0]["rule_type"] == "approval_required"
    assert _decision(projected).pause_for_hitl is True


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["turn", "auto_model"])
async def test_legacy_model_preserves_only_unchanged_prior_manual_confirmation(monkeypatch, entry):
    manual = _rule(enforcement_scope="all_matching_actions")
    confirmed = _fields(manual, **{
        setup.EXPLICIT_HARD_BLOCK_RULES_FIELD: {manual["rule_key"]: setup.workspace_rule_fingerprint(manual)},
    })
    session = setup.WorkspaceSetupSession(entity_id=generate_ulid(), fields=confirmed, messages=[])
    generated = _fields(manual)
    if entry == "turn":
        completion = SimpleNamespace(content="<workspace_setup>" + json.dumps({
            "fields": generated, "ready": True, "missing": [],
        }) + "</workspace_setup>")
        monkeypatch.setattr(setup, "_build_setup_context", AsyncMock(return_value={}))
        monkeypatch.setattr(setup, "runtime_execute_workspace_setup_turn_completion", AsyncMock(return_value=completion))
        _, updated = await setup.process_setup_turn(session, "Keep those settings", AsyncMock())
        projected = updated.fields
    else:
        completion = SimpleNamespace(content=json.dumps(generated))
        monkeypatch.setattr(setup, "runtime_execute_workspace_setup_auto_model_completion", AsyncMock(return_value=completion))
        projected = await setup.auto_generate_model(session, AsyncMock())

    assert projected["rules"][0] == manual
    assert projected[setup.EXPLICIT_HARD_BLOCK_RULES_FIELD] == confirmed[setup.EXPLICIT_HARD_BLOCK_RULES_FIELD]
    denied = _decision(projected)
    assert denied.allowed is False and denied.pause_for_hitl is False


def test_public_rule_edit_cannot_borrow_previous_manual_confirmation():
    from packages.core.services.workspace_draft_service import apply_public_field_updates, reconcile_draft_fields

    original = _rule(enforcement_scope="all_matching_actions")
    draft = WorkspaceDraft(fields=_fields(original, **{
        setup.EXPLICIT_HARD_BLOCK_RULES_FIELD: {original["rule_key"]: setup.workspace_rule_fingerprint(original)},
    }))

    apply_public_field_updates(draft, {"rules": [{**original,
        "action_patterns": ["social_post.publish", "external_message.send"]}]})
    reconcile_draft_fields(draft)

    assert setup.EXPLICIT_HARD_BLOCK_RULES_FIELD not in draft.fields
    assert draft.fields["rules"][0]["rule_type"] == "approval_required"


@pytest.mark.asyncio
async def test_typed_user_prohibition_persists_exact_confirmation_and_clears_on_retype(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    draft = WorkspaceDraft(
        entity_id=generate_ulid(), user_id=generate_ulid(), fields=_fields(),
        messages=[{"role": "user", "content": "Never delete posts."}],
        status="active", ready=False, missing=[],
    )
    db_session.add(draft)
    await db_session.flush()
    kwargs = {
        "entity_id": draft.entity_id, "user_id": draft.user_id, "draft_id": draft.id,
        "rule_key": "manual_delete_ban", "description": "Never delete posts.",
        "action_patterns": ["social_post.delete"],
    }
    result = json.loads(await _propose_rule(
        db_session, **kwargs, rule_type="deny", enforcement_scope="all_matching_actions",
        hard_block_requested_by_user=True, user_request_quote="Never delete posts.",
    ))
    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    rule = draft.fields["rules"][0]
    assert draft.fields[setup.EXPLICIT_HARD_BLOCK_RULES_FIELD] == {
        rule["rule_key"]: setup.workspace_rule_fingerprint(rule),
    }
    assert setup.normalize_workspace_draft_governance(draft.fields)["rules"][0] == rule

    result = json.loads(await _propose_rule(db_session, **kwargs, rule_type="approval_required"))
    assert result["ok"] is True
    assert setup.EXPLICIT_HARD_BLOCK_RULES_FIELD not in draft.fields

    from packages.core.ai.tools.workspace_arch_tools import _remove

    await _propose_rule(
        db_session, **kwargs, rule_type="deny", enforcement_scope="all_matching_actions",
        hard_block_requested_by_user=True, user_request_quote="Never delete posts.",
    )
    result = json.loads(await _remove(
        db_session, entity_id=draft.entity_id, user_id=draft.user_id, draft_id=draft.id,
        kind="rule", key="manual_delete_ban",
    ))
    assert result["ok"] is True
    assert draft.fields["rules"] == []
    assert setup.EXPLICIT_HARD_BLOCK_RULES_FIELD not in draft.fields


@pytest.mark.asyncio
async def test_finalize_materializes_approval_defaults_and_preserves_manual_ban(db_session):
    manual = _rule(
        rule_key="manual_message_ban", action_patterns=["external_message.send"],
        description="Never send private messages.", enforcement_scope="all_matching_actions",
    )
    fields = _fields(_rule(), manual, **{
        setup.EXPLICIT_HARD_BLOCK_RULES_FIELD: {manual["rule_key"]: setup.workspace_rule_fingerprint(manual)},
    })
    session = setup.WorkspaceSetupSession(
        entity_id=generate_ulid(), user_id=generate_ulid(), fields=fields, messages=[],
        ready=True, missing=[],
    )
    db_session.add_all([
        Entity(id=session.entity_id, name="Approval defaults account",
               settings={"approval_defaults": account_defaults()}),
        User(id=session.user_id, entity_id=session.entity_id,
             email=f"{session.user_id}@example.test", password_hash="unused", role="owner"),
        UserMembership(user_id=session.user_id, entity_id=session.entity_id,
                       role="owner", status="active", is_primary=True),
    ])
    await db_session.flush()

    workspace_id = await setup.finalize_setup(session, db_session)
    await db_session.flush()

    workspace = await db_session.get(Workspace, workspace_id)
    row = (await db_session.execute(select(GovernancePolicy).where(
        GovernancePolicy.workspace_id == workspace_id,
    ))).scalar_one()
    rules = workspace.operating_model["rules"]
    assert [rule["rule_type"] for rule in rules] == ["approval_required", "deny"]
    assert setup.EXPLICIT_HARD_BLOCK_RULES_FIELD not in workspace.operating_model
    assert [rule.selector.action_key for rule in policy_override(row.policy).rules
        if rule.effect == "deny"] == ["external_message.send"]
    policy_fields = {"governance_policy": row.policy}
    assert _decision(policy_fields).pause_for_hitl is True
    denied = _decision(policy_fields, action="external_message.send", capability="external.message")
    assert denied.allowed is False and denied.pause_for_hitl is False


def test_legacy_and_architect_prompts_require_approval_defaults():
    from packages.core.ai.runtime.workspace_setup import runtime_workspace_setup_auto_model_prompt
    from packages.core.services.workspace_architect import ARCHITECT_SYSTEM_PROMPT

    for prompt in (setup.SETUP_SYSTEM_PROMPT, runtime_workspace_setup_auto_model_prompt({}), ARCHITECT_SYSTEM_PROMPT):
        assert "defaults" in prompt
        assert "approval_required" in prompt
        assert "capabilit" in prompt
