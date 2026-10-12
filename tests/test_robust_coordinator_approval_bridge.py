"""Exact approval binding for coordinator -> delegated Chat children.

The delegated Chat runtime owns the exact tool-call continuation.  The Task UI
owns the parent lease pause.  These tests prove the second request is linked to
the first by durable server state and that every unified resolution surface
mirrors one verdict without turning a generic confirmation flag into authority.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.execution import (
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY,
    COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_TYPED_VERSION,
    COORDINATOR_VERSION_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    WorkLeaseStatus,
)
from packages.core.ai.runtime.approval_messages import approval_args_hash
from packages.core.dispatcher.service import Dispatcher
from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn
from packages.core.ai.runtime import workspace_delegation_actions
from packages.core.governance.approvals import (
    ApprovalContinuationBindingError,
    consume_approval,
    deny_approval,
    grant_approval,
    resolve_origin_requests,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import Workspace
from packages.core.workers import coordinator_tools


def test_delegation_resume_rebuilds_approved_call_and_exact_suffix():
    retry_continuation = {
        "kind": "retry_with_result_token",
        "tool": "mcp__chrome__click_element",
        "arguments": {"tabId": 7, "ref": "e1"},
    }
    suffix_continuation = {
        "kind": "retry_with_result_token",
        "tool": "mcp__chrome__type_text",
        "arguments": {"tabId": 7, "ref": "e2"},
    }
    calls = workspace_delegation_actions._delegation_resume_forced_calls(
        {
            "tool_name": "mcp__chrome__confirm_action",
            "tool_arguments": {"approvalId": "provider-approval"},
            "tool_call_id": "call-approved",
            "remaining_tool_calls": [{
                "id": "call-suffix",
                "name": "mcp__example__write",
                "arguments": {"value": "after approval"},
            }],
            "disable_followup_tools": True,
            "tool_continuations": {
                "call-approved": retry_continuation,
                "call-suffix": suffix_continuation,
            },
        },
        "runtime-approval-token",
    )

    assert calls == [
        {
            "name": "mcp__chrome__confirm_action",
            "arguments": {
                "approvalId": "provider-approval",
                "approval_token": "runtime-approval-token",
                "__manor_tool_continuation": retry_continuation,
            },
            "disable_followup_tools": True,
        },
        {
            "name": "mcp__example__write",
            "arguments": {
                "value": "after approval",
                "__manor_tool_continuation": suffix_continuation,
            },
            "disable_followup_tools": True,
        },
    ]


async def _fixture(
    db,
    *,
    child_status: str = "waiting_human",
    include_reverse_placeholder: bool = True,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    task_id = generate_ulid()
    parent_id = generate_ulid()
    child_id = generate_ulid()
    lease_id = generate_ulid()
    execution_claim_id = generate_ulid()
    worker_id = generate_ulid()
    nested_id = generate_ulid()
    conversation_id = generate_ulid()
    user_id = generate_ulid()

    db.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Coordinator approvals",
        operating_model={},
    ))
    db.add(Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Coordinator approval task",
        status="in_progress",
        details={},
        delegate_service_keys=["outreach"],
    ))
    db.add(ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        plan_dag={},
        status="running",
    ))
    db.add(ExecutionStep(
        id=parent_id,
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        step_key="coordinate",
        kind="subagent",
        params={COORDINATOR_PARAM_KEY: True},
        step_status="running",
        current_lease_id=lease_id,
        attempt_count=1,
        max_attempts=2,
        risk_level="high",
    ))
    hitl = {
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": nested_id,
        "hitl": {
            "id": nested_id,
            "prompt": "Approve the exact send?",
            "action": "tool.approve",
            "tool": "gmail_send",
        },
        "operation": {"tool": "gmail_send"},
    }
    child_params = {
        "prompt": "Send the approved message",
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
        COORDINATOR_CHILD_ITEM_PARAM_KEY: "notify",
        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: "child-idem-1",
    }
    db.add(ExecutionStep(
        id=child_id,
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        step_key="coordinate__notify",
        kind="subagent",
        service_key="outreach",
        params=child_params,
        step_status=child_status,
        result=hitl if child_status == "waiting_human" else None,
        attempt_count=1,
        max_attempts=2,
        risk_level="high",
    ))
    db.add(Worker(
        id=worker_id,
        entity_id=entity_id,
        kind="internal",
        display_name="Coordinator worker",
        trust_level="trusted",
        status="active",
        capabilities={"supported_kinds": ["subagent"]},
        consecutive_failures=0,
    ))
    db.add(WorkLease(
        id=lease_id,
        step_id=parent_id,
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        worker_id=worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        execution_claim_id=execution_claim_id,
    ))
    nested_context = {
        "tool": "gmail_send",
        "requested_by": user_id,
        "continuation": {
            "version": 1,
            "tool": "gmail_send",
            "arguments": {"to": "ops@example.com", "body": "Ready"},
        },
        "delegation_resume": {
            "version": 1,
            "approval_token": nested_id,
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "agent_subscription_id": generate_ulid(),
            "agent_id": generate_ulid(),
            "service_key": "outreach",
            "prompt": "Send the approved message",
            "max_rounds": 20,
            "delegation_idempotency_key": "child-idem-1",
            "task_id": task_id,
            "step_id": child_id,
            "tool_name": "gmail_send",
        },
    }
    if include_reverse_placeholder:
        nested_context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY] = {
            "version": 1,
            "request_id": None,
            "nested_request_id": nested_id,
            "child_step_id": child_id,
            "parent_step_id": parent_id,
            "parent_lease_id": lease_id,
            "parent_execution_claim_id": execution_claim_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "child_step_key": "coordinate__notify",
            "service_key": "outreach",
            "idempotency_key": "child-idem-1",
        }
    db.add(HitlRequest(
        id=nested_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key="gmail.send",
        capability_id="email.send",
        risk_level="high",
        origin_kind="tool_call",
        origin_conversation_id=conversation_id,
        origin_step_id=child_id,
        origin_task_id=task_id,
        status=ApprovalStatus.PENDING.value,
        dedup_key=f"tool:{conversation_id}:gmail.send:test",
        reason="Approve the exact send?",
        matched_rule="runtime.tool.approval",
        hitl_type="authorize",
        payload={},
        context=nested_context,
    ))
    await db.flush()
    return SimpleNamespace(
        entity_id=entity_id,
        workspace_id=workspace_id,
        plan_id=plan_id,
        task_id=task_id,
        parent_id=parent_id,
        child_id=child_id,
        lease_id=lease_id,
        execution_claim_id=execution_claim_id,
        nested_id=nested_id,
        conversation_id=conversation_id,
        user_id=user_id,
        hitl=hitl,
    )


async def _pause(db, fx):
    await Dispatcher().lease_needs_human(
        db,
        fx.lease_id,
        prompt="Approve the exact send?",
        pending_action={
            "kind": "needs_confirmation",
            "approval_token": fx.nested_id,
            "tool": "gmail_send",
        },
    )
    await db.flush()
    return (await db.execute(
        select(HitlRequest).where(
            HitlRequest.origin_kind == "lease",
            HitlRequest.origin_step_id == fx.parent_id,
        )
    )).scalar_one()


@pytest.mark.asyncio
async def test_dispatcher_binds_outer_pause_to_exact_waiting_child(db_session):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)

    binding = outer.context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY]
    assert binding == {
        "version": 1,
        "request_id": fx.nested_id,
        "child_step_id": fx.child_id,
        "parent_step_id": fx.parent_id,
        "parent_lease_id": fx.lease_id,
        "parent_execution_claim_id": fx.execution_claim_id,
        "plan_id": fx.plan_id,
        "task_id": fx.task_id,
        "child_step_key": "coordinate__notify",
        "service_key": "outreach",
        "idempotency_key": "child-idem-1",
    }
    nested = await db_session.get(HitlRequest, fx.nested_id)
    reverse = nested.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY]
    assert reverse["request_id"] == outer.id
    assert reverse["nested_request_id"] == fx.nested_id


@pytest.mark.asyncio
async def test_nested_request_is_fail_closed_before_outer_card_exists(
    db_session,
    monkeypatch,
):
    fx = await _fixture(
        db_session,
        child_status="running",
        include_reverse_placeholder=False,
    )

    @asynccontextmanager
    async def existing_session():
        yield db_session

    with monkeypatch.context() as persist_patch:
        persist_patch.setattr(
            "packages.core.database.async_session",
            existing_session,
        )
        persisted = await workspace_delegation_actions._persist_delegation_resume(
            entity_id=fx.entity_id,
            workspace_id=fx.workspace_id,
            user_id=fx.user_id,
            conversation_id=fx.conversation_id,
            sub=SimpleNamespace(
                id=generate_ulid(),
                agent_id=generate_ulid(),
                service_key="outreach",
            ),
            delegation_session_id=generate_ulid(),
            prompt="Send the approved message",
            max_rounds=20,
            delegation_idempotency_key="child-idem-1",
            task_id=fx.task_id,
            step_id=fx.child_id,
            parent_step_id=fx.parent_id,
            parent_lease_id=fx.lease_id,
            parent_execution_claim_id=fx.execution_claim_id,
            tool_name="gmail_send",
            tool_arguments={"to": "ops@example.com", "body": "Ready"},
            hitl=fx.hitl,
        )
    assert persisted is True

    nested = await db_session.get(HitlRequest, fx.nested_id)
    placeholder = nested.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY]
    assert placeholder == {
        "version": 1,
        "request_id": None,
        "nested_request_id": fx.nested_id,
        "child_step_id": fx.child_id,
        "parent_step_id": fx.parent_id,
        "parent_lease_id": fx.lease_id,
        "parent_execution_claim_id": fx.execution_claim_id,
        "plan_id": fx.plan_id,
        "task_id": fx.task_id,
        "child_step_key": "coordinate__notify",
        "service_key": "outreach",
        "idempotency_key": "child-idem-1",
    }

    with pytest.raises(ApprovalContinuationBindingError):
        await grant_approval(
            db_session,
            nested,
            by_user_id=fx.user_id,
            via="pre_link_direct",
            authority_prechecked=True,
        )
    with pytest.raises(ApprovalContinuationBindingError):
        await deny_approval(
            db_session,
            nested,
            by_user_id=fx.user_id,
            via="pre_link_direct",
            authority_prechecked=True,
        )

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=fx.conversation_id,
        entity_id=fx.entity_id,
        user_id=fx.user_id,
        hitl_id=fx.nested_id,
        action="approve",
    )
    assert resolution is not None
    assert resolution.runtime_metadata["approval_request_id"] is None
    assert nested.status == ApprovalStatus.PENDING.value

    from apps.api.routers import humans as humans_router

    async def no_workspace_check(*_args, **_kwargs):
        return None

    async def no_commitments(*_args, **_kwargs):
        return []

    monkeypatch.setattr(humans_router, "_require_workspace", no_workspace_check)
    monkeypatch.setattr(humans_router, "list_open_commitments", no_commitments)
    queue = await humans_router.get_human_queue(
        fx.workspace_id,
        user=SimpleNamespace(id=fx.user_id, entity_id=fx.entity_id),
        db=db_session,
    )
    assert queue.approvals == []
    assert queue.information_requests == []

    # The dispatcher can subsequently complete the same server-owned binding.
    child = await db_session.get(ExecutionStep, fx.child_id)
    child.step_status = "waiting_human"
    child.result = fx.hitl
    await db_session.flush()
    outer = await _pause(db_session, fx)
    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert nested.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY]["request_id"] == outer.id


@pytest.mark.asyncio
async def test_task_linked_chat_without_child_identity_is_not_claimed_by_coordinator(
    db_session,
    monkeypatch,
):
    fx = await _fixture(
        db_session,
        child_status="running",
        include_reverse_placeholder=False,
    )

    @asynccontextmanager
    async def existing_session():
        yield db_session

    with monkeypatch.context() as persist_patch:
        persist_patch.setattr(
            "packages.core.database.async_session",
            existing_session,
        )
        persisted = await workspace_delegation_actions._persist_delegation_resume(
            entity_id=fx.entity_id,
            workspace_id=fx.workspace_id,
            user_id=fx.user_id,
            conversation_id=fx.conversation_id,
            sub=SimpleNamespace(
                id=generate_ulid(),
                agent_id=generate_ulid(),
                service_key="outreach",
            ),
            delegation_session_id=generate_ulid(),
            prompt="Send the approved message",
            max_rounds=20,
            delegation_idempotency_key=None,
            task_id=fx.task_id,
            step_id=None,
            tool_name="gmail_send",
            tool_arguments={"to": "ops@example.com", "body": "Ready"},
            hitl=fx.hitl,
        )

    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert persisted is True
    assert COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY not in nested.context


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_kind", ["missing_claim", "expired_lease"])
async def test_pre_link_placeholder_requires_live_execution_claim(
    db_session,
    monkeypatch,
    stale_kind,
):
    fx = await _fixture(
        db_session,
        child_status="running",
        include_reverse_placeholder=False,
    )
    lease = await db_session.get(WorkLease, fx.lease_id)
    if stale_kind == "missing_claim":
        lease.execution_claim_id = None
    else:
        lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    @asynccontextmanager
    async def existing_session():
        yield db_session

    with monkeypatch.context() as persist_patch:
        persist_patch.setattr(
            "packages.core.database.async_session",
            existing_session,
        )
        persisted = await workspace_delegation_actions._persist_delegation_resume(
            entity_id=fx.entity_id,
            workspace_id=fx.workspace_id,
            user_id=fx.user_id,
            conversation_id=fx.conversation_id,
            sub=SimpleNamespace(
                id=generate_ulid(),
                agent_id=generate_ulid(),
                service_key="outreach",
            ),
            delegation_session_id=generate_ulid(),
            prompt="Send the approved message",
            max_rounds=20,
            delegation_idempotency_key="child-idem-1",
            task_id=fx.task_id,
            step_id=fx.child_id,
            parent_step_id=fx.parent_id,
            parent_lease_id=fx.lease_id,
            # Pass the original claim even after mutating the lease so this
            # exercises the exact stale-claim fence instead of merely omitting
            # coordinator identity.
            parent_execution_claim_id=fx.execution_claim_id,
            tool_name="gmail_send",
            tool_arguments={"to": "ops@example.com", "body": "Ready"},
            hitl=fx.hitl,
        )

    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert persisted is False
    assert COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY not in nested.context


@pytest.mark.asyncio
async def test_dispatcher_rejects_tampered_pre_link_placeholder(db_session):
    fx = await _fixture(db_session)
    nested = await db_session.get(HitlRequest, fx.nested_id)
    context = dict(nested.context)
    placeholder = dict(context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY])
    placeholder["child_step_id"] = generate_ulid()
    context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY] = placeholder
    nested.context = context
    await db_session.flush()

    with pytest.raises(RuntimeError, match="not bound to its exact child"):
        await _pause(db_session, fx)


@pytest.mark.asyncio
async def test_parent_cleanup_expires_pre_link_placeholder(db_session):
    fx = await _fixture(db_session, child_status="running")

    closed = await resolve_origin_requests(
        db_session,
        step_id=fx.parent_id,
        reason="parent_failed_before_outer_card",
    )

    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert closed == 1
    assert nested.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_grant_mirrors_to_nested_but_only_outer_is_consumed(
    db_session,
    monkeypatch,
):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)

    async def allowed(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        allowed,
    )
    operator_id = generate_ulid()
    await grant_approval(
        db_session,
        outer,
        by_user_id=operator_id,
        via="test",
        authority_prechecked=True,
    )
    await consume_approval(db_session, outer)
    await db_session.flush()

    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert outer.status == ApprovalStatus.CONSUMED.value
    assert nested.status == ApprovalStatus.GRANTED.value
    assert nested.decided_by_user_id == operator_id
    assert nested.context["delegation_resume"]["user_id"] == fx.user_id
    assert nested.consumed_at is None, "the exact tool guard consumes this grant"
    resume, resume_error = await workspace_delegation_actions._load_delegation_resume(
        db_session,
        entity_id=fx.entity_id,
        workspace_id=fx.workspace_id,
        user_id=fx.user_id,
        conversation_id=fx.conversation_id,
        approval_token=fx.nested_id,
        expected_task_id=fx.task_id,
        expected_step_id=fx.child_id,
        expected_idempotency_key="child-idem-1",
        expected_service_key="outreach",
    )
    assert resume_error is None and resume["step_id"] == fx.child_id
    _, wrong_child_error = await workspace_delegation_actions._load_delegation_resume(
        db_session,
        entity_id=fx.entity_id,
        workspace_id=fx.workspace_id,
        user_id=fx.user_id,
        conversation_id=fx.conversation_id,
        approval_token=fx.nested_id,
        expected_task_id=fx.task_id,
        expected_step_id=generate_ulid(),
        expected_idempotency_key="child-idem-1",
        expected_service_key="outreach",
    )
    assert "another child run" in wrong_child_error

    # A duplicate outer resolution cannot mint or spend a second nested grant.
    decided_at = nested.decided_at
    await grant_approval(
        db_session,
        outer,
        by_user_id=fx.user_id,
        via="duplicate",
        authority_prechecked=True,
    )
    assert nested.decided_at == decided_at


@pytest.mark.asyncio
async def test_deny_mirrors_to_nested_runtime_request(db_session, monkeypatch):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)

    async def allowed(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        allowed,
    )
    await deny_approval(
        db_session,
        outer,
        by_user_id=fx.user_id,
        via="test",
        authority_prechecked=True,
    )
    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert outer.status == ApprovalStatus.DENIED.value
    assert nested.status == ApprovalStatus.DENIED.value


@pytest.mark.asyncio
async def test_tampered_child_binding_fails_closed(db_session, monkeypatch):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)
    context = dict(outer.context)
    binding = dict(context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY])
    binding["child_step_id"] = generate_ulid()
    context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY] = binding
    outer.context = context
    await db_session.flush()

    async def allowed(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        allowed,
    )
    with pytest.raises(ApprovalContinuationBindingError):
        await grant_approval(
            db_session,
            outer,
            by_user_id=fx.user_id,
            via="test",
            authority_prechecked=True,
        )
    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert outer.status == ApprovalStatus.PENDING.value
    assert nested.status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
async def test_nested_request_cannot_be_resolved_outside_parent_card(db_session):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)
    nested = await db_session.get(HitlRequest, fx.nested_id)

    with pytest.raises(ApprovalContinuationBindingError):
        await grant_approval(
            db_session,
            nested,
            by_user_id=fx.user_id,
            via="direct_nested",
            authority_prechecked=True,
        )

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=fx.conversation_id,
        entity_id=fx.entity_id,
        user_id=fx.user_id,
        hitl_id=fx.nested_id,
        action="approve",
    )
    assert resolution is not None
    assert resolution.runtime_metadata["approval_request_id"] == outer.id
    assert nested.status == ApprovalStatus.PENDING.value
    assert outer.status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
async def test_parent_resume_forces_server_derived_child_without_model_token(
    db_session,
    monkeypatch,
):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)

    async def allowed(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        allowed,
    )
    await grant_approval(
        db_session,
        outer,
        by_user_id=generate_ulid(),
        via="test",
        authority_prechecked=True,
    )
    parent = await db_session.get(ExecutionStep, fx.parent_id)
    parent.step_status = "running"
    parent.current_lease_id = fx.lease_id
    lease = await db_session.get(WorkLease, fx.lease_id)
    resume_claim_id = generate_ulid()
    lease.status = WorkLeaseStatus.ACTIVE.value
    lease.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease.execution_claim_id = resume_claim_id
    lease.execution_claimed_at = datetime.now(timezone.utc)
    await db_session.commit()

    forced = await coordinator_tools.coordinator_resume_forced_tool_call({
        "step_id": fx.parent_id,
        "step_key": "coordinate",
        "plan_id": fx.plan_id,
        "entity_id": fx.entity_id,
        "workspace_id": fx.workspace_id,
        "task_id": fx.task_id,
        "lease_id": fx.lease_id,
        "execution_claim_id": resume_claim_id,
    })

    assert forced == {
        "name": "delegate_to_service",
        "arguments": {
            "checklist_item_key": "notify",
            "service_key": "outreach",
            "prompt": "Send the approved message",
        },
    }


@pytest.mark.asyncio
async def test_parent_resume_waits_on_approval_mutex_before_execution_locks(
    db_session,
    monkeypatch,
):
    """Concurrent approval and resume share one canonical lock prefix.

    The approval transaction owns outer HitlRequest -> Task and then continues
    toward Plan/Lease/Steps.  Resume must wait on the outer request before it
    owns any of those execution rows; otherwise the two paths can form the
    historical request/lease ABBA cycle under load.
    """

    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)
    nested = await db_session.get(HitlRequest, fx.nested_id)
    nested.status = ApprovalStatus.GRANTED.value
    nested.decided_at = datetime.now(timezone.utc)
    parent = await db_session.get(ExecutionStep, fx.parent_id)
    parent.step_status = "running"
    parent.current_lease_id = fx.lease_id
    lease = await db_session.get(WorkLease, fx.lease_id)
    resume_claim_id = generate_ulid()
    lease.status = WorkLeaseStatus.ACTIVE.value
    lease.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease.execution_claim_id = resume_claim_id
    lease.execution_claimed_at = datetime.now(timezone.utc)
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(coordinator_tools, "async_session", sessions)
    resume_task: asyncio.Task | None = None
    async with sessions() as approval_db:
        await approval_db.execute(text("SET LOCAL lock_timeout = '750ms'"))
        await approval_db.execute(
            select(HitlRequest)
            .where(HitlRequest.id == outer.id)
            .with_for_update()
        )
        await approval_db.execute(
            select(Task).where(Task.id == fx.task_id).with_for_update()
        )

        resume_task = asyncio.create_task(
            coordinator_tools.coordinator_resume_forced_tool_call({
                "step_id": fx.parent_id,
                "step_key": "coordinate",
                "plan_id": fx.plan_id,
                "entity_id": fx.entity_id,
                "workspace_id": fx.workspace_id,
                "task_id": fx.task_id,
                "lease_id": fx.lease_id,
                "execution_claim_id": resume_claim_id,
            })
        )
        try:
            # The resume transaction is blocked on the already-owned outer
            # request.  In particular it has not taken Lease/Step locks.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(resume_task),
                    timeout=0.1,
                )

            # The approval side can therefore finish the complete canonical
            # sequence without a lock timeout or deadlock.
            await approval_db.execute(
                select(ExecutionPlan)
                .where(ExecutionPlan.id == fx.plan_id)
                .with_for_update()
            )
            await approval_db.execute(
                select(WorkLease)
                .where(WorkLease.id == fx.lease_id)
                .with_for_update()
            )
            await approval_db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.id == fx.parent_id)
                .with_for_update()
            )
            await approval_db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.id == fx.child_id)
                .with_for_update()
            )
            await approval_db.execute(
                select(HitlRequest)
                .where(HitlRequest.id == fx.nested_id)
                .with_for_update()
            )
            await approval_db.commit()

            forced = await asyncio.wait_for(resume_task, timeout=2)
        finally:
            if approval_db.in_transaction():
                await approval_db.rollback()
            if resume_task is not None and not resume_task.done():
                resume_task.cancel()
                await asyncio.gather(resume_task, return_exceptions=True)

    assert forced == {
        "name": "delegate_to_service",
        "arguments": {
            "checklist_item_key": "notify",
            "service_key": "outreach",
            "prompt": "Send the approved message",
        },
    }


@pytest.mark.asyncio
async def test_parent_resume_refreshes_child_after_outer_request_wait(
    db_session,
    monkeypatch,
):
    """A locked child re-read must replace the pre-wait ORM snapshot."""

    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)
    nested = await db_session.get(HitlRequest, fx.nested_id)
    nested.status = ApprovalStatus.GRANTED.value
    nested.decided_at = datetime.now(timezone.utc)
    parent = await db_session.get(ExecutionStep, fx.parent_id)
    parent.step_status = "running"
    parent.current_lease_id = fx.lease_id
    lease = await db_session.get(WorkLease, fx.lease_id)
    resume_claim_id = generate_ulid()
    lease.status = WorkLeaseStatus.ACTIVE.value
    lease.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease.execution_claim_id = resume_claim_id
    lease.execution_claimed_at = datetime.now(timezone.utc)
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(coordinator_tools, "async_session", sessions)
    discovered = asyncio.Event()
    discover = coordinator_tools._coordinator_approval_lock_request_id

    async def observed_discovery(*args, **kwargs):
        request_id = await discover(*args, **kwargs)
        discovered.set()
        return request_id

    monkeypatch.setattr(
        coordinator_tools,
        "_coordinator_approval_lock_request_id",
        observed_discovery,
    )

    resume_task: asyncio.Task | None = None
    async with sessions() as decision_db:
        await decision_db.execute(
            select(HitlRequest)
            .where(HitlRequest.id == outer.id)
            .with_for_update()
        )
        resume_task = asyncio.create_task(
            coordinator_tools.coordinator_resume_forced_tool_call({
                "step_id": fx.parent_id,
                "step_key": "coordinate",
                "plan_id": fx.plan_id,
                "entity_id": fx.entity_id,
                "workspace_id": fx.workspace_id,
                "task_id": fx.task_id,
                "lease_id": fx.lease_id,
                "execution_claim_id": resume_claim_id,
            })
        )
        try:
            await asyncio.wait_for(discovered.wait(), timeout=2)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(resume_task), timeout=0.05)

            child = (await decision_db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.id == fx.child_id)
                .with_for_update()
            )).scalar_one()
            changed_params = dict(child.params or {})
            changed_params[COORDINATOR_PARENT_STEP_PARAM_KEY] = "other-parent"
            child.params = changed_params
            await decision_db.commit()

            forced = await asyncio.wait_for(resume_task, timeout=2)
        finally:
            if decision_db.in_transaction():
                await decision_db.rollback()
            if resume_task is not None and not resume_task.done():
                resume_task.cancel()
                await asyncio.gather(resume_task, return_exceptions=True)

    assert forced is None


@pytest.mark.asyncio
async def test_child_claim_refreshes_nested_request_after_outer_request_wait(
    db_session,
    monkeypatch,
):
    """A denied request committed during the wait cannot resume as pending."""

    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)
    idempotency_key = coordinator_tools._child_idempotency_key(
        fx.plan_id,
        "coordinate",
        "notify",
    )
    child = await db_session.get(ExecutionStep, fx.child_id)
    child_params = dict(child.params or {})
    child_params[COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY] = idempotency_key
    child.params = child_params
    nested = await db_session.get(HitlRequest, fx.nested_id)
    nested_context = dict(nested.context or {})
    resume = dict(nested_context["delegation_resume"])
    resume["delegation_idempotency_key"] = idempotency_key
    nested_context["delegation_resume"] = resume
    reverse = dict(nested_context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY])
    reverse["idempotency_key"] = idempotency_key
    nested_context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY] = reverse
    nested.context = nested_context
    outer_context = dict(outer.context or {})
    binding = dict(outer_context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY])
    binding["idempotency_key"] = idempotency_key
    outer_context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY] = binding
    outer.context = outer_context
    parent = await db_session.get(ExecutionStep, fx.parent_id)
    parent.step_status = "running"
    parent.current_lease_id = fx.lease_id
    lease = await db_session.get(WorkLease, fx.lease_id)
    resume_claim_id = generate_ulid()
    lease.status = WorkLeaseStatus.ACTIVE.value
    lease.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease.execution_claim_id = resume_claim_id
    lease.execution_claimed_at = datetime.now(timezone.utc)
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(coordinator_tools, "async_session", sessions)
    discovered = asyncio.Event()
    discover = coordinator_tools._coordinator_approval_lock_request_id

    async def observed_discovery(*args, **kwargs):
        request_id = await discover(*args, **kwargs)
        discovered.set()
        return request_id

    monkeypatch.setattr(
        coordinator_tools,
        "_coordinator_approval_lock_request_id",
        observed_discovery,
    )

    claim_task: asyncio.Task | None = None
    async with sessions() as decision_db:
        await decision_db.execute(
            select(HitlRequest)
            .where(HitlRequest.id == outer.id)
            .with_for_update()
        )
        claim_task = asyncio.create_task(
            coordinator_tools._insert_or_resume_child_step(
                {
                    "step_id": fx.parent_id,
                    "step_key": "coordinate",
                    "plan_id": fx.plan_id,
                    "entity_id": fx.entity_id,
                    "workspace_id": fx.workspace_id,
                    "task_id": fx.task_id,
                    "lease_id": fx.lease_id,
                    "execution_claim_id": resume_claim_id,
                    "params": {
                        "checklist": [{
                            "delegation_key": "notify",
                            "service_key": "outreach",
                        }],
                    },
                },
                contract={
                    "delegation_key": "notify",
                    "service_key": "outreach",
                },
                item_key="notify",
                service_key="outreach",
                prompt="Send the approved message",
            )
        )
        try:
            await asyncio.wait_for(discovered.wait(), timeout=2)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(claim_task), timeout=0.05)

            locked_nested = (await decision_db.execute(
                select(HitlRequest)
                .where(HitlRequest.id == fx.nested_id)
                .with_for_update()
            )).scalar_one()
            locked_nested.status = ApprovalStatus.DENIED.value
            locked_nested.decided_at = datetime.now(timezone.utc)
            await decision_db.commit()

            dispatch = await asyncio.wait_for(claim_task, timeout=2)
        finally:
            if decision_db.in_transaction():
                await decision_db.rollback()
            if claim_task is not None and not claim_task.done():
                claim_task.cancel()
                await asyncio.gather(claim_task, return_exceptions=True)

    payload = json.loads(dispatch.reused_response)
    assert payload["error"] == "delegated_approval_rejected"
    async with sessions() as verify_db:
        persisted_child = await verify_db.get(ExecutionStep, fx.child_id)
        assert persisted_child.step_status == "failed"


@pytest.mark.asyncio
async def test_parent_origin_cleanup_expires_both_linked_requests(db_session):
    fx = await _fixture(db_session)
    outer = await _pause(db_session, fx)

    closed = await resolve_origin_requests(
        db_session,
        step_id=fx.parent_id,
        reason="parent_terminal",
    )

    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert closed == 2
    assert outer.status == ApprovalStatus.EXPIRED.value
    assert nested.status == ApprovalStatus.EXPIRED.value


async def _typed_runtime_fixture(db):
    """Seed the V2 shape produced by a typed child running AgenticLoop.

    A typed child owns the Runtime request and the active lease itself.  The
    nested request deliberately carries only the ordinary Runtime continuation
    contract: it has no legacy ``delegation_resume`` payload and its
    ``origin_plan_id`` is optional.
    """

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    task_id = generate_ulid()
    root_id = generate_ulid()
    child_id = generate_ulid()
    lease_id = generate_ulid()
    execution_claim_id = generate_ulid()
    worker_id = generate_ulid()
    nested_id = generate_ulid()
    conversation_id = generate_ulid()
    user_id = generate_ulid()
    tool_name = "gmail_send"
    tool_arguments = {"to": "ops@example.com", "body": "Ready"}

    db.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Typed coordinator approvals",
        operating_model={},
    ))
    db.add(Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Typed coordinator approval task",
        status="in_progress",
        details={},
        delegate_service_keys=["outreach"],
    ))
    db.add(ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        plan_dag={},
        status="running",
    ))
    db.add(ExecutionStep(
        id=root_id,
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        step_key="coordinate",
        kind="subagent",
        params={
            COORDINATOR_PARAM_KEY: True,
            COORDINATOR_VERSION_PARAM_KEY: COORDINATOR_TYPED_VERSION,
            COORDINATOR_WAITING_CHILD_PARAM_KEY: {
                "version": 1,
                "child_step_id": child_id,
                "child_step_key": "coordinate__notify",
                "checklist_item_key": "notify",
            },
        },
        step_status="paused",
        attempt_count=1,
        max_attempts=2,
        risk_level="high",
    ))
    child_params = {
        "prompt": "Send the approved message",
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
        COORDINATOR_CHILD_RELEASED_PARAM_KEY: True,
        COORDINATOR_PARENT_STEP_PARAM_KEY: "coordinate",
        COORDINATOR_CHILD_ITEM_PARAM_KEY: "notify",
        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY: "typed-child-idem-1",
    }
    db.add(ExecutionStep(
        id=child_id,
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        step_key="coordinate__notify",
        kind="llm",
        service_key="outreach",
        params=child_params,
        step_status="running",
        current_lease_id=lease_id,
        attempt_count=1,
        max_attempts=2,
        risk_level="high",
    ))
    db.add(Worker(
        id=worker_id,
        entity_id=entity_id,
        kind="internal",
        display_name="Typed coordinator worker",
        trust_level="trusted",
        status="active",
        capabilities={"supported_kinds": ["llm"]},
        consecutive_failures=0,
    ))
    db.add(WorkLease(
        id=lease_id,
        step_id=child_id,
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        worker_id=worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        execution_claim_id=execution_claim_id,
    ))
    db.add(HitlRequest(
        id=nested_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key="gmail.send",
        capability_id="email.send",
        risk_level="high",
        origin_kind="tool_call",
        origin_conversation_id=conversation_id,
        origin_step_id=child_id,
        origin_plan_id=None,
        origin_task_id=task_id,
        status=ApprovalStatus.PENDING.value,
        dedup_key=f"tool:{conversation_id}:gmail.send:typed",
        reason="Approve the exact typed send?",
        matched_rule="runtime.tool.approval",
        hitl_type="authorize",
        payload={},
        context={
            "tool": tool_name,
            "args_hash": approval_args_hash(tool_arguments),
            "requested_by": user_id,
            "continuation": {
                "version": 1,
                "tool": tool_name,
                "arguments": tool_arguments,
            },
        },
    ))
    await db.flush()
    return SimpleNamespace(
        entity_id=entity_id,
        workspace_id=workspace_id,
        plan_id=plan_id,
        task_id=task_id,
        root_id=root_id,
        child_id=child_id,
        lease_id=lease_id,
        execution_claim_id=execution_claim_id,
        nested_id=nested_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
    )


async def _pause_typed_child(db, fx):
    await Dispatcher().lease_needs_human(
        db,
        fx.lease_id,
        prompt="Approve the exact typed send?",
        pending_action={
            "kind": "needs_confirmation",
            "approval_token": fx.nested_id,
            "tool": fx.tool_name,
        },
    )
    await db.flush()
    return (await db.execute(
        select(HitlRequest).where(
            HitlRequest.origin_kind == "lease",
            HitlRequest.origin_step_id == fx.child_id,
        )
    )).scalar_one()


@pytest.mark.asyncio
async def test_typed_runtime_pause_binds_exact_child_without_legacy_resume(
    db_session,
):
    fx = await _typed_runtime_fixture(db_session)
    outer = await _pause_typed_child(db_session, fx)

    assert outer.origin_plan_id == fx.plan_id
    assert outer.origin_task_id == fx.task_id
    binding = outer.context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY]
    assert binding == {
        "version": 1,
        "request_id": fx.nested_id,
        "child_step_id": fx.child_id,
        "parent_step_id": fx.child_id,
        "coordinator_parent_step_key": "coordinate",
        "coordinator_parent_step_id": fx.root_id,
        "parent_lease_id": fx.lease_id,
        "parent_execution_claim_id": fx.execution_claim_id,
        "plan_id": fx.plan_id,
        "task_id": fx.task_id,
        "child_step_key": "coordinate__notify",
        "service_key": "outreach",
        "idempotency_key": "typed-child-idem-1",
        "typed_child": True,
        "nested_tool": fx.tool_name,
        "nested_args_hash": approval_args_hash(fx.tool_arguments),
    }
    nested = await db_session.get(HitlRequest, fx.nested_id)
    assert "delegation_resume" not in nested.context
    reverse = nested.context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY]
    assert reverse == {
        **binding,
        "request_id": outer.id,
        "nested_request_id": fx.nested_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["grant", "deny"])
async def test_typed_outer_verdict_mirrors_exact_runtime_request(
    db_session,
    monkeypatch,
    decision,
):
    fx = await _typed_runtime_fixture(db_session)
    outer = await _pause_typed_child(db_session, fx)

    async def allowed(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        allowed,
    )
    decide = grant_approval if decision == "grant" else deny_approval
    await decide(
        db_session,
        outer,
        by_user_id=fx.user_id,
        via="typed_coordinator_test",
        authority_prechecked=True,
    )

    nested = await db_session.get(HitlRequest, fx.nested_id)
    expected = (
        ApprovalStatus.GRANTED.value
        if decision == "grant"
        else ApprovalStatus.DENIED.value
    )
    assert outer.status == expected
    assert nested.status == expected
    assert nested.decided_by_user_id == fx.user_id
    assert nested.consumed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["claim", "tool", "hash", "root_marker"])
async def test_typed_approval_binding_tampering_fails_closed(
    db_session,
    monkeypatch,
    tamper,
):
    fx = await _typed_runtime_fixture(db_session)
    outer = await _pause_typed_child(db_session, fx)
    nested = await db_session.get(HitlRequest, fx.nested_id)

    if tamper == "claim":
        outer_context = dict(outer.context or {})
        binding = dict(outer_context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY])
        binding["parent_execution_claim_id"] = generate_ulid()
        outer_context[COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY] = binding
        outer.context = outer_context
    elif tamper == "tool":
        nested_context = dict(nested.context or {})
        nested_context["tool"] = "gmail_send_as_someone_else"
        nested.context = nested_context
    elif tamper == "hash":
        nested_context = dict(nested.context or {})
        nested_context["args_hash"] = approval_args_hash({"to": "other@example.com"})
        nested.context = nested_context
    else:
        root = await db_session.get(ExecutionStep, fx.root_id)
        root_params = dict(root.params or {})
        root_params[COORDINATOR_WAITING_CHILD_PARAM_KEY] = {
            "version": 1,
            "child_step_id": generate_ulid(),
            "child_step_key": "coordinate__other",
            "checklist_item_key": "other",
        }
        root.params = root_params
    await db_session.flush()

    async def allowed(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.authorize_hitl_resolution",
        allowed,
    )
    with pytest.raises(ApprovalContinuationBindingError):
        await grant_approval(
            db_session,
            outer,
            by_user_id=fx.user_id,
            via="tampered_typed_coordinator_test",
            authority_prechecked=True,
        )
    assert outer.status == ApprovalStatus.PENDING.value
    assert nested.status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
async def test_typed_nested_runtime_request_rejects_direct_resolution(db_session):
    fx = await _typed_runtime_fixture(db_session)
    outer = await _pause_typed_child(db_session, fx)
    nested = await db_session.get(HitlRequest, fx.nested_id)

    with pytest.raises(ApprovalContinuationBindingError):
        await grant_approval(
            db_session,
            nested,
            by_user_id=fx.user_id,
            via="direct_typed_nested",
            authority_prechecked=True,
        )

    assert outer.status == ApprovalStatus.PENDING.value
    assert nested.status == ApprovalStatus.PENDING.value
