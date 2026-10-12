from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash
from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    ExecutionStepStatus,
    WorkLeaseStatus,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.tool_effects import (
    EffectReconciliationChoice,
    EffectReconciliationDecision,
    ToolEffectSettlementOutcome,
)
from packages.core.constants.task import TaskRecoveryChoice
from packages.core.contracts.tool_effects import (
    EffectReconciliationDecisionFactory,
    ToolEffectEvidenceFactory,
)
from packages.core.dispatcher.effect_safety import (
    EFFECT_APPLIED_DECISION,
    EFFECT_NOT_APPLIED_DECISION,
    ambiguous_effect_reconciliation_snapshot,
    effect_action_group_ids,
    validated_new_plan_effect_block_detail,
)
from packages.core.dispatcher.service import _merge_durable_evidence_refs
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import Workspace
from packages.core.plans.wakeup import plan_cycle_outbox_dedupe_key
from packages.core.services.effect_reconciliation import (
    EffectReconciliationError,
    reconcile_external_effects,
)
from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl
from packages.core.services.task_retry_service import (
    mark_plan_continuation_dispatch_failed,
)
from packages.core.workers.coordinator_tools import _merge_evidence_refs
from packages.core.workers.internal import _resume_checkpoint_with_answer
from packages.core.workspace_chat.service import (
    resolve_effect_reconciliation_pending_action,
)


async def _seed_agentic_reconciliation(
    db,
    *,
    checkpoint_arguments=None,
    effect_state="pending",
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    request_id = generate_ulid()
    user_id = generate_ulid()
    call_id = "call-write-1"
    tool_name = "mcp__example__publish"
    arguments = {"title": "Launch", "body": "Ready"}
    checkpoint_args = checkpoint_arguments or arguments
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "Publish it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": '{"title":"Launch","body":"Ready"}',
                    },
                }],
            },
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 3},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={
            "id": call_id,
            "name": tool_name,
            "arguments": checkpoint_args,
        },
    ).to_dict()
    effect = {
        "kind": "tool_effect",
        "effect": effect_state,
        "fields": {
            "tool": tool_name,
            "tool_call_id": call_id,
            "arguments_hash": _tool_arguments_hash(arguments),
            "effect_generation": 0,
            "owner_step_id": step_id,
            "owner_plan_id": plan_id,
            "intent_state": "started" if effect_state == "pending" else "returned",
        },
    }
    db.add_all([
        Entity(id=entity_id, name="Reconciliation entity"),
        User(
            id=user_id,
            entity_id=entity_id,
            email=f"{user_id}@example.test",
            password_hash="x",
            role="owner",
            status="active",
        ),
        UserMembership(
            user_id=user_id,
            entity_id=entity_id,
            role="owner",
            status="active",
            is_primary=True,
        ),
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Reconciliation WS",
            operating_model={},
        ),
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Publish launch",
            status="failed",
            details={},
        ),
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            plan_dag={"steps": []},
            status="paused",
        ),
        ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="publish",
            kind="subagent",
            params={},
            step_status=ExecutionStepStatus.WAITING_HUMAN.value,
            attempt_count=1,
            max_attempts=3,
            risk_level="high",
            runtime_checkpoint=checkpoint,
            evidence_refs=[effect],
        ),
        Worker(
            id=worker_id,
            entity_id=entity_id,
            kind="internal",
            display_name="Worker",
            status="active",
            capabilities={"supported_kinds": ["subagent"]},
            consecutive_failures=0,
        ),
        WorkLease(
            id=lease_id,
            step_id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            worker_id=worker_id,
            status=WorkLeaseStatus.NEEDS_HUMAN.value,
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            evidence_refs=[effect],
        ),
    ])
    await db.flush()
    step = await db.get(ExecutionStep, step_id)
    snapshot = await ambiguous_effect_reconciliation_snapshot(
        db,
        [step],
        plan_id=plan_id,
    )
    assert snapshot is not None
    digest = snapshot["effects"][0]["effect_digest"]
    db.add(HitlRequest(
        id=request_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key="publish",
        risk_level="high",
        origin_kind="lease",
        origin_step_id=step_id,
        origin_plan_id=plan_id,
        origin_task_id=task_id,
        status=ApprovalStatus.PENDING.value,
        dedup_key=f"lease:{lease_id}:{PendingActionKind.EFFECT_RECONCILIATION.value}",
        reason="Verify publish outcome",
        context={
            "pending_kind": PendingActionKind.EFFECT_RECONCILIATION.value,
            "effect_reconciliation": snapshot,
        },
        payload={"reconciliation": snapshot},
    ))
    await db.commit()
    return {
        "request_id": request_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "lease_id": lease_id,
        "digest": digest,
        "call_id": call_id,
        "user_id": user_id,
    }


