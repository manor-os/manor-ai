"""Step 4 of the unified approval rewrite: card lifecycle + honest badge.

When a plan reaches a terminal state, its open HitlRequests are expired
(step 2) AND the governance chat cards that rendered them are marked resolved
— so no orphaned "no longer attached to a waiting step" card lingers, and the
sidebar pending-action badge (which counts unresolved cards) stays honest.
`open_approval_requests` exposes the store truth per workspace directly.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.governance.approvals import (
    ApprovalOrigin,
    ApprovalSubject,
    count_open_requests_by_workspace,
    resolve_approval,
)
from packages.core.governance.service import resolve_stale_hitl_cards
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Workspace


async def _ws_with_card(db, *, plan_id: str, step_id: str, resolved: bool = False):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    conversation_id = generate_ulid()
    db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Cards WS", operating_model={}))
    db.add(Conversation(
        id=conversation_id, entity_id=entity_id, user_id=generate_ulid(),
        workspace_id=workspace_id, title="ws chat",
    ))
    msg = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="approval needed",
        message_kind="hitl_request",
        pending_action={
            "kind": "governance_approval",
            "step_id": step_id,
            "plan_id": plan_id,
            "step_key": "publish",
        },
    )
    if resolved:
        from datetime import datetime, timezone
        msg.resolved_at = datetime.now(timezone.utc)
    db.add(msg)
    await db.flush()
    return entity_id, workspace_id, msg


@pytest.mark.asyncio
async def test_resolve_stale_cards_closes_matching_unresolved_only(db_session):
    plan_id, step_id = generate_ulid(), generate_ulid()
    _, _, target = await _ws_with_card(db_session, plan_id=plan_id, step_id=step_id)
    # a card for a DIFFERENT plan must survive
    other_plan, other_step = generate_ulid(), generate_ulid()
    _, _, bystander = await _ws_with_card(db_session, plan_id=other_plan, step_id=other_step)
    # an already-resolved card must not be double-stamped
    _, _, done = await _ws_with_card(db_session, plan_id=plan_id, step_id=step_id, resolved=True)
    original_resolution = done.resolution

    closed = await resolve_stale_hitl_cards(db_session, plan_id=plan_id, reason="plan_terminal")

    assert closed == 1
    assert target.resolved_at is not None
    assert target.resolution == {"choice": "expired", "reason": "plan_terminal"}
    assert bystander.resolved_at is None
    assert done.resolution == original_resolution


@pytest.mark.asyncio
async def test_resolve_stale_cards_by_step_ids(db_session):
    plan_id, step_id = generate_ulid(), generate_ulid()
    _, _, target = await _ws_with_card(db_session, plan_id=plan_id, step_id=step_id)

    closed = await resolve_stale_hitl_cards(db_session, step_ids=[step_id])

    assert closed == 1
    assert target.resolved_at is not None


@pytest.mark.asyncio
async def test_finalize_expires_requests_and_closes_cards(db_session):
    """The _finalize integration: terminal plan → open request expired AND its
    chat card resolved, in one cleanup pass."""
    from packages.core.plans.executor import PlanExecutor

    plan_id, step_id = generate_ulid(), generate_ulid()
    entity_id, workspace_id, card = await _ws_with_card(
        db_session, plan_id=plan_id, step_id=step_id,
    )
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        status="running", execution_mode="live", plan_dag={"steps": []},
    )
    db_session.add(plan)
    decision = await resolve_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=entity_id, workspace_id=workspace_id,
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=step_id, plan_id=plan_id),
    )
    assert decision.outcome == "needs_human"
    await db_session.flush()

    await PlanExecutor._finalize(db_session, plan, "failed")

    request = await db_session.get(HitlRequest, decision.request.id)
    assert request.status == "expired"
    assert card.resolved_at is not None
    assert card.resolution == {"choice": "expired", "reason": "plan_terminal"}


@pytest.mark.asyncio
async def test_count_open_requests_by_workspace_groups_pending_only(db_session):
    entity_id = generate_ulid()
    ws_a, ws_b = generate_ulid(), generate_ulid()
    for wid in (ws_a, ws_b):
        db_session.add(Workspace(id=wid, entity_id=entity_id, name=f"WS {wid[:4]}", operating_model={}))
    await db_session.flush()

    async def _mint(wid):
        d = await resolve_approval(
            db_session,
            subject=ApprovalSubject(
                entity_id=entity_id, workspace_id=wid,
                action_key="social_post.publish", capability_id="external.social",
                risk_level="high", kind="action",
            ),
            origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
        )
        return d.request

    await _mint(ws_a)
    await _mint(ws_a)
    spent = await _mint(ws_b)
    spent.status = "consumed"  # terminal rows must not count
    await db_session.flush()

    counts = await count_open_requests_by_workspace(
        db_session, workspace_ids=[ws_a, ws_b],
    )
    assert counts.get(ws_a) == 2
    assert counts.get(ws_b) is None or counts.get(ws_b) == 0
