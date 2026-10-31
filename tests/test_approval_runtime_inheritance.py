"""Runtime regression coverage for the single inherited approval gate."""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.constants.approvals import ApprovalOutcome, ApprovalStatus
from packages.core.governance import approvals, service
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance.policy import PolicyDecision, PolicyError, policy_from_dict
from packages.core.services.runtime_authorization.domain import PermissionDecision


def _chain(mode="risk_based", *, rules=(), local_rules=(), local_mode=None):
    return build_policy_chain(
        scope=ApprovalScope(kind="workspace", id="ws", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": mode, "rules": list(rules)}},
        local={"root_entity_id": "entity", "policy": {"mode": local_mode, "rules": list(local_rules)}},
        local_revision=1,
    )


def _rule(effect, selector, id="rule"):
    return {"id": id, "effect": effect, "selector": selector, "source_kind": "user"}


@pytest.mark.parametrize("origin_kind", ["step", "tool_call", "operation"])
@pytest.mark.asyncio
async def test_existing_pending_is_not_released_by_automatic_mode(monkeypatch, origin_kind):
    req = SimpleNamespace(status=ApprovalStatus.PENDING, reason="review this content", matched_rule="old-ask", action_key="external_message.send", resource_id=None, context={})
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(True, reason="mode.automatic")))
    monkeypatch.setattr(approvals, "_find_open_request", AsyncMock(return_value=req))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=req))
    mint = AsyncMock()
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    result = await approvals.resolve_approval(
        None, subject=approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send"),
        origin=approvals.ApprovalOrigin(kind=origin_kind, step_id="step", conversation_id="conv"),
        permission_decision=PermissionDecision.allow(),
    )
    assert result.outcome == ApprovalOutcome.NEEDS_HUMAN
    assert result.request is req
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_hard_deny_precedes_existing_grant(monkeypatch):
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(False, reason="root deny")))
    lookup = AsyncMock(return_value=SimpleNamespace(status=ApprovalStatus.GRANTED))
    monkeypatch.setattr(approvals, "_find_open_request", lookup)
    result = await approvals.resolve_approval(None,
        subject=approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send"),
        origin=approvals.ApprovalOrigin(kind="tool_call", conversation_id="conv"),
        permission_decision=PermissionDecision.allow())
    assert result.outcome == ApprovalOutcome.DENY
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_always_cannot_mutate_request(monkeypatch):
    req = SimpleNamespace(status=ApprovalStatus.PENDING)
    with pytest.raises(ValueError, match="versioned"):
        await approvals.grant_approval(None, req, by_user_id="actor", via="chat", standing=True)
    assert req.status == ApprovalStatus.PENDING


@pytest.mark.parametrize("mode,operation,expected", [
    ("confirm_changes", "modify", "ask"), ("risk_based", "modify", "allow"),
    ("risk_based", "delete", "ask"), ("automatic", "delete", "allow"),
])
def test_same_file_facts_across_execution_surfaces(mode, operation, expected):
    # Chat, Room file adapters and Task steps feed the same canonical facts;
    # neither the caller surface nor a running Workflow grants consent.
    facts = action_facts(action_key=f"workspace.file.{operation}", resource_kind="file",
        capability_id="file.write", operation=operation, risk_level="medium")
    assert evaluate_policy(_chain(mode), facts).outcome == expected


def test_unknown_effect_follows_the_inherited_mode(monkeypatch):
    # Production fallback for an undeclared key (tests run the strict contract).
    monkeypatch.delenv("MANOR_STRICT_EFFECT_CONTRACT", raising=False)
    # "Run automatically" means it; "Confirm by risk" still asks for an
    # effect it cannot show routine.
    facts = action_facts(action_key="provider.custom_action", risk_level="low")
    assert evaluate_policy(_chain("automatic"), facts).outcome == "allow"
    assert evaluate_policy(_chain("risk_based"), facts).outcome == "ask"
    assert evaluate_policy(_chain("confirm_changes"), facts).outcome == "ask"


def test_one_allowed_target_does_not_cover_another_target():
    chain = _chain("confirm_changes", local_rules=[_rule("allow", {
        "kind": "resource", "action_key": "workspace.file.modify", "resource_kind": "file",
        "account_id": "account-a", "resource_ids": ["one"],
    })])
    facts = action_facts(action_key="workspace.file.modify", capability_id="file.write",
        resource_kind="file", resource_ids=("one", "two"), account_id="account-a")
    assert evaluate_policy(chain, facts).outcome == "ask"