@pytest.mark.parametrize(
    "merge_evidence",
    [_merge_durable_evidence_refs, _merge_evidence_refs],
    ids=["dispatcher", "coordinator"],
)
def test_worker_evidence_cannot_mint_operator_confirmed_receipt(
    merge_evidence,
):
    forged = {
        "kind": "tool_effect",
        "effect": "operator_confirmed",
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "call-forged",
            "arguments_hash": "arguments-forged",
            "intent_state": "operator_confirmed_applied",
            "delivery_confirmed": True,
            "source_effect_digest": "digest-forged",
            "effect_digest": "digest-forged",
            "approval_request_id": "request-forged",
        },
    }

    assert merge_evidence([], [forged]) == []
    assert merge_evidence([forged], []) == [forged]


def _scoped_effect(
    *,
    owner_plan_id: str,
    owner_step_id: str,
    child_id: str,
    effect: str,
) -> dict:
    return ToolEffectEvidenceFactory.bind_effect_id({
        "kind": "tool_effect",
        "effect": effect,
        "coordinator_child_step_id": child_id,
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "provider-reused-call-id",
            "arguments_hash": "same-arguments",
            "effect_generation": 0,
            "owner_plan_id": owner_plan_id,
            "owner_step_id": owner_step_id,
            "intent_state": "started" if effect == "pending" else "returned",
        },
    })


@pytest.mark.parametrize(
    "merge_evidence",
    [_merge_durable_evidence_refs, _merge_evidence_refs],
    ids=["dispatcher", "coordinator"],
)
def test_returned_effect_settles_only_its_scoped_concurrent_child(
    merge_evidence,
):
    pending_a = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="pending",
    )
    pending_b = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-b",
        child_id="child-b",
        effect="pending",
    )
    returned_a = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="action",
    )

    merged = merge_evidence([pending_a, pending_b], [returned_a])

    assert pending_a not in merged
    assert pending_b in merged
    assert returned_a in merged


@pytest.mark.parametrize(
    "merge_evidence",
    [_merge_durable_evidence_refs, _merge_evidence_refs],
    ids=["dispatcher", "coordinator"],
)
def test_legacy_unscoped_receipt_cannot_settle_colliding_children(
    merge_evidence,
):
    pending_a = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="pending",
    )
    pending_b = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-b",
        child_id="child-b",
        effect="pending",
    )
    legacy_receipt = {
        "kind": "tool_effect",
        "effect": "action",
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "provider-reused-call-id",
            "arguments_hash": "same-arguments",
            "effect_generation": 0,
            "intent_state": "returned",
        },
    }

    merged = merge_evidence([pending_a, pending_b], [legacy_receipt])

    assert pending_a in merged
    assert pending_b in merged


@pytest.mark.parametrize(
    "merge_evidence",
    [_merge_durable_evidence_refs, _merge_evidence_refs],
    ids=["dispatcher", "coordinator"],
)
def test_single_scoped_pending_cannot_be_settled_by_another_child(
    merge_evidence,
):
    pending = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="pending",
    )
    wrong_child_receipt = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-b",
        child_id="child-b",
        effect="action",
    )

    merged = merge_evidence([pending], [wrong_child_receipt])

    assert pending in merged
    assert wrong_child_receipt in merged


@pytest.mark.parametrize(
    "merge_evidence",
    [_merge_durable_evidence_refs, _merge_evidence_refs],
    ids=["dispatcher", "coordinator"],
)
def test_modern_pending_requires_the_receipt_effect_id(merge_evidence):
    pending = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="pending",
    )
    receipt_without_effect_id = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="action",
    )
    receipt_without_effect_id["fields"].pop("effect_id")

    merged = merge_evidence([pending], [receipt_without_effect_id])

    assert pending in merged
    assert receipt_without_effect_id in merged


@pytest.mark.parametrize(
    "invalid_outcome",
    [None, "", "provider_new_state"],
)
@pytest.mark.parametrize(
    "merge_evidence",
    [_merge_durable_evidence_refs, _merge_evidence_refs],
    ids=["dispatcher", "coordinator"],
)
def test_malformed_effect_receipt_never_settles_pending(
    merge_evidence,
    invalid_outcome,
):
    pending = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="pending",
    )
    malformed = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="action",
    )
    if invalid_outcome is None:
        malformed.pop("effect")
    else:
        malformed["effect"] = invalid_outcome

    merged = merge_evidence([pending], [malformed])

    assert pending in merged
    assert malformed in merged


