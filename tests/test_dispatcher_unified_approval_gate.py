"""The dispatcher step gate, rewired onto the unified approval core.

``tests/test_unified_approvals.py`` pins ``resolve_approval`` in isolation.
These tests pin the GATE itself — that the dispatcher delegates to that one
decision and maps its outcomes correctly — by driving real checkouts:

  * a pause mints exactly ONE request and links it to the card, so the
    operator surface can grant THAT request;
  * a grant lets the same step through on the next checkout (the #289 / #317
    "I already approved this" loop);
  * re-tripping the gate reuses the one request instead of minting a second
    card (the duplicate-card / inflated-badge fix);
  * a hard block still fails the step and mints no request — approval can
    never be offered for something policy forbids;
  * a terminal plan expires its still-open request (the orphaned "no longer
    attached to a waiting step" card).

Parity with the two gates this replaced is covered by the pre-existing
dispatcher tests in ``tests/test_runtime_tool_policy.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.dispatcher.service import Dispatcher
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.governance.approvals import grant_approval
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.task import Task


async def _scenario(
    db,
    *,
    requires_approval=True,
    risk_level="medium",
    policy=None,
    proposal_authorized=False,
    authorization_overrides=None,
    capability_id="external.social",
    task_runtime_rules=None,
):
    """A workspace step parked in front of the gate, plus a worker to claim it."""
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    # A real workspace-routed subagent step only leases through an active
    # subscription bound to the worker — include the full chain so a granted
    # step can actually dispatch (the loop test's whole point).
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    task_rows = []
    plan_task_id = None
    if proposal_authorized:
        predecessor_task_id = generate_ulid()
        plan_task_id = generate_ulid()
        now = datetime.now(timezone.utc)
        authorization = {
            "version": 1,
            "authorization_id": f"proposal-item:{plan_task_id}",
            "workspace_id": workspace_id,
            "review_id": "review-1",
            "proposal_id": "proposal-1",
            "proposal_item_id": "proposal-item",
            "task_id": plan_task_id,
            "predecessor_task_id": predecessor_task_id,
            "provider": "youtube",
            "action": "publish_video",
            "destination": "studio.youtube.com",
            "visibility": "public",
            "intended_channel": "paired_chrome_signed_in_channel",
            "max_executions": 1,
            "approved_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "approved_by": "operator-1",
            "consumed_at": None,
        }
        authorization.update(authorization_overrides or {})
        task_rows = [
            Task(
                id=predecessor_task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Create verified MP4",
                status="completed",
                actual_output={"files": [{"name": "daily-stickman-video.mp4"}]},
            ),
            Task(
                id=plan_task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Publish verified MP4 publicly to YouTube",
                status="pending",
                details={
                    "proposal_external_authorization": authorization,
                    **(
                        {"runtime_context": {"rules": task_runtime_rules}}
                        if task_runtime_rules else {}
                    ),
                },
            ),
        ]
    db.add_all(task_rows + [
        Workspace(id=workspace_id, entity_id=entity_id,
                  name="Gated workspace", status="active"),
        worker,
        Agent(id=agent_id, entity_id=entity_id, name="Content Agent",
              slug=f"content-{agent_id[:8].lower()}", status="active"),
        AgentSubscription(
            id=subscription_id, entity_id=entity_id, agent_id=agent_id,
            workspace_id=workspace_id, name="Content",
            service_key="content", status="active",
        ),
        SubscriptionWorker(
            subscription_id=subscription_id, worker_id=worker.id,
            priority=100, is_preferred=True,
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
            task_id=plan_task_id,
            status="running", execution_mode="live",
            approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="publish_post",
            kind="subagent",
            service_key="content",
            capability_id=capability_id,
            params={"prompt": "Publish the approved post."},
            depends_on=[], step_status="pending",
            risk_level=risk_level,
            requires_approval=requires_approval,
            attempt_count=0, max_attempts=1,
        ),
    ])
    if policy is not None:
        await update_policy(db, entity_id=entity_id, workspace_id=workspace_id,
                            policy=policy, changed_by="test")
    await db.flush()
    return {
        "entity_id": entity_id, "workspace_id": workspace_id,
        "plan_id": plan_id, "step_id": step_id, "worker": worker,
    }


async def _open_requests(db, entity_id):
    return list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.status == "pending",
        )
    )).scalars().all())


def _repark(step):
    """Simulate the resolver putting the step back in front of the gate."""
    step.step_status = "pending"
    step.error = None
    step.human_input_prompt = None
    step.current_lease_id = None


@pytest.mark.asyncio
async def test_gate_pauses_and_links_one_request_to_the_card(db_session, monkeypatch):
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(db_session)

    leases = await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert leases == []
    assert step.step_status == "waiting_human"

    requests = await _open_requests(db_session, s["entity_id"])
    assert len(requests) == 1
    # The card and the step error both point at THAT request, so the operator
    # surface grants the request the gate is actually waiting on.
    assert step.error["approval_request_id"] == requests[0].id
    assert cards[0]["approval_request_id"] == requests[0].id
    assert requests[0].origin_step_id == s["step_id"]


@pytest.mark.asyncio
async def test_grant_lets_the_step_through_next_checkout__289_317(db_session, monkeypatch):
    """The loop fix: approve once, and the very next checkout dispatches it."""
    monkeypatch.setattr(
        "packages.core.governance.service.post_hitl_card",
        lambda **kw: _noop(),
    )
    s = await _scenario(db_session)

    await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])
    assert step.step_status == "waiting_human"

    request = (await _open_requests(db_session, s["entity_id"]))[0]
    await grant_approval(db_session, request, by_user_id="operator", via="chat_card")
    _repark(step)
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert len(leases) == 1, "granted step must dispatch — this was the approval loop"
    assert step.step_status == "running"
    await db_session.refresh(request)
    assert request.status == "consumed"        # one-time grant was spent
    assert await _open_requests(db_session, s["entity_id"]) == []


@pytest.mark.asyncio
async def test_retrip_reuses_one_request_and_posts_no_duplicate_card(db_session, monkeypatch):
    """Re-tripping the gate must not mint a second request/card — that is what
    inflated the pending-approval badge to 3-for-1."""
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(db_session)

    await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])
    first = (await _open_requests(db_session, s["entity_id"]))[0]

    # Step goes back to pending WITHOUT anyone approving (retry, replan, nudge).
    _repark(step)
    await db_session.flush()
    await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert step.step_status == "waiting_human"
    still_open = await _open_requests(db_session, s["entity_id"])
    assert len(still_open) == 1
    assert still_open[0].id == first.id, "must reuse the same request"


@pytest.mark.asyncio
async def test_hard_block_fails_step_and_mints_no_request(db_session, monkeypatch):
    """Hard blocks stay hard: a forbidden capability is failed outright, never
    offered as an approvable card — even though the step is approval-flagged."""
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(
        db_session,
        policy=WorkspacePolicy(never_allow_capabilities=["external.social"]),
    )

    leases = await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert leases == []
    assert step.step_status == "failed"
    assert step.error["type"] == "GovernancePolicy"
    assert cards == []
    assert await _open_requests(db_session, s["entity_id"]) == []


@pytest.mark.asyncio
async def test_proposal_authorization_skips_duplicate_high_risk_step_prompt(
    db_session,
    monkeypatch,
):
    """The approved Proposal is the one human authorization for its publish task."""
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(
        db_session,
        requires_approval=False,
        risk_level="high",
        proposal_authorized=True,
        capability_id=None,
    )

    leases = await Dispatcher().checkout_steps_for_worker(
        db_session,
        s["worker"],
        max_n=1,
    )
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert len(leases) == 1
    assert step.step_status == "running"
    assert cards == []
    assert await _open_requests(db_session, s["entity_id"]) == []


@pytest.mark.asyncio
async def test_proposal_authorization_skips_matching_runtime_approval_rule(
    db_session,
    monkeypatch,
):
    """Proposal approval also satisfies approval-only policy for that action."""
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(
        db_session,
        requires_approval=False,
        risk_level="high",
        proposal_authorized=True,
        capability_id=None,
        task_runtime_rules=[
            {
                "rule_key": "proposal_publish_requires_approval",
                "rule_type": "approval_required",
                "capability_patterns": ["external.social"],
            }
        ],
    )

    leases = await Dispatcher().checkout_steps_for_worker(
        db_session,
        s["worker"],
        max_n=1,
    )
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert len(leases) == 1
    assert step.step_status == "running"
    assert cards == []
    assert await _open_requests(db_session, s["entity_id"]) == []


@pytest.mark.asyncio
async def test_proposal_authorization_does_not_override_policy_hard_block(
    db_session,
    monkeypatch,
):
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(
        db_session,
        requires_approval=False,
        risk_level="high",
        proposal_authorized=True,
        policy=WorkspacePolicy(never_allow_capabilities=["external.social"]),
        capability_id=None,
    )

    leases = await Dispatcher().checkout_steps_for_worker(
        db_session,
        s["worker"],
        max_n=1,
    )
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert leases == []
    assert step.step_status == "failed"
    assert step.error["type"] == "GovernancePolicy"
    assert cards == []
    assert await _open_requests(db_session, s["entity_id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requires_approval", "authorization_overrides"),
    [
        (True, None),
        (False, {"expires_at": "2020-01-01T00:00:00+00:00"}),
        (False, {"consumed_at": "2026-08-11T00:00:00+00:00"}),
    ],
)
async def test_proposal_scope_must_be_active_and_cannot_waive_explicit_approval(
    db_session,
    monkeypatch,
    requires_approval,
    authorization_overrides,
):
    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _scenario(
        db_session,
        requires_approval=requires_approval,
        risk_level="high",
        proposal_authorized=True,
        authorization_overrides=authorization_overrides,
    )

    leases = await Dispatcher().checkout_steps_for_worker(
        db_session,
        s["worker"],
        max_n=1,
    )
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert leases == []
    assert step.step_status == "waiting_human"
    assert len(cards) == 1
    assert len(await _open_requests(db_session, s["entity_id"])) == 1


@pytest.mark.asyncio
async def test_terminal_plan_expires_the_open_request(db_session, monkeypatch):
    """A plan that ends while a step is still waiting must not leave an
    orphaned card behind."""
    monkeypatch.setattr(
        "packages.core.governance.service.post_hitl_card",
        lambda **kw: _noop(),
    )
    from packages.core.plans.executor import PlanExecutor

    s = await _scenario(db_session)
    await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    assert len(await _open_requests(db_session, s["entity_id"])) == 1

    plan = await db_session.get(ExecutionPlan, s["plan_id"])
    await PlanExecutor._finalize(db_session, plan, "failed")

    assert await _open_requests(db_session, s["entity_id"]) == []


async def _noop():
    return None