def test_root_deny_cannot_be_overridden_by_local_automatic():
    chain = _chain(rules=[_rule("deny", {"kind": "category", "category": "files"})], local_mode="automatic")
    assert evaluate_policy(chain, action_facts(action_key="workspace.file.modify", capability_id="file.write")).outcome == "deny"


@pytest.mark.asyncio
async def test_running_workflow_identity_is_not_approval():
    assert not await service.automated_run_always_allowed(None, entity_id="e", workspace_id="ws", user_id="u", workflow_run_id="running")


@pytest.mark.asyncio
async def test_provider_pending_cannot_be_auto_confirmed(monkeypatch):
    from packages.core.ai.runtime import approval_service
    from packages.core.ai.runtime import provider_approvals
    from packages.core import database

    request = {"provider": "chrome", "provider_approval_id": "same-request",
        "confirmation_tool": "mcp__chrome__confirm_action", "confirmation_arguments": {},
        "retry_tool": "mcp__chrome__click", "retry_arguments": {},
        "action_key": "social.publish", "expires_at": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(provider_approvals, "normalize_provider_approval", lambda *args: request)
    monkeypatch.setattr(approvals, "find_requests_by_dedup", AsyncMock(return_value=[SimpleNamespace(status="pending")]))
    policy_allow = AsyncMock(return_value=True)
    monkeypatch.setattr(approval_service, "_standing_grant", policy_allow)
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()
    monkeypatch.setattr(database, "async_session", session)
    execute = AsyncMock()
    original = '{"approval_required": true}'
    result = await approval_service.runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__click", arguments={}, result=original, execute=execute,
        entity_id="e", user_id="u", workspace_id="ws")
    assert result == original
    execute.assert_not_awaited()
    policy_allow.assert_not_awaited()


@pytest.mark.parametrize("field", ["never_allow_actions", "hitl_required_actions", "auto_approve_actions", "automated_runs_always_allow"])
def test_online_workspace_schema_rejects_retired_fields(field):
    with pytest.raises(PolicyError, match="Retired"):
        policy_from_dict({field: []})


@pytest.mark.parametrize("status", ["denied", "consumed", "expired"])
@pytest.mark.asyncio
async def test_same_operation_cannot_replay_after_terminal_decision(monkeypatch, status):
    req = SimpleNamespace(status=status, action_key="external_message.send", resource_id=None,
        context={"args_hash": "same"}, reason="decision", matched_rule="old")
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(True)))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=req))
    mint = AsyncMock()
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    result = await approvals.resolve_approval(None,
        subject=approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send"),
        origin=approvals.ApprovalOrigin(kind="tool_call", operation_id="run:call", args_hash="same"),
        permission_decision=PermissionDecision.allow())
    assert result.outcome == ApprovalOutcome.DENY
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_operation_changed_payload_cannot_use_grant(monkeypatch):
    req = SimpleNamespace(status="granted", action_key="external_message.send", resource_id=None,
        context={"args_hash": "old"}, reason="decision", matched_rule="old")
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(True)))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=req))
    result = await approvals.resolve_approval(None,
        subject=approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send"),
        origin=approvals.ApprovalOrigin(kind="tool_call", operation_id="run:call", args_hash="changed"),
        permission_decision=PermissionDecision.allow())
    assert result.outcome == ApprovalOutcome.DENY
    assert result.matched_rule == "approval.operation_changed"


def test_identical_arguments_from_new_user_run_have_distinct_identity():
    subject = approvals.ApprovalSubject(entity_id="e", action_key="external_message.send")
    first = approvals.ApprovalOrigin(kind="tool_call", operation_id="runtime:run1:call:call1", args_hash="same")
    second = approvals.ApprovalOrigin(kind="tool_call", operation_id="runtime:run2:call:call1", args_hash="same")
    assert approvals.dedup_key_for(subject, first) != approvals.dedup_key_for(subject, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["read_file", "update_goal_value"])