def test_server_settlement_cannot_cross_scoped_effect_identity():
    pending_a = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-a",
        child_id="child-a",
        effect="pending",
    )
    pending_b = _scoped_effect(
        owner_plan_id="plan-1",
        owner_step_id="step-b",
        child_id="child-b",
        effect="pending",
    )
    settlement_a = ToolEffectEvidenceFactory.settlement(
        ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_NOT_APPLIED,
        pending_a["fields"],
        coordinator_child_step_id="child-a",
    )

    merged = ToolEffectEvidenceFactory.append_server_owned(
        [pending_a, pending_b],
        [settlement_a],
    )

    assert pending_a not in merged
    assert pending_b in merged
    assert settlement_a in merged


def test_reconciliation_options_are_intents_not_stale_capabilities():
    snapshot = {
        "effects": [{"effect_digest": "digest-1"}],
        "not_applied_allowed": False,
    }

    assert EffectReconciliationDecisionFactory.card_options(snapshot) == [
        "confirm_effect_applied",
        "confirm_effect_not_applied",
        "cancel",
    ]
    assert (
        EffectReconciliationDecisionFactory.normalize_choice(
            "confirm_effect_not_applied"
        ).value
        == "not_applied"
    )


@pytest.mark.parametrize(
    ("choice", "allow_cancel", "expected"),
    [
        (EffectReconciliationChoice.CONFIRM_APPLIED.value, False, True),
        (EffectReconciliationDecision.APPLIED.value, False, True),
        (EffectReconciliationChoice.RECONCILE.value, False, True),
        (EffectReconciliationChoice.CANCEL.value, False, False),
        (EffectReconciliationChoice.CANCEL.value, True, True),
        ("provider_new_choice", True, False),
    ],
)
def test_reconciliation_choice_classification_is_closed(
    choice,
    allow_cancel,
    expected,
):
    assert (
        EffectReconciliationDecisionFactory.is_reconciliation_choice(
            choice,
            allow_cancel=allow_cancel,
        )
        is expected
    )


@pytest.mark.parametrize(
    "values",
    [
        {"digest-1": "provider_new_decision"},
        {1: EffectReconciliationDecision.APPLIED.value},
        {"": EffectReconciliationDecision.APPLIED.value},
    ],
)
def test_reconciliation_decision_map_rejects_invalid_or_aliased_keys(values):
    assert EffectReconciliationDecisionFactory.normalize_map(values) is None


def test_effect_reconciliation_request_rejects_invalid_decision_map():
    from apps.api.routers.workspace_chat import (
        ResolveActionRequest,
        _effect_reconciliation_decisions,
    )

    with pytest.raises(HTTPException) as exc_info:
        _effect_reconciliation_decisions(
            {
                "payload": {
                    "reconciliation": {
                        "effects": [{"effect_digest": "digest-1"}],
                    },
                },
            },
            ResolveActionRequest(
                choice=EffectReconciliationChoice.RECONCILE.value,
                payload={"decisions": {"digest-1": "provider_new_decision"}},
            ),
        )

    assert exc_info.value.status_code == 400


def test_generic_task_recovery_choices_do_not_alias_effect_decisions():
    assert {member.value for member in TaskRecoveryChoice} == {"retry", "cancel"}


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("applied", "applied"), ("not_applied", "not_applied")],
)
def test_legacy_single_effect_choice_uses_decision_vocabulary(choice, expected):
    from apps.api.routers.workspace_chat import (
        ResolveActionRequest,
        _effect_reconciliation_decisions,
    )

    decisions, results = _effect_reconciliation_decisions(
        {
            "payload": {
                "reconciliation": {
                    "effects": [{"effect_digest": "digest-1"}],
                },
            },
        },
        ResolveActionRequest(choice=choice),
    )

    assert decisions == {"digest-1": expected}
    assert results == {}


