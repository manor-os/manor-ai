"""Workspace settings and one-time approvals share the same policy service.

Exact saved exceptions affect new operations; existing pending requests retain
human review. Automated origins cannot bypass the current Workspace rules.
"""

import json

import pytest

from packages.core import database
from packages.core.ai.runtime.approval_service import (
    guard_runtime_tool_action as guard_workspace_tool_action,
    resolve_runtime_approval_message,
)
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalRule
from packages.core.models.hitl_request import HitlRequest
from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval
from tests.test_runtime_guard_unified_store import _fixture
from packages.core.models.base import generate_ulid


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


_CREATE_ARGS = {
    "name": "hourly X post runner health check",
    "schedule_kind": "cron",
    "cron_expr": "0 * * * *",
    "payload_message": "Check the runner is healthy.",
}


async def _setup_workspace(db_session, *, policy: WorkspacePolicy) -> dict[str, str]:
    ids = await _fixture(db_session)
    await update_policy(db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        policy=policy, changed_by=ids["user_id"], change_summary="test policy")
    await db_session.commit()
    return ids


def _runtime_envelope(ids, *, surface, tool_name: str = "create_scheduled_job"):
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.constants.runtime_principal import RuntimePrincipalKind

    profile = (
        RuntimeProfile.WORKFLOW_STEP
        if surface.value == "workflow_agent_step"
        else RuntimeProfile.WORKSPACE_OPERATOR
    )
    return RuntimeEnvelope(
        surface=surface,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER,
            entity_id=ids["entity_id"],
            actor_user_id=ids["user_id"],
            execution_user_id=ids["user_id"],
            workspace_id=ids["workspace_id"],
        ),
        profile=profile,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        allowed_tool_names=(tool_name,),
    )


async def _guard(ids, arguments, *, runtime_envelope=None, operation="primary"):
    return await guard_workspace_tool_action(
        name="create_scheduled_job",
        arguments=arguments,
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"],
        runtime_envelope=runtime_envelope,
        operation_id=f"test:{ids['conversation_id']}:{operation}",
    )


@pytest.mark.asyncio
async def test_workspace_hitl_without_grant_still_prompts(db_session, monkeypatch):
    """Loop precondition: a workspace that HITL-gates automation returns a card."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(rules=[ApprovalRule(id="review-automation", effect="ask", selector={"kind": "category", "category": "automation"})])),
    )

    blocked = await _guard(ids, dict(_CREATE_ARGS))
    payload = json.loads(blocked or "{}")
    assert payload["__hitl__"] is True
    assert payload["hitl"]["action"] == "workspace.automation.create"
    assert payload["hitl"]["capability_id"] == "automation.manage"


@pytest.mark.asyncio
async def test_workspace_exact_setting_keeps_pending_but_admits_new_operations(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(db_session, policy=WorkspacePolicy(max_risk_level="high",
        approval=ApprovalPolicyOverride(mode="confirm_changes")))
    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)))
    with pytest.raises(ValueError, match="[Aa]lways|approval.settings"):
        await resolve_runtime_approval_message(db_session,
            conversation_id=ids["conversation_id"], entity_id=ids["entity_id"], user_id=ids["user_id"],
            hitl_id=blocked["approval_token"], action="always_approve")
    await set_workspace_test_approval(db_session, workspace_id=ids["workspace_id"],
        entity_id=ids["entity_id"], actor_id=ids["user_id"],
        user_rules=[action_rule("workspace.automation.create")])
    assert (await db_session.get(HitlRequest, blocked["approval_token"])).status == "pending"
    assert json.loads(await _guard(ids, dict(_CREATE_ARGS)))["approval_token"] == blocked["approval_token"]
    assert await _guard(ids, {**_CREATE_ARGS, "name": "daily digest"}, operation="new-intent") is None


@pytest.mark.asyncio
async def test_workspace_approve_once_covers_one_retry_only(db_session, monkeypatch):
    """Plain approve is a one-time grant: token retry passes, third ask re-prompts."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(rules=[ApprovalRule(id="review-automation", effect="ask", selector={"kind": "category", "category": "automation"})])),
    )

    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    token = blocked["approval_token"]
    await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=token,
        action="approve",
    )

    assert await _guard(ids, {**_CREATE_ARGS, "approval_token": token}) is None
    reask = json.loads(await _guard(ids, dict(_CREATE_ARGS), operation="new-intent") or "{}")
    assert reask["__hitl__"] is True
    assert reask["approval_token"] != token