async def test_reads_and_business_control_do_not_skip_policy(monkeypatch, tool_name):
    from packages.core.ai.runtime import approval_service, envelope
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.services import runtime_authorization
    from packages.core import database
    classification = classify_runtime_tool(tool_name, {"path": "one", "goal_id": "goal", "value": 1})
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()
    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", AsyncMock(return_value=PermissionDecision.allow()))
    monkeypatch.setattr(envelope, "runtime_effective_tool_scope", lambda **kwargs: SimpleNamespace(searchable_tool_names=lambda: {tool_name}))
    policy_gate = AsyncMock(return_value=PolicyDecision(False, reason="explicit root denial"))
    monkeypatch.setattr(approvals, "_policy_decision", policy_gate)
    result = await approval_service.guard_runtime_tool_action(name=tool_name, arguments={"path": "one"},
        entity_id="e", user_id="u", workspace_id=None, conversation_id="conv",
        classification=classification, operation_id="runtime:run:call:one")
    assert "blocked_by_governance" in result
    policy_gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_independent_requirement_does_not_reuse_old_grant(monkeypatch):
    old = SimpleNamespace(status="granted", action_key="external_message.send", resource_id=None,
        context={"args_hash": "same", "approval_requirements": []}, reason="review", matched_rule="old")
    new = SimpleNamespace(status="pending", reason="new review", matched_rule="new")
    explanation = {"outcome": "ask", "rule_id": "new", "scope": {"kind": "account", "id": "e"}, "source": "rule"}
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(False, True, "new review", "new", explanation)))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=old))
    mint = AsyncMock(return_value=new)
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    result = await approvals.resolve_approval(SimpleNamespace(flush=AsyncMock()),
        subject=approvals.ApprovalSubject(entity_id="e", workspace_id="ws", action_key="external_message.send"),
        origin=approvals.ApprovalOrigin(kind="tool_call", operation_id="run:call", args_hash="same"),
        permission_decision=PermissionDecision.allow())
    assert old.status == "expired"
    assert result.outcome == ApprovalOutcome.NEEDS_HUMAN
    assert result.request is new
    assert mint.await_args.kwargs["origin"].context["approval_requirements"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "existing", "expected"), [
    (PolicyDecision(False, True, "review", "ask", {"outcome": "ask", "rule_id": "ask"}), None, "undecided"),
    (PolicyDecision(True, reason="mode.automatic"), None, None),
    (PolicyDecision(False, True, "new review", "new", {"outcome": "ask", "rule_id": "new", "source": "rule"}),
     "granted-old-requirements", "undecided"),
    (PolicyDecision(True, reason="mode.automatic"), "granted", "undecided"),
    (PolicyDecision(False, reason="root deny"), None, "refused"),
], ids=["ask", "allow", "changed-grant", "one-time-grant", "deny"])
async def test_gate_preview_decides_without_writing(monkeypatch, policy, existing, expected):
    # A batch schedule previews each call's gate before running any of them:
    # the same decision, but no request is minted, expired or claimed.
    from packages.core.ai.runtime import approval_service, envelope
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.services import runtime_authorization
    from packages.core import database

    arguments = {"path": "plan.md", "content": "x"}
    action = classify_runtime_tool("write_file", arguments).action
    request = None if existing is None else SimpleNamespace(
        status="granted", action_key=action.action_key, resource_id=action.resource_id,
        context={"args_hash": approval_service.approval_args_hash(arguments, tool_name="write_file"),
                 "approval_requirements": [] if existing == "granted-old-requirements" else ["x"]},
        reason="review", matched_rule="old", hitl_type="authorize",
    )

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()  # no commit/flush/add: any write raises

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", AsyncMock(return_value=PermissionDecision.allow()))
    monkeypatch.setattr(envelope, "runtime_effective_tool_scope", lambda **kwargs: SimpleNamespace(searchable_tool_names=lambda: {"write_file"}))
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=policy))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=request))
    mint = AsyncMock()
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    monkeypatch.setattr(approvals, "_operation_requirements", lambda *args, **kwargs: ["x"])

    result = await approval_service.guard_runtime_tool_action(
        name="write_file", arguments=dict(arguments), entity_id="e", user_id="u",
        workspace_id=None, conversation_id="conv",
        classification=classify_runtime_tool("write_file", arguments),
        operation_id="runtime:run:call:one", preview=True,
    )

    if expected == "undecided":
        assert result == approval_service.RUNTIME_GATE_PREVIEW_UNDECIDED
    elif expected == "refused":
        assert "blocked_by_governance" in result
    else:
        assert result is None
    mint.assert_not_awaited()
    if request is not None:
        assert request.status == "granted"