@pytest.mark.asyncio
async def test_plan_scoped_reconciliation_supersedes_generic_recovery_surface(
    db_session,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_a_id = generate_ulid()
    plan_b_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Recover exact publish Plan",
        status="waiting_on_customer",
        details={},
    )
    db_session.add_all([
        Entity(id=entity_id, name="Plan binding entity"),
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Plan binding workspace",
            operating_model={},
        ),
        task,
        ExecutionPlan(
            id=plan_a_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            plan_dag={},
            status="failed",
        ),
        ExecutionPlan(
            id=plan_b_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            plan_dag={},
            status="paused",
        ),
    ])
    await db_session.flush()

    old_card = await ensure_task_recovery_hitl(
        db_session,
        task,
        plan_id=plan_a_id,
        prompt="The first Plan stopped.",
        issue="Retry or cancel.",
    )
    assert old_card is not None
    old_request_id = old_card.pending_action["approval_request_id"]

    snapshot = {
        "version": 1,
        "plan_id": plan_b_id,
        "generation": 0,
        "effects": [{"effect_digest": "digest-plan-b"}],
        "not_applied_allowed": True,
    }
    new_card = await ensure_task_recovery_hitl(
        db_session,
        task,
        plan_id=plan_b_id,
        prompt="Verify whether the second Plan published.",
        issue="The provider outcome is unknown.",
        reconciliation=snapshot,
    )
    await db_session.commit()
    assert new_card is not None
    old_card_id = old_card.id
    new_card_id = new_card.id
    new_request_id = new_card.pending_action["approval_request_id"]

    db_session.expire_all()
    old_request = await db_session.get(HitlRequest, old_request_id)
    old_card = await db_session.get(Message, old_card_id)
    new_request = await db_session.get(HitlRequest, new_request_id)
    new_card = await db_session.get(Message, new_card_id)

    assert old_request.status == ApprovalStatus.EXPIRED.value
    assert old_card.resolved_at is not None
    assert old_card.resolution["choice"] == "superseded"
    assert new_request.status == ApprovalStatus.PENDING.value
    assert new_request.origin_task_id == task_id
    assert new_request.origin_plan_id == plan_b_id
    assert new_request.dedup_key.endswith(f":plan:{plan_b_id}")
    assert new_card.resolved_at is None
    assert new_card.pending_action["task_id"] == task_id
    assert new_card.pending_action["plan_id"] == plan_b_id
    assert new_card.pending_action["approval_request_id"] == new_request.id
    assert new_card.pending_action["payload"]["reconciliation"] == snapshot


@pytest.mark.asyncio
async def test_concurrent_plan_recovery_mints_leave_one_exact_actionable_binding(
    db_session,
):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_ids = (generate_ulid(), generate_ulid())
    db_session.add_all([
        Entity(id=entity_id, name="Concurrent Plan binding entity"),
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Concurrent Plan binding workspace",
            operating_model={},
        ),
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Choose one exact recovery Plan",
            status="waiting_on_customer",
            details={},
        ),
        *[
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                plan_dag={},
                status="paused",
            )
            for plan_id in plan_ids
        ],
    ])
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    both_ready = asyncio.Event()
    ready_count = 0

    async def _mint(plan_id: str) -> None:
        nonlocal ready_count
        async with session_factory() as session:
            task = await session.get(Task, task_id)
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
            await both_ready.wait()
            await ensure_task_recovery_hitl(
                session,
                task,
                plan_id=plan_id,
                prompt=f"Verify provider outcome for {plan_id}.",
                issue="The provider outcome is unknown.",
                reconciliation={
                    "version": 1,
                    "plan_id": plan_id,
                    "generation": 0,
                    "effects": [{"effect_digest": f"digest-{plan_id}"}],
                    "not_applied_allowed": True,
                },
            )
            await session.commit()

    await asyncio.gather(*(_mint(plan_id) for plan_id in plan_ids))
    db_session.expire_all()

    requests = list((await db_session.execute(
        select(HitlRequest)
        .where(
            HitlRequest.origin_task_id == task_id,
            HitlRequest.action_key == "task.recover",
        )
        .order_by(HitlRequest.id)
    )).scalars().all())
    cards = list((await db_session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Message.pending_action.isnot(None),
        )
        .order_by(Message.id)
    )).scalars().all())
    live_requests = [
        request
        for request in requests
        if request.status == ApprovalStatus.PENDING.value
    ]
    actionable_cards = [card for card in cards if card.resolved_at is None]

    assert len(requests) == 2
    assert len(live_requests) == 1
    assert len(actionable_cards) == 1
    live_request = live_requests[0]
    actionable_card = actionable_cards[0]
    assert live_request.origin_plan_id in plan_ids
    assert actionable_card.pending_action["approval_request_id"] == live_request.id
    assert actionable_card.pending_action["plan_id"] == live_request.origin_plan_id
    assert (
        actionable_card.pending_action["payload"]["reconciliation"]["plan_id"]
        == live_request.origin_plan_id
    )
    assert all(
        card.resolved_at is not None
        for card in cards
        if card.id != actionable_card.id
    )


