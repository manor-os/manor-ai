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