@pytest.mark.asyncio
async def test_task_constraints_keep_every_ask_and_later_deny_wins():
    def db_for(rules):
        task = SimpleNamespace(details={"runtime_context": {"rules": rules}})
        return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task)))

    asks = [{"rule_key": key, "rule_type": "approval_required",
             "approval_selectors": [{"kind": "category", "category": "communication"}]}
            for key in ("legal-review", "content-review")]
    decision = await service._check_task_runtime_rules(db_for(asks), task_id="task",
        action_key="external_message.send", risk_level="low")
    assert {item["rule_id"] for item in decision.explanation["target_decisions"]} == {"legal-review", "content-review"}
    denied = await service._check_task_runtime_rules(db_for([*asks, {
        "rule_key": "blocked", "rule_type": "deny",
        "approval_selectors": [{"kind": "action", "action_key": "external_message.send"}],
    }]), task_id="task", action_key="external_message.send", risk_level="low")
    assert denied.allowed is False and denied.pause_for_hitl is False


@pytest.mark.asyncio
async def test_task_ask_adds_to_current_account_requirement(monkeypatch):
    root_rule = _rule("ask", {"kind": "action", "action_key": "external_message.send"}, id="account-review")
    monkeypatch.setattr("packages.core.governance.approval_policy_service.load_policy_chain", AsyncMock(return_value=_chain(rules=[root_rule])))
    task_explanation = {"outcome": "ask", "rule_id": "task-review", "source": "constraint",
        "scope": {"kind": "task", "id": "task"}, "reason_code": "task.rule"}
    monkeypatch.setattr(service, "_check_task_runtime_rules", AsyncMock(return_value=PolicyDecision(False, True,
        "task review", "task-review", task_explanation)))
    decision = await service.check_step_policy(None, entity_id="entity", workspace_id=None,
        kind="tool", action_key="external_message.send", risk_level="low", task_id="task")
    assert {item["rule_id"] for item in decision.explanation["target_decisions"]} == {"account-review", "task-review"}
    assert len(approvals._consent_requirements(decision)) == 2


@pytest.mark.asyncio
async def test_exact_origin_preauthorization_does_not_skip_explicit_policy_rule(monkeypatch):
    explanation = {"outcome": "ask", "source": "rule", "rule_id": "explicit-review"}
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(False, True,
        "explicit review", "explicit-review", explanation)))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=None))
    mint = AsyncMock(return_value=SimpleNamespace(reason="review", matched_rule="explicit-review"))
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    result = await approvals.resolve_approval(None,
        subject=approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send"),
        origin=approvals.ApprovalOrigin(kind="tool_call", operation_id="runtime:run:call:one"),
        permission_decision=PermissionDecision.allow(), policy_hitl_preauthorized=True)
    assert result.outcome == ApprovalOutcome.NEEDS_HUMAN
    mint.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_change", ["type", "declared", "explicit", "content"])
async def test_new_human_review_cannot_reuse_ordinary_or_older_review_grant(monkeypatch, review_change):
    decision = PolicyDecision(True, explanation={"outcome": "allow", "source": "mode"})
    subject = approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send")
    old_payload = {"diff": {"before": 1, "after": 2}}
    old_type = "review" if review_change == "content" else "authorize"
    old_requirements = approvals._operation_requirements(decision, subject,
        hitl_type=old_type, payload=old_payload if review_change == "content" else None, require_explicit_approval=False)
    old = SimpleNamespace(status="granted", action_key="external_message.send", resource_id=None,
        context={"args_hash": "same", "approval_requirements": old_requirements}, reason="old", matched_rule="old")
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=decision))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=old))
    mint = AsyncMock(return_value=SimpleNamespace(reason="new review", matched_rule="review"))
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    if review_change == "declared":
        subject = replace(subject, requires_approval=True)
    result = await approvals.resolve_approval(SimpleNamespace(flush=AsyncMock()), subject=subject,
        origin=approvals.ApprovalOrigin(kind="tool_call", operation_id="runtime:run:call:one", args_hash="same"),
        permission_decision=PermissionDecision.allow(),
        hitl_type="review" if review_change in {"type", "content"} else "authorize",
        payload={"diff": {"before": 1, "after": 3}} if review_change == "content" else None,
        require_explicit_approval=review_change == "explicit")
    assert old.status == "expired"
    assert result.outcome == ApprovalOutcome.NEEDS_HUMAN
    mint.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_request_records_transaction_identity_and_current_requirements(monkeypatch):
    from packages.core.ai.runtime import approval_service as runtime
    request = {"provider": "chrome", "provider_approval_id": "provider-transaction",
        "confirmation_tool": "mcp__chrome__confirm_action", "confirmation_arguments": {},
        "retry_tool": "mcp__chrome__click", "retry_arguments": {"selector": "send"}, "action_key": "chrome.click"}
    monkeypatch.setattr(runtime, "freeze_provider_approval_request", lambda value: value)
    monkeypatch.setattr(runtime, "load_runtime_approval_conversation", AsyncMock(return_value=SimpleNamespace(workspace_id="ws")))
    monkeypatch.setattr(approvals, "find_requests_by_dedup", AsyncMock(return_value=[]))
    explanation = {"outcome": "ask", "source": "mode", "scope": {"kind": "workspace", "id": "ws"}}
    monkeypatch.setattr(service, "check_step_policy", AsyncMock(return_value=PolicyDecision(False, True, explanation=explanation)))
    async def mint(db, **kwargs):
        origin, subject = kwargs["origin"], kwargs["subject"]
        assert origin.operation_id == "provider:chrome:provider-transaction"
        assert origin.context["approval_requirements"]
        assert origin.args_hash
        return SimpleNamespace(id="request", origin_conversation_id="conversation", origin_task_id=None,
            context=origin.context, action_key=subject.action_key, capability_id=subject.capability_id)
    monkeypatch.setattr(approvals, "mint_approval_request", AsyncMock(side_effect=mint))
    result = await runtime.register_provider_runtime_approval(None, conversation_id="conversation",
        entity_id="entity", user_id="user", request=request)
    assert result["hitl"]["id"] == "request"