@pytest.mark.asyncio
async def test_reconciliation_message_cas_requires_exact_request_and_kind(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    conversation_id = generate_ulid()
    message_id = generate_ulid()
    request_id = generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Message CAS entity"),
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Message CAS workspace",
            operating_model={},
        ),
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Message CAS",
            channel="workspace",
            scope="workspace_main",
        ),
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="Verify provider outcome",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={
                "kind": PendingActionKind.EFFECT_RECONCILIATION.value,
                "approval_request_id": request_id,
            },
        ),
    ])
    await db_session.flush()

    for expected_request_id, expected_kind in (
        (generate_ulid(), PendingActionKind.EFFECT_RECONCILIATION.value),
        (request_id, PendingActionKind.TASK_RECOVERY.value),
    ):
        message, won = await resolve_effect_reconciliation_pending_action(
            db_session,
            message_id=message_id,
            approval_request_id=expected_request_id,
            pending_action_kind=expected_kind,
            user_id=generate_ulid(),
            resolution={"choice": "confirm_effect_not_applied"},
        )
        assert message is not None
        assert won is False
        assert message.resolved_at is None

    message, won = await resolve_effect_reconciliation_pending_action(
        db_session,
        message_id=message_id,
        approval_request_id=request_id,
        pending_action_kind=PendingActionKind.EFFECT_RECONCILIATION.value,
        user_id=generate_ulid(),
        resolution={"choice": "confirm_effect_not_applied"},
    )
    assert message is not None
    assert won is True
    assert message.resolved_at is not None
    rows = list((await db_session.execute(
        select(Message).where(Message.conversation_id == conversation_id)
    )).scalars().all())
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_not_applied_retains_exact_checkpoint_and_advances_generation(db_session):
    fx = await _seed_agentic_reconciliation(db_session)

    result = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()

    step = await db_session.get(ExecutionStep, fx["step_id"])
    request = await db_session.get(HitlRequest, fx["request_id"])
    lease = await db_session.get(WorkLease, fx["lease_id"])
    assert result.reset_step_ids == (fx["step_id"],)
    assert step.step_status == ExecutionStepStatus.PENDING.value
    assert step.params[ACTION_EFFECT_GENERATION_PARAM_KEY] == 1
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == fx["call_id"]
    assert step.runtime_checkpoint["pending_tool_result"] is None
    assert request.status == ApprovalStatus.CONSUMED.value
    assert lease.status == WorkLeaseStatus.CANCELLED.value
    assert _resume_checkpoint_with_answer({
        "step_id": step.id,
        "params": {},
        "action_effect_generation": 1,
        "runtime_checkpoint": step.runtime_checkpoint,
    }) is not None

    replayed = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, fx["step_id"])
    assert replayed.replayed is True
    assert step.params[ACTION_EFFECT_GENERATION_PARAM_KEY] == 1


@pytest.mark.asyncio
async def test_exact_replay_preserves_rearmed_plan_cycle_after_failed_dispatch_hint(
    db_session,
):
    """A failed broker hint re-arms the durable wake without replaying effects."""
    fx = await _seed_agentic_reconciliation(db_session)
    original = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
        user_id=fx["user_id"],
    )
    await db_session.commit()

    outbox_key = plan_cycle_outbox_dedupe_key(original.plan_id)
    original_wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.dedupe_key == outbox_key)
    )).scalar_one()
    original_event_id = original_wake.id
    original_generation = original_wake.payload["wake_generation"]
    assert original_wake.payload["reason"] == "effect_reconciliation_resolved"

    assert await mark_plan_continuation_dispatch_failed(
        db_session,
        plan_id=original.plan_id,
        user_id=fx["user_id"],
        reason="test_queue_failure",
    ) is True
    await db_session.commit()
    db_session.expire_all()

    rearmed_wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.dedupe_key == outbox_key)
    )).scalar_one()
    rearmed_generation = rearmed_wake.payload["wake_generation"]
    assert rearmed_wake.id == original_event_id
    assert rearmed_generation != original_generation
    assert rearmed_wake.payload["reason"] == "legacy_hint_failure:test_queue_failure"
    assert rearmed_wake.delivered_at is None
    assert rearmed_wake.attempt_count == 0

    task = await db_session.get(Task, original.task_id)
    assert task.status == "in_progress"
    assert "_pending_plan_dispatch" not in (task.details or {})

    replayed = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()

    plan = await db_session.get(ExecutionPlan, original.plan_id)
    task = await db_session.get(Task, original.task_id)
    step = await db_session.get(ExecutionStep, fx["step_id"])
    replay_wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.dedupe_key == outbox_key)
    )).scalar_one()
    requests = list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_plan_id == original.plan_id)
    )).scalars())

    assert replayed.replayed is True
    assert replayed.should_dispatch is True
    assert plan.status == "running"
    assert task.status == "in_progress"
    assert "_pending_plan_dispatch" not in (task.details or {})
    assert [request.id for request in requests] == [fx["request_id"]]
    assert replay_wake.id == original_event_id
    assert replay_wake.payload["wake_generation"] == rearmed_generation
    assert step.params[ACTION_EFFECT_GENERATION_PARAM_KEY] == 1


