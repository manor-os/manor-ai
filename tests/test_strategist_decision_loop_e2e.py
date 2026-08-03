"""W7 — closed-loop E2E for the Strategist decision layer (v2 path).

Full journeys through ledger → review → consolidation → briefing →
proposal → approval → execution → next review:

* Test A "full loop with standing grant" — pre-history facts (task
  transitions + a goal measurement that degrades pace) land in the
  ledger; review 1 consumes them through a frozen ReviewRun window,
  the briefing-driven prompt cites the real evidence ids, the 2-task
  proposal auto-approves via the ``workspace.proposal.task`` standing
  grant into a WorkspaceWorkBatch; executing + completing the batch
  produces the next window, and review 2 sees its own decisions in
  ``previous_decisions`` while an EMPTY proposal still succeeds and
  advances the watermark.
* Test B "needs_human + rejection learning signal" — no grant → one
  pending HitlRequest for the cohort; the operator rejects with
  reason_code=WRONG_DIRECTION and the NEXT review's briefing carries
  the rejection (reason included) back to the Strategist.
* Test C "review failure does not lose the window" — consolidation
  blowing up fails the ReviewRun WITHOUT advancing the watermark; the
  next review re-consumes the exact same events.
* Test D "human_request + meta-suppression loop" — one review proposes a
  goal-linked task + a meta bookkeeping task + a human_request: the meta
  task is suppressed pre-persist (proposal notes say so), the real task
  auto-approves via the standing grant, and the human_request becomes an
  ``executing`` proposal item with an open HumanCommitment surfaced in
  chat (hitl_request) and in the ``/human-queue`` API. The operator
  fulfils it through the respond endpoint (item → succeeded,
  ``human_commitment_fulfilled`` with causation = item id) and the NEXT
  review sees the auto-approval in ``previous_decisions`` while the
  human_participation consolidation shows zero open commitments.
* Test E "experiment lifecycle loop" — a failing every-6h automation in
  the ledger; review 1 proposes ONE bounded experiment (0 tasks) that the
  ``workspace.proposal.experiment`` standing grant auto-starts: overlay on
  the job WITHOUT a revision bump, baseline frozen, ``experiment_started``
  caused by the item. Two cohort runs land under the ``xp:{id}:`` ledger
  correlation, the guardrail tick completes the experiment on max_runs and
  auto-evaluates it deterministically (overlay removed, success_rate
  verdict met), and review 2's learning_evidence report carries the
  numbers-only digest while the timeline API renders review 1's tree with
  the experiment item.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.goals.service import record_measurement
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.review_run import ReviewRun
from packages.core.models.task import Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceWorkBatch,
)
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services import feature_flags as feature_flags_service
from packages.core.services.task_state_machine import apply_task_status_transition
from packages.core.services.workspace_operation_service import (
    check_work_batch_completion,
)
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"


# ── seeding helpers (test_proposal_items.py pattern) ───────────────────

async def _seed_workspace(db, *, name: str, entity_id: str | None = None) -> Workspace:
    """Entity + workspace (with an operating_model strategist template) +
    active Goal + subscribed agent with service_key 'ops'.

    ``entity_id`` may be pinned to a registered user's entity so the
    HTTP endpoints (Test D) can see the same workspace."""
    entity_id = entity_id or generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        status="active",
        settings={},
        operating_model={
            "strategist": {
                "proposal_shape": {"max_tasks_per_cycle": 3},
            },
        },
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Grow followers",
        metric_key="follower_count",
        target_value=1000,
        status="active",
        # 10 days elapsed of a 30-day runway: a flat first measurement
        # computes pace 'at_risk' → goal_pace_changed ledger fact.
        created_at=datetime.now(timezone.utc) - timedelta(days=10),
        deadline=date.today() + timedelta(days=20),
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Ops Agent",
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
        service_key="ops",
        status="active",
    )
    db.add_all([workspace, goal, agent, subscription])
    await db.commit()
    return workspace


async def _active_goal(db, workspace: Workspace) -> Goal:
    return (await db.execute(
        select(Goal).where(
            Goal.workspace_id == workspace.id, Goal.status == "active",
        )
    )).scalar_one()


async def _set_flag(db, enabled: bool) -> None:
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == FLAG_KEY)
    )).scalar_one_or_none()
    if flag is None:
        db.add(FeatureFlag(key=FLAG_KEY, description="test", default_enabled=enabled))
    else:
        flag.default_enabled = enabled
    await db.commit()
    feature_flags_service._bump_cache()


async def _emit(db, workspace: Workspace, *, event_type: str = et.EXECUTION_COMPLETED):
    event = await record_event(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=event_type,
        source_kind="task",
        source_id=f"task_{generate_ulid()}",
        idempotency_key=f"e2e:{generate_ulid()}",
    )
    assert event is not None
    await db.commit()
    await asyncio.sleep(0.002)  # distinct-millisecond ULID ordering
    return event


def _proposal_payload(tasks: list[dict], summary: str) -> dict:
    return {"summary": summary, "tasks": tasks}


def _two_task_payload(evidence_id: str) -> dict:
    return _proposal_payload(
        summary="Draft docs, then publish.",
        tasks=[
            {
                "task_key": "draft_docs",
                "title": "Draft source docs",
                "description": "Write the source docs for this cycle.",
                "owner_service_key": "ops",
                "priority": 3,
                "basis": {
                    "report_refs": ["goal"],
                    "evidence_refs": [evidence_id],
                },
                "deliverables": [{
                    "name": "docs",
                    "kind": "value",
                    "shape": "TextResult",
                    "acceptance": "docs drafted",
                    "usage": "input for publish",
                }],
            },
            {
                "task_key": "publish_docs",
                "title": "Publish the docs",
                "description": "Publish once drafting completes.",
                "owner_service_key": "ops",
                "priority": 3,
                "depends_on_task_keys": ["draft_docs"],
                "basis": {"report_refs": ["execution"], "evidence_refs": []},
                "deliverables": [{
                    "name": "published",
                    "kind": "value",
                    "shape": "TextResult",
                    "acceptance": "docs published",
                    "usage": "operator review",
                }],
            },
        ],
    )


_EMPTY_PAYLOAD = _proposal_payload(
    [], "All domains healthy; nothing worth proposing this cycle.",
)


def _scripted_completion(payloads: list[dict], captured: list[dict]):
    """One queued proposal payload per review cycle; prompts captured."""
    async def fake(system_prompt, user_prompt, **kwargs):
        captured.append({"system": system_prompt, "user": user_prompt})
        return SimpleNamespace(content=json.dumps(payloads.pop(0)))
    return fake


def _quiet_side_surfaces(monkeypatch):
    """Keep reviews off real chat + Celery surfaces."""
    async def _noop_post(*args, **kwargs):
        return None
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop_post)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)


async def _reviews(db, workspace_id: str) -> list[ReviewRun]:
    return list((await db.execute(
        select(ReviewRun)
        .where(ReviewRun.workspace_id == workspace_id)
        .order_by(ReviewRun.id.asc())
    )).scalars().all())


async def _items(db, review_id: str) -> list[ProposalItemRecord]:
    return list((await db.execute(
        select(ProposalItemRecord)
        .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
        .where(ProposalRecord.review_id == review_id)
        .order_by(ProposalItemRecord.item_key.asc())
    )).scalars().all())


async def _tasks(db, workspace: Workspace) -> dict[str, Task]:
    rows = (await db.execute(
        select(Task).where(Task.workspace_id == workspace.id)
    )).scalars().all()
    return {t.details.get("strategist_task_key"): t for t in rows if t.details}


async def _events_of(db, workspace: Workspace, event_type: str) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == event_type,
        ).order_by(WorkspaceEvent.id.asc())
    )).scalars().all())


async def _window_event_ids(db, review: ReviewRun) -> set[str]:
    stmt = select(WorkspaceEvent.id).where(
        WorkspaceEvent.workspace_id == review.workspace_id,
        WorkspaceEvent.id <= review.watermark_end,
    )
    if review.watermark_start is not None:
        stmt = stmt.where(WorkspaceEvent.id > review.watermark_start)
    return {row for row in (await db.execute(stmt)).scalars().all()}


async def _max_event_id(db, workspace: Workspace) -> str:
    from sqlalchemy import func
    return (await db.execute(
        select(func.max(WorkspaceEvent.id)).where(
            WorkspaceEvent.workspace_id == workspace.id
        )
    )).scalar()


# ── Test A: full loop with a standing grant ────────────────────────────

async def test_full_loop_with_standing_grant(db_session, monkeypatch):
    from packages.core.governance.service import add_auto_approve_action

    db = db_session
    workspace = await _seed_workspace(db, name="E2E Standing Grant WS")
    await _set_flag(db, True)
    await add_auto_approve_action(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        action_key="workspace.proposal.task",
        changed_by="operator",
    )
    await db.commit()

    # ── step 2: pre-history through the REAL wiring points ─────────────
    # A prior task runs to completion via the unified state machine…
    pre_task = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="warm-up task from a previous wave",
        status="pending",
        details={},
    )
    db.add(pre_task)
    await db.flush()
    await apply_task_status_transition(pre_task, "in_progress", db=db)
    await asyncio.sleep(0.002)
    await apply_task_status_transition(pre_task, "completed", db=db)
    await db.commit()
    await asyncio.sleep(0.002)

    # …and a goal measurement lands (flat progress on a 1/3-elapsed
    # runway → pace degrades → goal_pace_changed fact).
    goal = await _active_goal(db, workspace)
    await record_measurement(db, goal, value=10)
    await db.commit()
    await asyncio.sleep(0.002)

    started = await _events_of(db, workspace, et.EXECUTION_STARTED)
    completed = await _events_of(db, workspace, et.EXECUTION_COMPLETED)
    measured = await _events_of(db, workspace, et.GOAL_MEASURED)
    paced = await _events_of(db, workspace, et.GOAL_PACE_CHANGED)
    assert [e.source_id for e in started] == [pre_task.id]
    assert [e.source_id for e in completed] == [pre_task.id]
    assert len(measured) == 1 and measured[0].source_id == goal.id
    assert len(paced) == 1
    assert paced[0].payload["new_pace"] in ("behind", "at_risk")
    evidence_id = paced[0].id  # a real ledger fact the proposal will cite

    max_pre_review_id = await _max_event_id(db, workspace)

    # ── step 3: review cycle 1 (2-task proposal citing the evidence) ───
    captured: list[dict] = []
    payloads = [_two_task_payload(evidence_id), dict(_EMPTY_PAYLOAD)]
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _scripted_completion(payloads, captured),
    )
    _quiet_side_surfaces(monkeypatch)

    result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")

    # ── step 4: assert the whole chain ─────────────────────────────────
    assert not result.get("skipped")
    assert result["task_count"] == 2
    assert result["approval_outcome"] == "allow"
    assert result["auto_approved"] is True
    assert set(result["approved_task_ids"]) == set(result["task_ids"])

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 1
    review1 = reviews[0]
    assert review1.status == "succeeded"
    assert result["review_id"] == review1.id
    # The frozen window ends exactly at the last pre-review fact.
    assert review1.watermark_end == max_pre_review_id
    assert isinstance(review1.briefing, dict)
    assert len(review1.briefing["reports"]) == 8

    report_rows = list((await db.execute(
        select(ConsolidationReport).where(ConsolidationReport.review_id == review1.id)
    )).scalars().all())
    assert len(report_rows) == 8
    assert {r.domain for r in report_rows} == set(review1.briefing["reports"])

    # Briefing-driven prompt: domain report sections + coverage gaps +
    # the REAL evidence id from step 2 (via the pace_degraded observation).
    assert len(captured) == 1
    user_prompt = captured[0]["user"]
    assert "## Goal report" in user_prompt
    assert "## Execution report" in user_prompt
    assert "## Coverage gaps" in user_prompt
    assert review1.id in user_prompt
    assert evidence_id in user_prompt

    # Proposal cohort: record resolved, both items approved by the
    # standing grant, rooted at the WorkspaceWorkBatch.
    record = await db.get(ProposalRecord, result["proposal_id"])
    assert record is not None and record.review_id == review1.id
    assert record.status == "resolved"

    tasks = await _tasks(db, workspace)
    assert tasks["draft_docs"].status == "in_progress"
    assert tasks["publish_docs"].status == "pending"  # waits on draft_docs
    batch_id = tasks["draft_docs"].details.get("workspace_work_batch_id")
    assert batch_id
    assert tasks["publish_docs"].details.get("workspace_work_batch_id") == batch_id

    items = await _items(db, review1.id)
    assert [i.item_key for i in items] == ["draft_docs", "publish_docs"]
    assert {i.status for i in items} == {"approved"}
    assert {i.execution_root_id for i in items} == {batch_id}
    by_key = {i.item_key: i for i in items}
    assert by_key["draft_docs"].basis["report_refs"] == ["goal"]
    assert by_key["draft_docs"].basis["evidence_refs"] == [evidence_id]
    assert by_key["publish_docs"].depends_on_item_keys == ["draft_docs"]

    created_events = await _events_of(db, workspace, et.PROPOSAL_CREATED)
    assert len(created_events) == 1
    assert created_events[0].causation_id == review1.id
    assert created_events[0].payload["task_count"] == 2

    approved_events = await _events_of(db, workspace, et.PROPOSAL_ITEM_APPROVED)
    assert len(approved_events) == 2
    assert {e.source_id for e in approved_events} == set(result["task_ids"])
    assert {e.causation_id for e in approved_events} == {review1.id}
    assert {e.root_execution_id for e in approved_events} == {batch_id}
    # Cohort decisions landed AFTER the frozen window → next review's food.
    assert all(e.id > review1.watermark_end for e in approved_events)

    # ── step 5: execute the batch to completion ────────────────────────
    draft, publish = tasks["draft_docs"], tasks["publish_docs"]
    await apply_task_status_transition(draft, "completed", db=db)
    await check_work_batch_completion(db, draft)
    await db.commit()
    await asyncio.sleep(0.002)

    await db.refresh(publish)
    assert publish.status == "in_progress"  # dependency released
    await apply_task_status_transition(publish, "completed", db=db)
    await check_work_batch_completion(db, publish)
    await db.commit()
    await asyncio.sleep(0.002)

    batch = await db.get(WorkspaceWorkBatch, batch_id)
    assert batch.status == "completed"
    assert batch.completed_at is not None

    exec_completed = await _events_of(db, workspace, et.EXECUTION_COMPLETED)
    completed_by_source = {e.source_id: e for e in exec_completed}
    assert {pre_task.id, draft.id, publish.id} <= set(completed_by_source)
    assert completed_by_source[draft.id].root_execution_id == batch_id
    assert completed_by_source[publish.id].root_execution_id == batch_id

    # ── step 6: review cycle 2 closes the loop with an EMPTY proposal ──
    result2 = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
    assert not result2.get("skipped")
    assert result2["task_count"] == 0

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 2
    review2 = reviews[1]
    assert review2.status == "succeeded"
    # Contiguous windows: cycle 2 starts exactly where cycle 1 froze.
    assert review2.watermark_start == review1.watermark_end
    assert review2.watermark_end is not None
    assert review2.watermark_end > review2.watermark_start

    window_ids = await _window_event_ids(db, review2)
    assert completed_by_source[draft.id].id in window_ids
    assert completed_by_source[publish.id].id in window_ids
    assert {e.id for e in approved_events} <= window_ids
    assert created_events[0].id in window_ids

    # The Strategist sees its own approved decisions coming back around.
    decisions = review2.briefing["previous_decisions"]
    assert {d["decision"] for d in decisions} == {"approved"}
    assert {d["task_id"] for d in decisions} >= {draft.id, publish.id}
    assert all(
        d["review_id"] == review1.id
        for d in decisions if d["task_id"] in (draft.id, publish.id)
    )

    # Empty proposal is first-class: review succeeded, no new cohort.
    proposal_records = list((await db.execute(
        select(ProposalRecord).where(ProposalRecord.workspace_id == workspace.id)
    )).scalars().all())
    assert [p.review_id for p in proposal_records] == [review1.id]
    assert "proposal_id" not in result2


# ── Test B: needs_human + rejection learning signal ────────────────────

async def test_needs_human_rejection_feeds_next_briefing(db_session, monkeypatch):
    db = db_session
    workspace = await _seed_workspace(db, name="E2E Needs Human WS")
    await _set_flag(db, True)
    await _emit(db, workspace)  # some window activity

    one_task = _proposal_payload(
        summary="One risky bet.",
        tasks=[{
            "task_key": "risky_bet",
            "title": "Chase a growth hack",
            "description": "Try the growth hack the metrics hinted at.",
            "owner_service_key": "ops",
            "priority": 2,
            "basis": {"report_refs": ["goal"], "evidence_refs": []},
            "deliverables": [{
                "name": "hack_report",
                "kind": "value",
                "shape": "TextResult",
                "acceptance": "experiment written up",
                "usage": "operator review",
            }],
        }],
    )
    captured: list[dict] = []
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _scripted_completion([one_task, dict(_EMPTY_PAYLOAD)], captured),
    )
    _quiet_side_surfaces(monkeypatch)

    result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")

    # No standing grant → cohort waits on ONE pending HitlRequest.
    assert result["approval_outcome"] == "needs_human"
    assert result["auto_approved"] is False
    req = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.dedup_key == f"proposal:{result['proposal_id']}",
        )
    )).scalar_one()
    assert req.status == "pending"
    assert req.action_key == "workspace.proposal.task"
    assert result["approval_request_id"] == req.id

    items = await _items(db, result["review_id"])
    assert len(items) == 1
    assert items[0].status == "proposed"
    assert items[0].approval_request_id == req.id

    # Operator pushes back with a coded reason.
    operator_id = generate_ulid()
    rejection_comment = "Wrong direction — focus on retention, not hacks."
    cancelled = await strategist_service.reject_proposal(
        db,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        reason=rejection_comment,
        reason_code="WRONG_DIRECTION",
        actor_id=operator_id,
    )
    await db.commit()
    assert set(cancelled) == set(result["task_ids"])

    items = await _items(db, result["review_id"])
    assert items[0].status == "rejected"
    assert items[0].decision["reason_code"] == "WRONG_DIRECTION"
    assert items[0].decision["comment"] == rejection_comment
    assert items[0].decision["decided_by"] == operator_id
    assert items[0].execution_root_id is None

    await db.refresh(req)
    assert req.status == "denied"
    record = await db.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"
    task = (await db.execute(
        select(Task).where(Task.id == result["task_ids"][0])
    )).scalar_one()
    assert task.status == "cancelled"

    rejected_events = await _events_of(db, workspace, et.PROPOSAL_ITEM_REJECTED)
    assert len(rejected_events) == 1
    assert rejected_events[0].source_id == task.id
    assert rejected_events[0].causation_id == result["review_id"]
    assert rejected_events[0].payload["rejection_reason"] == rejection_comment
    assert rejected_events[0].actor_id == operator_id

    # Next review: the rejection is a learning signal in the briefing.
    await asyncio.sleep(0.002)
    result2 = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
    assert not result2.get("skipped")
    assert result2["task_count"] == 0

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 2
    review2 = reviews[1]
    assert review2.status == "succeeded"
    decisions = review2.briefing["previous_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "rejected"
    assert decisions[0]["task_id"] == task.id
    assert decisions[0]["review_id"] == result["review_id"]
    assert decisions[0]["reason"] == rejection_comment
    # …and the rendered prompt carries it verbatim to the Strategist.
    assert "Wrong direction" in captured[1]["user"]


# ── Test C: review failure does not lose the window ────────────────────

async def test_review_failure_preserves_window_for_next_review(db_session, monkeypatch):
    db = db_session
    workspace = await _seed_workspace(db, name="E2E Failure Recovery WS")
    await _set_flag(db, True)
    seeded = [
        await _emit(db, workspace),
        await _emit(db, workspace, event_type=et.EXECUTION_STARTED),
        await _emit(db, workspace),
    ]
    seeded_ids = {e.id for e in seeded}

    captured: list[dict] = []
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _scripted_completion([dict(_EMPTY_PAYLOAD)], captured),
    )
    _quiet_side_surfaces(monkeypatch)

    # Consolidation blows up → the whole review fails.
    async def _boom(db_, review):
        raise RuntimeError("consolidation exploded")

    with monkeypatch.context() as broken:
        broken.setattr("packages.core.consolidators.run_all", _boom)
        with pytest.raises(RuntimeError, match="consolidation exploded"):
            await _execute_strategist_review_cycle(db, workspace.id, "scheduled")

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 1
    failed = reviews[0]
    assert failed.status == "failed"
    assert "consolidation exploded" in (failed.error or "")
    # Failed run froze a window but the watermark did NOT advance…
    from packages.core.review import latest_succeeded_review
    assert await latest_succeeded_review(db, workspace.id) is None

    # …so the next (healthy) review re-consumes the exact same events.
    result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
    assert not result.get("skipped")

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 2
    recovered = reviews[1]
    assert recovered.status == "succeeded"
    assert recovered.watermark_start is None  # ledger genesis: nothing consumed yet
    assert recovered.watermark_end is not None
    window_ids = await _window_event_ids(db, recovered)
    assert seeded_ids <= window_ids
    assert len(captured) == 1  # the failed run never reached the LLM


# ── Test D: human_request + meta-suppression loop ──────────────────────

_META_TASK_TITLE = "Record this week's learnings to LEARNINGS.md"
_HR_QUESTION = (
    "Should we double down on short-form video as the main direction next month?"
)


def _mixed_payload(goal_id: str) -> dict:
    """1 real goal-linked task + 1 meta bookkeeping task + 1 human_request."""
    payload = _proposal_payload(
        summary="One growth bet; one thing needs the operator's call.",
        tasks=[
            {
                "task_key": "ship_growth_experiment",
                "title": "Ship a short-form video growth experiment",
                "description": "Produce and publish three short-form videos.",
                "owner_service_key": "ops",
                "priority": 2,
                "estimated_impact": {"goal_id": goal_id, "metric_delta": 50},
                "basis": {"report_refs": ["goal"], "evidence_refs": []},
                "deliverables": [{
                    "name": "videos",
                    "kind": "value",
                    "shape": "TextResult",
                    "acceptance": "three videos published",
                    "usage": "follower growth",
                }],
            },
            {
                "task_key": "record_learnings",
                "title": _META_TASK_TITLE,
                "description": "Capture what we learned this cycle.",
                "owner_service_key": "ops",
                "priority": 4,
                "deliverables": [{
                    "name": "learnings",
                    "kind": "value",
                    "shape": "TextResult",
                    "acceptance": "learnings recorded",
                    "usage": "future cycles",
                }],
            },
        ],
    )
    payload["human_requests"] = [{
        "request_key": "confirm_direction",
        "request_kind": "decision",
        "question": _HR_QUESTION,
        "expected_by_hours": 24,
        "context": "Engagement on long-form has been flat for two cycles.",
    }]
    return payload


async def test_human_request_and_meta_suppression_loop(client, db_session, monkeypatch):
    """Full journey: mixed proposal → suppression + standing-grant
    auto-approval + human_request commitment → operator fulfils via the
    respond endpoint → next review sees the decision and a clean
    human_participation report."""
    from auth_helpers import register_user_and_get_token

    from packages.core.consolidators.human_participation import (
        HumanParticipationConsolidator,
    )
    from packages.core.governance.service import add_auto_approve_action
    from packages.core.models.participant import HumanCommitment
    from packages.core.models.task import Conversation, Message

    db = db_session

    # ── step 1: registered owner + workspace under the SAME entity ─────
    resp = await register_user_and_get_token(client, json={
        "username": "e2e_hr_owner",
        "email": "e2e_hr_owner@test.com",
        "password": "pass123",
        "entity_name": "E2E HR Corp",
    })
    token_data = resp.json()
    owner_headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert me.status_code == 200
    owner_user_id = token_data["user_id"]
    entity_id = me.json()["entity_id"]

    workspace = await _seed_workspace(
        db, name="E2E Human Loop WS", entity_id=entity_id,
    )
    await _set_flag(db, True)
    await add_auto_approve_action(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        action_key="workspace.proposal.task",
        changed_by="operator",
    )
    await db.commit()
    goal = await _active_goal(db, workspace)
    await _emit(db, workspace)  # some window activity

    # ── step 2: review cycle 1 (mixed proposal) ────────────────────────
    captured: list[dict] = []
    payloads = [_mixed_payload(goal.id), dict(_EMPTY_PAYLOAD)]
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _scripted_completion(payloads, captured),
    )
    # Proposal card / Celery stay quiet; _post_human_request_chat runs
    # for REAL against the shared test DB so the chat surface is asserted.
    _quiet_side_surfaces(monkeypatch)

    result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")

    assert not result.get("skipped")
    review1_id = result["review_id"]

    # Meta task suppressed pre-persist: only the real task survived, and
    # both the run result and the ProposalRecord say why.
    assert result["task_count"] == 1
    assert "Suppressed 1 meta/bookkeeping task(s)" in (result["notes"] or "")
    assert _META_TASK_TITLE in (result["notes"] or "")
    tasks = await _tasks(db, workspace)
    assert set(tasks) == {"ship_growth_experiment"}
    record = await db.get(ProposalRecord, result["proposal_id"])
    assert "Suppressed 1 meta/bookkeeping task(s)" in (record.notes or "")
    assert _META_TASK_TITLE in (record.notes or "")

    # Real task auto-approved via the standing grant.
    assert result["approval_outcome"] == "allow"
    assert result["auto_approved"] is True
    real_task = tasks["ship_growth_experiment"]
    assert result["approved_task_ids"] == [real_task.id]
    assert real_task.status == "in_progress"

    # human_request item: executing, no HitlRequest, commitment root.
    assert len(result["human_requests"]) == 1
    items = await _items(db, review1_id)
    by_key = {i.item_key: i for i in items}
    assert set(by_key) == {"hr_confirm_direction", "ship_growth_experiment"}
    hr_item = by_key["hr_confirm_direction"]
    assert hr_item.kind == "human_request"
    assert hr_item.status == "executing"
    assert hr_item.action_key == "workspace.proposal.human_request"
    assert hr_item.approval_request_id is None
    assert hr_item.decision["decision"] == "auto"
    assert hr_item.payload["request_kind"] == "decision"
    assert hr_item.payload["question"] == _HR_QUESTION

    commitment = (await db.execute(
        select(HumanCommitment).where(
            HumanCommitment.workspace_id == workspace.id,
            HumanCommitment.source_kind == "proposal_item",
            HumanCommitment.source_id == hr_item.id,
        )
    )).scalar_one()
    assert hr_item.execution_root_id == commitment.id
    assert commitment.status == "waiting"
    assert commitment.expected_input == _HR_QUESTION
    assert commitment.expected_by is not None
    delta = commitment.expected_by - datetime.now(timezone.utc)
    assert timedelta(hours=23) < delta < timedelta(hours=25)

    # Chat surface: one REAL hitl_request message with the commitment ref
    # and deliberately no pending_action (the queue is the action surface).
    chat_messages = list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.workspace_id == workspace.id,
            Message.message_kind == "hitl_request",
        )
    )).scalars().all())
    assert len(chat_messages) == 1
    assert chat_messages[0].refs == [{"type": "human_commitment", "id": commitment.id}]
    assert chat_messages[0].pending_action is None
    assert _HR_QUESTION in chat_messages[0].content
    assert chat_messages[0].author_kind == "agent"

    # ── step 3: the human queue lists it; the operator fulfils it ──────
    base = f"/api/v1/workspaces/{workspace.id}/human-queue"
    queue = await client.get(base, headers=owner_headers)
    assert queue.status_code == 200, queue.text
    queue_data = queue.json()
    assert [c["id"] for c in queue_data["commitments"]] == [commitment.id]
    assert queue_data["commitments"][0]["request_kind"] == "decision"
    assert queue_data["commitments"][0]["blocking"] is False
    assert queue_data["approvals"] == []  # standing grant left nothing pending

    fulfilled = await client.post(
        f"{base}/commitments/{commitment.id}/respond",
        headers=owner_headers,
        json={"action": "fulfill", "response": "Yes — go short-form."},
    )
    assert fulfilled.status_code == 200, fulfilled.text
    assert fulfilled.json()["status"] == "fulfilled"

    await db.refresh(commitment)
    assert commitment.status == "fulfilled"
    assert commitment.fulfilled_at is not None
    assert commitment.participant_id == owner_user_id
    assert commitment.response == {"text": "Yes — go short-form."}
    await db.refresh(hr_item)
    assert hr_item.status == "succeeded"
    assert hr_item.finished_at is not None

    fulfilled_events = await _events_of(db, workspace, et.HUMAN_COMMITMENT_FULFILLED)
    assert len(fulfilled_events) == 1
    assert fulfilled_events[0].source_id == commitment.id
    assert fulfilled_events[0].causation_id == hr_item.id
    assert fulfilled_events[0].actor_id == owner_user_id
    # The queue is empty again.
    queue = await client.get(base, headers=owner_headers)
    assert queue.json()["commitments"] == []

    # ── step 4: finish the batch, then the next review closes the loop ─
    await apply_task_status_transition(real_task, "completed", db=db)
    await check_work_batch_completion(db, real_task)
    await db.commit()
    await asyncio.sleep(0.002)
    result2 = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
    assert not result2.get("skipped")
    assert result2["task_count"] == 0

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 2
    review1, review2 = reviews
    assert review1.id == review1_id
    assert review2.status == "succeeded"
    # Watermark advanced contiguously.
    assert review2.watermark_start == review1.watermark_end
    assert review2.watermark_end > review2.watermark_start

    # previous_decisions carries BOTH auto-approvals — including the
    # human_request item — back to the Strategist.
    decisions = {d["task_id"]: d for d in review2.briefing["previous_decisions"]}
    assert {real_task.id, hr_item.id} <= set(decisions)
    assert decisions[hr_item.id]["decision"] == "approved"
    assert decisions[hr_item.id]["review_id"] == review1_id
    assert decisions[real_task.id]["decision"] == "approved"

    # human_participation consolidation shows the fulfilled cycle: the
    # two approvals happened in this window and nothing is waiting.
    hp_report = (await db.execute(
        select(ConsolidationReport).where(
            ConsolidationReport.review_id == review2.id,
            ConsolidationReport.domain
            == HumanParticipationConsolidator.domain,
        )
    )).scalar_one()
    metrics = hp_report.metrics
    assert metrics["active_human_commitments"] == 0
    assert metrics["blocking_commitments"] == 0
    assert metrics["decisions_since_last_review"]["approved"] >= 2
    assert metrics["decisions_since_last_review"]["rejected"] == 0
    assert metrics["contributions_since_last_review"] == 0
    assert metrics["open_approvals"] == 0


# ── Test E: experiment lifecycle loop ──────────────────────────────────

_XP_KEY = "shorter_digest_prompt"
_XP_HYPOTHESIS = (
    "A shorter digest prompt should stop the daily digest automation "
    "from failing and raise its success rate."
)
_XP_PATCH = {"payload_message": "patched: concise digest prompt"}


def _experiment_payload(job_id: str) -> dict:
    """0 tasks + 1 bounded experiment targeting the failing job."""
    payload = _proposal_payload(
        [], "No new work; one bounded experiment on the failing digest job.",
    )
    payload["experiments"] = [{
        "experiment_key": _XP_KEY,
        "hypothesis": _XP_HYPOTHESIS,
        "target_kind": "scheduled_job",
        "target_id": job_id,
        "overlay_patch": dict(_XP_PATCH),
        "max_runs": 2,
        "success_metrics": {"success_rate": {"target": 0.9}},
        "guardrails": {"rollback_on_consecutive_failures": 2},
    }]
    return payload


async def test_experiment_lifecycle_loop(client, db_session, monkeypatch):
    """Full journey: failing automation facts → review 1 proposes a bounded
    experiment that the standing grant auto-starts (overlay, frozen
    baseline, NO revision bump) → the cohort lands via the ``xp:{id}:``
    correlation → the guardrail tick completes on max_runs and
    auto-evaluates (overlay off) → review 2 consumes the lifecycle facts
    and its learning_evidence report + the timeline API close the loop."""
    from auth_helpers import register_user_and_get_token

    from packages.core.consolidators.learning_evidence import (
        LearningEvidenceConsolidator,
    )
    from packages.core.experiments import (
        EXPERIMENT_OVERLAY_KEY,
        check_experiment_guardrails,
    )
    from packages.core.governance.service import add_auto_approve_action
    from packages.core.models.experiment import Experiment
    from packages.core.models.scheduler import ScheduledJob

    db = db_session

    # ── step 1: owner + failing automation + experiment standing grant ─
    resp = await register_user_and_get_token(client, json={
        "username": "e2e_xp_owner",
        "email": "e2e_xp_owner@test.com",
        "password": "pass123",
        "entity_name": "E2E XP Corp",
    })
    token_data = resp.json()
    owner_headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert me.status_code == 200
    entity_id = me.json()["entity_id"]

    workspace = await _seed_workspace(
        db, name="E2E Experiment WS", entity_id=entity_id,
    )
    await _set_flag(db, True)
    await add_auto_approve_action(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        action_key="workspace.proposal.experiment",
        changed_by="operator",
    )
    await db.commit()

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"job_{generate_ulid()}",
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name="Daily digest",
        schedule_kind="every",
        every_seconds=6 * 3600.0,
        payload_message="original digest prompt",
        execution_type="agent",
        execution_target={"workspace_id": workspace.id},
        enabled=True,
    )
    db.add(job)
    await db.commit()

    # The failing automation the Strategist reacts to: 3 failed runs in
    # the ledger BEFORE review 1 (they also become the frozen baseline).
    for n in range(3):
        event = await record_event(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            event_type=et.AUTOMATION_RUN_FAILED,
            source_kind="scheduled_job",
            source_id=job.id,
            run_id=f"base_{n}",
            status="error",
            idempotency_key=f"e2e-xp:{workspace.id}:base:{n}",
        )
        assert event is not None
        await db.commit()
        await asyncio.sleep(0.002)

    # ── step 2: review 1 — 0 tasks, 1 experiment proposal ──────────────
    captured: list[dict] = []
    payloads = [_experiment_payload(job.id), dict(_EMPTY_PAYLOAD)]
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _scripted_completion(payloads, captured),
    )
    _quiet_side_surfaces(monkeypatch)

    result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")

    assert not result.get("skipped")
    assert result["task_count"] == 0
    review1_id = result["review_id"]
    assert len(result["experiments"]) == 1
    digest = result["experiments"][0]
    assert digest["outcome"] == "allow"      # standing grant
    assert digest["risk_level"] == "medium"  # no max_cost declared → ≤ $20
    assert digest["experiment_id"]
    assert digest["approval_request_id"] is None  # grant → no per-item card

    items = await _items(db, review1_id)
    assert [i.item_key for i in items] == [f"xp_{_XP_KEY}"]
    item = items[0]
    assert item.kind == "experiment"
    assert item.status == "executing"
    assert item.action_key == "workspace.proposal.experiment"
    assert item.approval_request_id is None
    assert item.execution_root_id == digest["experiment_id"]
    assert item.decision["decision"] == "approved"
    assert item.payload["max_runs"] == 2
    assert item.payload["hypothesis"] == _XP_HYPOTHESIS

    experiment = await db.get(Experiment, digest["experiment_id"])
    assert experiment is not None
    assert experiment.status == "running"
    assert experiment.proposal_item_id == item.id
    assert experiment.scope["target_kind"] == "scheduled_job"
    assert experiment.scope["target_id"] == job.id

    # Overlay on WITHOUT a revision bump; stored config untouched;
    # baseline frozen from the pre-review ledger (3 failed runs).
    await db.refresh(job)
    assert job.execution_target[EXPERIMENT_OVERLAY_KEY] == {
        "experiment_id": experiment.id,
        "patch": dict(_XP_PATCH),
    }
    assert job.revision == 1
    assert job.payload_message == "original digest prompt"
    assert experiment.baseline_snapshot["run_count"] == 3
    assert experiment.baseline_snapshot["success_rate"] == 0.0

    started = await _events_of(db, workspace, et.EXPERIMENT_STARTED)
    assert len(started) == 1
    assert started[0].source_id == experiment.id
    assert started[0].causation_id == item.id
    # The start decision landed AFTER review 1's frozen window.
    review1 = (await _reviews(db, workspace.id))[0]
    assert review1.status == "succeeded"
    assert started[0].id > review1.watermark_end

    # ── step 3: cohort runs recovered from the ledger by correlation ───
    for period in ("p1", "p2"):
        for event_type, status in (
            (et.AUTOMATION_RUN_DISPATCHED, "dispatched"),
            (et.AUTOMATION_RUN_COMPLETED, "success"),
        ):
            event = await record_event(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                event_type=event_type,
                source_kind="scheduled_job",
                source_id=job.id,
                run_id=f"xr_{period}",
                correlation_id=(
                    f"xp:{experiment.id}:{period}"
                    if event_type == et.AUTOMATION_RUN_DISPATCHED
                    else None
                ),
                status=status,
                idempotency_key=f"e2e-xp:{workspace.id}:{period}:{event_type}",
            )
            assert event is not None
            await db.commit()
            await asyncio.sleep(0.002)

    # ── step 4: guardrail tick → completed on max_runs + auto-evaluated ─
    tick = await check_experiment_guardrails(db)
    await db.commit()
    mine = [row for row in tick if row["experiment_id"] == experiment.id]
    assert len(mine) == 1
    assert mine[0]["outcome"] == "completed"
    assert mine[0]["reason"] == "max_runs 2 reached"
    assert mine[0]["run_count"] == 2

    await db.refresh(experiment)
    await db.refresh(job)
    assert experiment.status == "evaluated"
    assert experiment.evaluated_at is not None
    # Overlay removed on stop; the job config is restored, still revision 1.
    assert EXPERIMENT_OVERLAY_KEY not in (job.execution_target or {})
    assert job.execution_target == {"workspace_id": workspace.id}
    assert job.revision == 1

    # Deterministic evaluation: 2/2 cohort successes vs the 0.9 target,
    # baseline from the frozen snapshot (0.0), no guardrail violations.
    verdict = experiment.evaluation["metrics"]["success_rate"]
    assert verdict["cohort"] == pytest.approx(1.0)
    assert verdict["target"] == 0.9
    assert verdict["baseline"] == 0.0
    assert verdict["met"] is True
    assert experiment.evaluation["cohort"] == {
        "run_count": 2,
        "finished_count": 2,
        "completed": 2,
        "failed": 0,
        "success_rate": pytest.approx(1.0),
    }
    assert experiment.evaluation["guardrail_violations"] is False

    completed_events = await _events_of(db, workspace, et.EXPERIMENT_COMPLETED)
    assert len(completed_events) == 1
    assert completed_events[0].source_id == experiment.id
    assert completed_events[0].causation_id == item.id
    assert completed_events[0].payload == {"reason": "max_runs 2 reached"}
    evaluated_events = await _events_of(db, workspace, et.EXPERIMENT_EVALUATED)
    assert len(evaluated_events) == 1
    assert evaluated_events[0].source_id == experiment.id
    assert evaluated_events[0].causation_id == item.id
    assert evaluated_events[0].payload["metrics_met"] == 1
    assert evaluated_events[0].payload["run_count"] == 2

    # ── step 5: review 2 consumes the lifecycle facts ──────────────────
    result2 = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
    assert not result2.get("skipped")
    assert result2["task_count"] == 0

    reviews = await _reviews(db, workspace.id)
    assert len(reviews) == 2
    review2 = reviews[1]
    assert review2.status == "succeeded"
    # Watermark advanced contiguously past the whole experiment story.
    assert review2.watermark_start == review1.watermark_end
    assert review2.watermark_end > review2.watermark_start
    window_ids = await _window_event_ids(db, review2)
    assert {
        started[0].id, completed_events[0].id, evaluated_events[0].id,
    } <= window_ids

    # learning_evidence consolidation carries the numbers-only digest.
    le_report = (await db.execute(
        select(ConsolidationReport).where(
            ConsolidationReport.review_id == review2.id,
            ConsolidationReport.domain == LearningEvidenceConsolidator.domain,
        )
    )).scalar_one()
    xp_digest = {e["id"]: e for e in le_report.metrics["experiments"]}
    assert experiment.id in xp_digest
    assert xp_digest[experiment.id]["status"] == "evaluated"
    summary = xp_digest[experiment.id]["verdict_summary"]
    assert summary["metrics_met"] == 1
    assert summary["metrics_evaluable"] == 1
    assert summary["metrics_declared"] == 1
    assert summary["guardrail_violations"] is False
    assert summary["run_count"] == 2
    assert summary["success_rate"] == pytest.approx(1.0)
    # started + completed + evaluated all fell in review 2's window.
    assert le_report.coverage["sources"]["experiment_events"] == 3
    assert f"experiment:{experiment.id}" in le_report.evidence_refs

    # ── step 6: the timeline API renders review 1's tree ───────────────
    timeline = await client.get(
        f"/api/v1/workspaces/{workspace.id}/timeline?review_id={review1.id}",
        headers=owner_headers,
    )
    assert timeline.status_code == 200, timeline.text
    tree = timeline.json()
    assert tree["review"]["id"] == review1.id
    assert tree["review"]["status"] == "succeeded"
    assert tree["review"]["watermark_end"] == review1.watermark_end
    assert {r["domain"] for r in tree["reports"]} == set(review1.briefing["reports"])
    tree_items = {i["item_key"]: i for i in tree["proposal"]["items"]}
    assert set(tree_items) == {f"xp_{_XP_KEY}"}
    xp_node = tree_items[f"xp_{_XP_KEY}"]
    assert xp_node["kind"] == "experiment"
    assert xp_node["status"] == "executing"
    assert xp_node["risk_level"] == "medium"
    assert xp_node["decision"]["decision"] == "approved"
    assert xp_node["execution_root_id"] == experiment.id
    # The experiment root is present in the executions branch.
    assert experiment.id in tree["executions"]