@pytest.mark.asyncio
async def test_workspace_reject_is_per_request_not_standing(db_session, monkeypatch):
    """Rejecting a card denies THAT request; the next attempt asks again with
    a fresh card (rejection is not a standing deny)."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(rules=[ApprovalRule(id="review-automation", effect="ask", selector={"kind": "category", "category": "automation"})])),
    )

    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)) or "{}")
    message = await resolve_runtime_approval_message(
        db_session,
        conversation_id=ids["conversation_id"],
        entity_id=ids["entity_id"],
        user_id=ids["user_id"],
        hitl_id=blocked["approval_token"],
        action="reject",
    )
    assert message and "rejected" in message

    reask = json.loads(await _guard(ids, dict(_CREATE_ARGS), operation="new-intent") or "{}")
    assert reask["__hitl__"] is True
    assert reask["approval_token"] != blocked["approval_token"]


@pytest.mark.asyncio
async def test_workspace_standing_grant_does_not_override_hard_block(db_session, monkeypatch):
    """never_allow beats auto_approve — a standing grant can never override an
    admin's hard governance block (decide()'s deny precedence)."""
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(mode="automatic", rules=[
            ApprovalRule(id="deny-automation", effect="deny", selector={"kind": "category", "category": "automation"}),
            ApprovalRule(id="allow-create", effect="allow", selector={"kind": "action", "action_key": "workspace.automation.create"}),
        ])),
    )

    blocked = await _guard(ids, dict(_CREATE_ARGS))
    payload = json.loads(blocked or "{}")
    assert payload["error"] == "blocked_by_governance"
    assert payload["capability_id"] == "automation.manage"


@pytest.mark.asyncio
async def test_automated_and_direct_origins_share_the_current_policy(
    db_session,
    monkeypatch,
):
    from packages.core.models.workflow import WorkflowRun

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(mode="automatic", rules=[
            ApprovalRule(id="review-automation", effect="ask", selector={"kind": "category", "category": "automation"}),
        ])),
    )
    run = WorkflowRun(
        workflow_id=generate_ulid(),
        entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"],
        trigger_source="manual",
        status="running",
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_snapshot={},
        execution_trace=[],
        started_by=ids["user_id"],
    )
    db_session.add(run)
    await db_session.commit()

    from packages.core.ai.runtime.surfaces import ChatSurface

    allowed = await _guard(
        ids,
        {
            **_CREATE_ARGS,
            "_workflow_run_id_from_context": run.id,
        },
        runtime_envelope=_runtime_envelope(
            ids,
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
        ),
    )
    spoofed_direct_chat = await _guard(
        ids,
        {
            **_CREATE_ARGS,
            "_workflow_run_id_from_context": run.id,
        },
        runtime_envelope=_runtime_envelope(
            ids,
            surface=ChatSurface.WORKSPACE_CHAT,
        ),
    )
    direct_chat = await _guard(ids, dict(_CREATE_ARGS))

    assert json.loads(allowed or "{}")["error"] == "approval_required"
    assert json.loads(spoofed_direct_chat or "{}")["error"] == "approval_required"
    assert json.loads(direct_chat or "{}")["error"] == "approval_required"


@pytest.mark.asyncio
async def test_automatic_mode_never_overrides_hard_block(
    db_session,
    monkeypatch,
):
    from packages.core.models.workflow import WorkflowRun

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(
        db_session,
        policy=WorkspacePolicy(max_risk_level="high", approval=ApprovalPolicyOverride(mode="automatic", rules=[
            ApprovalRule(id="deny-automation", effect="deny", selector={"kind": "category", "category": "automation"}),
        ])),
    )
    run = WorkflowRun(
        workflow_id=generate_ulid(),
        entity_id=ids["entity_id"],
        workspace_id=ids["workspace_id"],
        trigger_source="schedule",
        status="running",
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_snapshot={},
        execution_trace=[],
        started_by=ids["user_id"],
    )
    db_session.add(run)
    await db_session.commit()

    blocked = await _guard(
        ids,
        {
            **_CREATE_ARGS,
            "_workflow_run_id_from_context": run.id,
        },
    )

    payload = json.loads(blocked or "{}")
    assert payload["error"] == "blocked_by_governance"
    assert payload["capability_id"] == "automation.manage"


# ── "Always allow" on the question itself ─────────────────────────────────
#
# Two unrelated scenarios share one contract: an agent's tool call (create an
# automation) and an outbound customer reply. In both, "Always allow" writes
# the same user allow rule, the next action of that kind passes the gate
# without a question, and undoing it in chat makes the gate ask again.


async def _owner(db_session, ids):
    from packages.core.models.user import User

    return await db_session.get(User, ids["user_id"])


async def _decided(db_session, ids, action_key: str, *, status: str, count: int) -> None:
    """Past authorizations a person decided on this action in this Workspace."""
    for _ in range(count):
        db_session.add(HitlRequest(
            entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], action_key=action_key,
            risk_level="medium", origin_kind="tool_call", status=status, hitl_type="authorize",
            dedup_key=f"history:{generate_ulid()}", decided_by_user_id=ids["user_id"],
            decided_via="chat_card", payload={}, context={},
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_always_allow_admits_the_next_tool_call_until_undone_in_chat(db_session, monkeypatch):
    from packages.core.ai.runtime.always_allow_actions import runtime_stop_always_allow
    from packages.core.governance.approval_commands import (
        SaveRuleCommand, project_approval_command, save_rule,
    )

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    ids = await _setup_workspace(db_session, policy=WorkspacePolicy(max_risk_level="high",
        approval=ApprovalPolicyOverride(mode="confirm_changes")))
    blocked = json.loads(await _guard(ids, dict(_CREATE_ARGS)))
    card = blocked["hitl"]
    # "Always allow" is offered in words; first time, "just this once" is recommended.
    assert card["options"] == ["approve", "approve_and_save_rule", "reject"]
    assert card["recommendation"]["option"] == "approve"
    assert card["recommendation"]["reason_code"] == "approve_once"
    assert card["option_details"]["approve_and_save_rule"]["label"] == "Always create automation without asking"
    assert card["batch_key"] == "authorize:workspace.automation.create"

    actor = await _owner(db_session, ids)
    token = blocked["approval_token"]
    projection = await project_approval_command(db_session, request_id=token, actor=actor)
    assert "save_rule" in projection["available_decisions"]
    option = next(item for item in projection["rule_options"] if item["coverage"] == "all_targets_for_action")
    await save_rule(db_session, actor=actor, command=SaveRuleCommand(
        request={"kind": "hitl", "id": token}, choice="save_rule",
        expectedDigest=projection["digest"], idempotencyKey="always-allow-automation",
        expectedPolicyVersion=projection["policy_version"], optionId=option["id"],
    ))
    await db_session.commit()
    # Saving the rule decides nothing: the card's own approve does.
    assert (await db_session.get(HitlRequest, token)).status == "pending"
    assert await _guard(ids, {**_CREATE_ARGS, "name": "daily digest"}, operation="after-always") is None

    undone = json.loads(await runtime_stop_always_allow(
        db_session, entity_id=ids["entity_id"], user_id=ids["user_id"], workspace_id=ids["workspace_id"],
        params={"action_key": "workspace.automation.create"},
    ))
    assert [item["action_key"] for item in undone["removed"]] == ["workspace.automation.create"]
    asked_again = json.loads(await _guard(ids, {**_CREATE_ARGS, "name": "weekly digest"}, operation="after-undo"))
    assert asked_again["__hitl__"] is True


@pytest.mark.asyncio
async def test_repeated_approvals_recommend_always_unless_one_was_declined(db_session, monkeypatch):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    monkeypatch.setenv("APPROVAL_ALWAYS_RECOMMEND_AFTER", "3")
    ids = await _setup_workspace(db_session, policy=WorkspacePolicy(max_risk_level="high",
        approval=ApprovalPolicyOverride(mode="confirm_changes")))
    await _decided(db_session, ids, "workspace.automation.create", status="consumed", count=3)
    card = json.loads(await _guard(ids, dict(_CREATE_ARGS)))["hitl"]
    assert card["options"][0] == "approve_and_save_rule"
    assert card["recommendation"]["reason_code"] == "repeated_always"
    assert card["recommendation"]["reason_params"] == {"count": 3}
    assert card["option_details"]["approve"]["label"] == "Just this once"

    await _decided(db_session, ids, "workspace.automation.create", status="denied", count=1)
    card = json.loads(await _guard(ids, {**_CREATE_ARGS, "name": "other"}, operation="declined-once"))["hitl"]
    assert card["recommendation"]["option"] == "approve"


@pytest.mark.asyncio
async def test_external_reply_always_allow_sends_later_replies_until_undone(db_session, monkeypatch):
    """The second scenario: an outbound customer reply, held on a message card."""
    from packages.core.ai.runtime.always_allow_actions import (
        runtime_list_always_allow, runtime_stop_always_allow,
    )
    from packages.core.governance import approval_policy_service as policies
    from packages.core.governance.always_allow import save_always_allow_rule
    from packages.core.models.task import Message
    from packages.core.services import channel_reply_approvals, notification_workspace_callbacks
    from sqlalchemy import select

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    monkeypatch.setattr(channel_reply_approvals, "async_session", lambda: _SessionContext(db_session))
    sent_buttons = []

    async def capture(**kwargs):
        from packages.core.services.decision_recommendation import channel_actions

        sent_buttons.append(channel_actions(kwargs.get("pending_action")))
        return 1

    monkeypatch.setattr(notification_workspace_callbacks, "notify_workspace_hitl_approvers", capture)
    ids = await _setup_workspace(db_session, policy=WorkspacePolicy(max_risk_level="high",
        approval=ApprovalPolicyOverride(mode="confirm_changes")))

    async def hold():
        return await channel_reply_approvals.maybe_hold_external_reply_for_approval(
            entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], channel_type="telegram",
            channel_config_id="cfg-1", channel_binding_id="binding-1", channel_contact_id="contact-1",
            conversation_id=ids["conversation_id"], agent_id=None, agent_subscription_id=None,
            route_snapshot={}, sender_id="lena", sender_name="Lena", chat_id="chat-1",
            reply_text="Hi Lena, this month's quote is attached.",
        )

    await hold()
    card = (await db_session.execute(select(Message.pending_action).where(
        Message.pending_action["kind"].as_string() == "external_message_approval",
    ))).scalars().one()
    # People read the channel's name, never its id or the rule that held it.
    body = (await db_session.execute(select(Message.content).where(
        Message.pending_action["kind"].as_string() == "external_message_approval",
    ))).scalars().one()
    assert "Channel: Telegram" in body
    assert "`telegram`" not in body and "approval.mode" not in body and "Rule:" not in body
    # A message cannot be unsent: "just this once" leads, "always" is still offered.
    assert card["options"] == ["approve", "approve_and_save_rule", "reject"]
    assert card["recommendation"]["reason_code"] == "irreversible_once"
    assert [button["key"] for button in sent_buttons[0]] == ["approve", "approve_and_save_rule", "reject"]
    assert sent_buttons[0][0]["label"] == "Approve · Recommended"

    actor = await _owner(db_session, ids)
    scope = await policies.resolve_scope(db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"])
    await save_always_allow_rule(db_session, scope=scope, actor=actor, action_key=card["action_key"])
    await db_session.commit()
    gate = await channel_reply_approvals.external_reply_policy(
        db_session, workspace_id=ids["workspace_id"], resource_id="cfg-1")
    assert gate.allowed

    listed = json.loads(await runtime_list_always_allow(
        db_session, entity_id=ids["entity_id"], user_id=ids["user_id"], workspace_id=ids["workspace_id"]))
    rule_id = next(item["rule_id"] for item in listed["always_allowed"] if item["action_key"] == card["action_key"])
    await runtime_stop_always_allow(
        db_session, entity_id=ids["entity_id"], user_id=ids["user_id"], workspace_id=ids["workspace_id"],
        params={"rule_id": rule_id},
    )
    gate = await channel_reply_approvals.external_reply_policy(
        db_session, workspace_id=ids["workspace_id"], resource_id="cfg-1")
    assert not gate.allowed and gate.pause_for_hitl