@pytest.mark.asyncio
async def test_reconciliation_repeat_rejects_conflicting_decision(db_session):
    fx = await _seed_agentic_reconciliation(db_session)
    await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
        user_id=fx["user_id"],
    )
    await db_session.commit()

    with pytest.raises(EffectReconciliationError, match="different decision"):
        await reconcile_external_effects(
            db_session,
            approval_request_id=fx["request_id"],
            plan_id=fx["plan_id"],
            decisions={fx["digest"]: EFFECT_APPLIED_DECISION},
            supplied_results={fx["digest"]: {"ok": True}},
            user_id=fx["user_id"],
        )


@pytest.mark.asyncio
async def test_cancel_terminalizes_current_plan_without_dispatching_it(db_session):
    fx = await _seed_agentic_reconciliation(db_session)

    result = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: "cancel"},
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()

    plan = await db_session.get(ExecutionPlan, fx["plan_id"])
    task = await db_session.get(Task, result.task_id)
    step = await db_session.get(ExecutionStep, fx["step_id"])
    request = await db_session.get(HitlRequest, fx["request_id"])
    assert result.should_dispatch is False
    assert plan.status == "cancelled"
    assert task.status == "cancelled"
    assert step.step_status == ExecutionStepStatus.FAILED.value
    assert request.status == ApprovalStatus.DENIED.value

    replayed = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: "cancel"},
        user_id=fx["user_id"],
    )
    assert replayed.replayed is True
    assert replayed.should_dispatch is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_status",
    [WorkLeaseStatus.EXPIRED.value, WorkLeaseStatus.CANCELLED.value],
)
async def test_not_applied_rejects_non_quiescent_revoked_lease(
    db_session,
    unsafe_status,
):
    fx = await _seed_agentic_reconciliation(db_session)
    lease = await db_session.get(WorkLease, fx["lease_id"])
    lease.status = unsafe_status
    await db_session.commit()

    with pytest.raises(EffectReconciliationError, match="not known to be quiescent"):
        await reconcile_external_effects(
            db_session,
            approval_request_id=fx["request_id"],
            plan_id=fx["plan_id"],
            decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
            user_id=fx["user_id"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quiescent_status",
    [WorkLeaseStatus.FAILED.value, WorkLeaseStatus.EXPIRED.value],
)
async def test_not_applied_accepts_terminal_effect_lease_after_claim_is_gone(
    db_session,
    quiescent_status,
):
    fx = await _seed_agentic_reconciliation(db_session)
    lease = await db_session.get(WorkLease, fx["lease_id"])
    lease.status = quiescent_status
    lease.execution_claim_id = None
    lease.error = {
        "type": "lease_expired" if quiescent_status == "expired" else "ToolFailure",
        "message": "External action outcome needs reconciliation",
        "failure": {
            "reason": "Verify the provider outcome",
            "retryable": False,
            "requires_human": True,
            "blockers": [],
        },
    }
    await db_session.commit()

    result = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_NOT_APPLIED_DECISION},
        user_id=fx["user_id"],
    )
    await db_session.commit()

    assert result.reset_step_ids == (fx["step_id"],)
    step = await db_session.get(ExecutionStep, fx["step_id"])
    assert step.params[ACTION_EFFECT_GENERATION_PARAM_KEY] == 1


@pytest.mark.asyncio
async def test_applied_injects_provider_result_and_resumes_agent_loop(db_session):
    fx = await _seed_agentic_reconciliation(db_session)
    provider_result = {"ok": True, "post_id": "post-1"}

    await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_APPLIED_DECISION},
        supplied_results={fx["digest"]: provider_result},
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()

    step = await db_session.get(ExecutionStep, fx["step_id"])
    checkpoint = RuntimeAgentCheckpoint.from_dict(step.runtime_checkpoint)
    assert step.step_status == ExecutionStepStatus.PENDING.value
    assert checkpoint.pending_tool_result == {
        "id": fx["call_id"],
        "content": '{"ok": true, "post_id": "post-1"}',
    }
    assert any(
        ref.get("kind") == "tool_effect"
        and ref.get("effect") == "operator_confirmed"
        for ref in step.evidence_refs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [EFFECT_APPLIED_DECISION, EFFECT_NOT_APPLIED_DECISION])