@pytest.mark.asyncio
async def test_provider_preflight_expires_grant_on_new_requirement_before_claim(monkeypatch):
    from packages.core.ai.runtime import approval_service as runtime
    continuation = {"provider": "chrome", "provider_approval_id": "tx",
        "confirmation_tool": "confirm", "retry_tool": "retry"}
    req = SimpleNamespace(id="request", status="granted", workspace_id="ws", action_key="chrome.click",
        resource_id=None, resource_kind=None, capability_id="chrome.action", risk_level="high", origin_task_id=None,
        context={"kind": "provider", **continuation, "continuation": continuation,
                 "requested_by": "user", "approval_requirements": []})
    monkeypatch.setattr(runtime, "_provider_runtime_execution_metadata", lambda value: ("request", "confirm", "retry"))
    monkeypatch.setattr(runtime, "_load_runtime_request", AsyncMock(return_value=req))
    monkeypatch.setattr(runtime, "_foreground_cannot_claim_worker_approval", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime, "freeze_provider_approval_request", lambda value: value)
    explanation = {"outcome": "ask", "source": "rule", "rule_id": "new-review"}
    monkeypatch.setattr(service, "check_step_policy", AsyncMock(return_value=PolicyDecision(False, True, explanation=explanation)))
    expire = AsyncMock()
    monkeypatch.setattr(runtime, "_expire_provider_runtime_approval", expire)
    result = await runtime.prepare_provider_runtime_approval_execution(None, runtime_metadata={}, entity_id="entity",
        conversation_id="conversation", tool_name="retry", arguments={})
    assert result.changed is True and result.result
    expire.assert_awaited_once_with(None, req, "approval_requirements_changed")


@pytest.mark.asyncio
async def test_manual_step_retry_has_new_intent_but_automatic_retry_cannot_replay(monkeypatch):
    from packages.core.dispatcher.step_state import renew_step_approval_intent, step_approval_operation_id
    step = SimpleNamespace(id="step", plan_id="plan", approval_operation_revision=0,
        params={}, step_status="failed", attempt_count=1)
    original = step_approval_operation_id(step)
    assert original == "step:plan:step"
    consumed = SimpleNamespace(status="consumed", action_key="external_message.send", resource_id=None,
        context={"args_hash": "same"}, reason="spent", matched_rule="mode")
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(False, True,
        "review", "mode", {"outcome": "ask", "source": "mode"})))
    async def lookup(db, entity, key):
        return consumed if key == f"operation:{original}" else None
    monkeypatch.setattr(approvals, "_find_operation_request", lookup)
    mint = AsyncMock(return_value=SimpleNamespace(reason="new review", matched_rule="mode"))
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    # An automatic retry does not renew the intent, so it keeps the spent id.
    assert step_approval_operation_id(step) == original
    async def resolve():
        return await approvals.resolve_approval(None,
            subject=approvals.ApprovalSubject(entity_id="entity", workspace_id="ws", action_key="external_message.send"),
            origin=approvals.ApprovalOrigin(kind="step", step_id=step.id, plan_id=step.plan_id,
                operation_id=step_approval_operation_id(step), args_hash="same"),
            permission_decision=PermissionDecision.allow())
    assert (await resolve()).outcome == ApprovalOutcome.DENY
    renew_step_approval_intent(step)
    assert step.approval_operation_revision == 1
    assert step_approval_operation_id(step) == f"{original}:intent:1"
    assert (await resolve()).outcome == ApprovalOutcome.NEEDS_HUMAN
    mint.assert_awaited_once()