@pytest.mark.parametrize("old_card", [False, True])
async def test_scoped_and_legacy_copies_of_one_call_need_one_action_decision(
    db_session,
    decision,
    old_card,
):
    fx = await _seed_agentic_reconciliation(db_session, effect_state="action")
    step = await db_session.get(ExecutionStep, fx["step_id"])
    lease = await db_session.get(WorkLease, fx["lease_id"])
    request = await db_session.get(HitlRequest, fx["request_id"])
    scoped = ToolEffectEvidenceFactory.bind_effect_id({
        **step.evidence_refs[0],
        "fields": {
            **step.evidence_refs[0]["fields"],
            "result_sha256": "saved-provider-result-digest",
        },
    })
    legacy = {
        **scoped,
        "fields": {
            key: value for key, value in scoped["fields"].items()
            if key not in {
                "effect_id", "owner_plan_id", "owner_step_id", "effect_generation",
            }
        },
    }
    step.evidence_refs = [scoped, legacy]
    lease.evidence_refs = [scoped, legacy]
    snapshot = await ambiguous_effect_reconciliation_snapshot(
        db_session,
        [step],
        plan_id=fx["plan_id"],
    )
    assert snapshot is not None
    assert len(snapshot["effects"]) == 2
    assert len({item["action_group_id"] for item in snapshot["effects"]}) == 1
    assert all(item["applied_result_required"] is True for item in snapshot["effects"])
    digests = [item["effect_digest"] for item in snapshot["effects"]]
    card_snapshot = (
        {
            **snapshot,
            "effects": [
                {
                    key: value for key, value in item.items()
                    if key not in {"action_group_id", "applied_result_required"}
                }
                for item in snapshot["effects"]
            ],
        }
        if old_card else snapshot
    )
    request.context = {**request.context, "effect_reconciliation": card_snapshot}
    request.payload = {"reconciliation": card_snapshot}
    await db_session.commit()

    result = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={digest: decision for digest in digests},
        supplied_results=(
            {digests[0]: {"ok": True, "post_id": "post-1"}}
            if decision == EFFECT_APPLIED_DECISION else None
        ),
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()

    step = await db_session.get(ExecutionStep, fx["step_id"])
    request = await db_session.get(HitlRequest, fx["request_id"])
    assert result.reset_step_ids == (fx["step_id"],)
    assert request.status == ApprovalStatus.CONSUMED.value
    assert set(request.context["effect_reconciliation_resolution"]["effect_digests"]) == set(digests)
    assert await ambiguous_effect_reconciliation_snapshot(
        db_session,
        [step],
        plan_id=fx["plan_id"],
    ) is None
    if decision == EFFECT_APPLIED_DECISION:
        checkpoint = RuntimeAgentCheckpoint.from_dict(step.runtime_checkpoint)
        assert checkpoint.pending_tool_result["id"] == fx["call_id"]
    else:
        assert step.params[ACTION_EFFECT_GENERATION_PARAM_KEY] == 1


@pytest.mark.parametrize("conflicting_field", [
    "arguments_hash", "result_sha256", "owner_step_id",
])
def test_conflicting_legacy_receipt_is_not_grouped_with_scoped_call(
    conflicting_field,
):
    scoped = ToolEffectEvidenceFactory.bind_effect_id({
        "kind": "tool_effect",
        "effect": "action",
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "call-1",
            "arguments_hash": "args-1",
            "owner_plan_id": "plan-1",
            "owner_step_id": "step-1",
            "effect_generation": 0,
            "result_sha256": "result-1",
        },
    })
    legacy = {
        **scoped,
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "call-1",
            "arguments_hash": "args-1",
            "result_sha256": "result-1",
        },
    }
    legacy["fields"][conflicting_field] = "different-value"
    groups = effect_action_group_ids(
        [scoped, legacy],
        owner_plan_id="plan-1",
        owner_step_id="step-1",
    )
    assert len(set(groups.values())) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_state", ["pending", "action"])
async def test_historical_applied_effect_parks_successor_without_requeue_loop(
    db_session,
    effect_state,
):
    fx = await _seed_agentic_reconciliation(
        db_session,
        effect_state=effect_state,
    )
    old_plan = await db_session.get(ExecutionPlan, fx["plan_id"])
    task_id = old_plan.task_id
    successor_id = generate_ulid()
    successor_step_id = generate_ulid()
    successor_lease_id = generate_ulid()
    source_lease = await db_session.get(WorkLease, fx["lease_id"])
    db_session.add(ExecutionPlan(
        id=successor_id,
        entity_id=old_plan.entity_id,
        workspace_id=old_plan.workspace_id,
        task_id=old_plan.task_id,
        plan_dag={"steps": []},
        status="running",
        created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    ))
    db_session.add(ExecutionStep(
        id=successor_step_id,
        plan_id=successor_id,
        entity_id=old_plan.entity_id,
        workspace_id=old_plan.workspace_id,
        step_key="successor_publish",
        kind="action",
        params={},
        step_status=ExecutionStepStatus.RUNNING.value,
        attempt_count=1,
        max_attempts=3,
        risk_level="high",
        current_lease_id=successor_lease_id,
    ))
    db_session.add(WorkLease(
        id=successor_lease_id,
        step_id=successor_step_id,
        plan_id=successor_id,
        entity_id=old_plan.entity_id,
        workspace_id=old_plan.workspace_id,
        worker_id=source_lease.worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        execution_claim_id="stale-successor-claim",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        evidence_refs=[],
    ))
    await db_session.commit()

    result = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_APPLIED_DECISION},
        supplied_results={fx["digest"]: {"ok": True, "post_id": "post-1"}},
        user_id=fx["user_id"],
    )
    await db_session.commit()
    db_session.expire_all()

    successor = await db_session.get(ExecutionPlan, successor_id)
    successor_step = await db_session.get(ExecutionStep, successor_step_id)
    successor_lease = await db_session.get(WorkLease, successor_lease_id)
    old_step = await db_session.get(ExecutionStep, fx["step_id"])
    task = await db_session.get(Task, task_id)
    assert result.plan_id == successor_id
    assert result.should_dispatch is False
    assert successor.status == "needs_attention"
    assert successor.last_error["type"] == (
        "HistoricalAppliedEffectRequiresContinuation"
    )
    assert task.status == "waiting_on_customer"
    assert successor_lease.status == WorkLeaseStatus.CANCELLED.value
    assert successor_lease.execution_claim_id is None
    assert successor_step.step_status == ExecutionStepStatus.WAITING_HUMAN.value
    assert successor_step.current_lease_id is None

    if effect_state == "pending":
        terminal_receipt = next(
            ref
            for ref in old_step.evidence_refs
            if ref.get("kind") == "tool_effect"
            and ref.get("effect") == "operator_confirmed"
        )
        assert terminal_receipt["fields"]["source_effect_digest"] == fx["digest"]
        assert terminal_receipt["fields"]["effect_digest"] == fx["digest"]
    else:
        assert any(
            ref.get("kind") == "tool_effect"
            and ref.get("effect") == effect_state
            for ref in old_step.evidence_refs
        )
    assert await ambiguous_effect_reconciliation_snapshot(
        db_session,
        [old_step],
        plan_id=fx["plan_id"],
    ) is None
    assert await validated_new_plan_effect_block_detail(
        db_session,
        [old_step],
    ) is not None
    reconciliation_requests = list((await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.origin_plan_id == fx["plan_id"],
            HitlRequest.context["pending_kind"].as_string()
            == PendingActionKind.EFFECT_RECONCILIATION.value,
        )
    )).scalars().all())
    assert [request.id for request in reconciliation_requests] == [fx["request_id"]]
    assert reconciliation_requests[0].status == ApprovalStatus.CONSUMED.value

    replayed = await reconcile_external_effects(
        db_session,
        approval_request_id=fx["request_id"],
        plan_id=fx["plan_id"],
        decisions={fx["digest"]: EFFECT_APPLIED_DECISION},
        supplied_results={fx["digest"]: {"ok": True, "post_id": "post-1"}},
        user_id=fx["user_id"],
    )
    assert replayed.replayed is True
    assert replayed.should_dispatch is False


@pytest.mark.asyncio
async def test_reconciliation_rejects_same_call_identity_with_different_arguments(db_session):
    fx = await _seed_agentic_reconciliation(
        db_session,
        checkpoint_arguments={"title": "Different", "body": "Ready"},
    )

    with pytest.raises(EffectReconciliationError, match="exact pending agent call"):
        await reconcile_external_effects(
            db_session,
            approval_request_id=fx["request_id"],
            plan_id=fx["plan_id"],
            decisions={fx["digest"]: EFFECT_APPLIED_DECISION},
            supplied_results={fx["digest"]: {"ok": True}},
            user_id=fx["user_id"],
        )