@pytest.mark.asyncio
async def test_room_claim_invalidates_new_policy_requirement_before_execution(monkeypatch):
    from packages.core.services import room_tool_approval_service as room_service
    run = SimpleNamespace(id="run", status="running")
    approval = SimpleNamespace(status="approved", approval_requirements=[])
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: approval)), commit=AsyncMock())
    monkeypatch.setattr(room_service, "_locked_run", AsyncMock(return_value=run))
    monkeypatch.setattr(room_service, "_validated_scope", AsyncMock(return_value=(object(), object(), object())))
    explanation = {"outcome": "ask", "rule_id": "new-room-rule", "source": "rule"}
    monkeypatch.setattr(room_service, "room_tool_policy", AsyncMock(return_value=PolicyDecision(False, True, explanation=explanation)))
    with pytest.raises(room_service.RoomToolApprovalError, match="approval_requirements_changed"):
        await room_service.claim_approved_room_tool_call(db, run_id="run", tool_call_id="call",
            tool_name="room_create_file", arguments={"name": "report.txt", "content": "value"})
    assert approval.status == "invalidated"
    db.commit.assert_awaited_once()


def test_personal_file_receipt_reuses_exact_public_gate_without_losing_scope_checks():
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceipt, WorkspaceFileAuthorizationResource, WorkspaceFileResourceMatchKind,
    )
    from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationAccess
    receipt = RuntimeToolAuthorizationReceipt(tool_name="write_file", entity_id="entity", user_id="user",
        workspace_id=None, conversation_id="conversation", task_id=None,
        action_key="workspace.file.modify", capability_id="file.write", access=RuntimeAuthorizationAccess.ACTION,
        arguments_hash="hash", resource_kind="file", file_resources=(WorkspaceFileAuthorizationResource("docs/file.txt", WorkspaceFileResourceMatchKind.EXACT),),
        operation_id="runtime:run:call:one")
    kwargs = dict(capability_id="file.write", action_key="workspace.file.modify", paths=["docs/file.txt"],
        entity_id="entity", user_id="user", workspace_id=None, conversation_id="conversation", task_id=None)
    assert receipt.authorizes_workspace_file_mutation(**kwargs)
    assert not receipt.authorizes_workspace_file_mutation(**{**kwargs, "user_id": "other"})
    assert not receipt.authorizes_workspace_file_mutation(**{**kwargs, "paths": ["docs/other.txt"]})


@pytest.mark.asyncio
async def test_retired_file_always_rejects_before_database_access():
    from packages.core.services.ai_file_permissions import resolve_file_approval_message
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(ValueError, match="versioned"):
        await resolve_file_approval_message(db, conversation_id="conversation", entity_id="entity",
            user_id="user", hitl_id="approval", action="always_approve")
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sandbox_cleanup_alias_keeps_permission_boundary_without_business_approval(monkeypatch):
    from packages.core.ai.runtime import approval_service, envelope
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.services import runtime_authorization
    from packages.core import database
    arguments = {"action": "destroy", "params": {"sandbox_id": "owned"}}
    classification = classify_runtime_tool("sandbox", arguments)
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()
    monkeypatch.setattr(database, "async_session", session)
    permission = AsyncMock(return_value=PermissionDecision.allow())
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", permission)
    monkeypatch.setattr(envelope, "runtime_effective_tool_scope", lambda **kwargs: SimpleNamespace(searchable_tool_names=lambda: {"sandbox"}))
    policy = AsyncMock()
    monkeypatch.setattr(approvals, "_policy_decision", policy)
    assert await approval_service.guard_runtime_tool_action(name="sandbox", arguments=arguments,
        entity_id="entity", user_id="user", workspace_id=None, conversation_id="conversation",
        classification=classification) is None
    permission.assert_awaited_once()
    policy.assert_not_awaited()
