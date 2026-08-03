"""Typed review triggers — a human's request is never silently skipped.

Before ``ReviewTriggerKind`` the Celery task carried one free-text
``trigger`` string and decided suppression with
``not trigger.startswith("user_feedback")``. The chat tool sent
``"user_request: <reason>"``, which failed that prefix test, so an
explicitly requested review was dropped without a review, a proposal or a
word back to the person who asked.

These tests pin the replacement: behavior comes from the enum, prose is
inert, and every path a person can be waiting on either runs or says why
it cannot.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.review_run import ReviewRun
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.proposals.constants import (
    LEARNING_EXCLUDED_REASON_CODES,
    REASON_CODES,
    USER_REASON_CODES,
)
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.strategist.triggers import (
    ReviewTrigger,
    ReviewTriggerKind,
    classify_legacy_trigger,
)
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"

_seq = 0


# ── scaffolding ───────────────────────────────────────────────────────

async def _seed_workspace(db) -> Workspace:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Trigger Kind WS",
        status="active",
        settings={},
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Grow followers",
        metric_key="follower_count",
        target_value=1000,
        status="active",
    )
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Ops Agent", status="active")
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


async def _emit(db, workspace: Workspace) -> None:
    global _seq
    _seq += 1
    event = await record_event(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id=f"task_{_seq}",
        idempotency_key=f"trigger-kind:{workspace.id}:{_seq}",
    )
    assert event is not None
    await db.commit()
    await asyncio.sleep(0.002)


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


_ONE_TASK = {
    "summary": "Ship the next thing.",
    "tasks": [{
        "task_key": "ship_it",
        "title": "Ship the next thing",
        "description": "Do the next obvious thing.",
        "owner_service_key": "ops",
        "priority": 3,
        "basis": {"report_refs": ["goal"], "evidence_refs": []},
        "deliverables": [{
            "name": "shipped",
            "kind": "value",
            "shape": "TextResult",
            "acceptance": "shipped",
            "usage": "operator review",
        }],
    }],
}


def _install_llm(monkeypatch, counter: list[int]):
    async def fake(system_prompt, user_prompt, **kwargs):
        counter.append(1)
        return SimpleNamespace(content=json.dumps(_ONE_TASK))

    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion", fake,
    )
    return counter


def _quiet_side_effects(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)


async def _seed_open_proposal(db, workspace: Workspace, *, review_id: str) -> Task:
    task = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Undecided proposal from the last review",
        description="Waiting on the operator.",
        status="proposed",
        details={"strategist_review_id": review_id},
    )
    db.add(task)
    await db.commit()
    return task


async def _skip_notices(db, workspace: Workspace) -> list[Message]:
    conv_id = (await db.execute(
        select(Conversation.id).where(Conversation.workspace_id == workspace.id)
    )).scalars().first()
    if conv_id is None:
        return []
    rows = (await db.execute(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.id.asc())
    )).scalars().all()
    return [m for m in rows if (m.meta or {}).get("strategist_review_skip")]


# ── 1. every call site's kind mapping ─────────────────────────────────

@pytest.mark.parametrize(
    "legacy_trigger,expected_kind",
    [
        # scheduler_tasks.py — the workspace cadence tick
        ("scheduled", ReviewTriggerKind.SCHEDULED),
        # workspace_setup_service.py — first review after setup
        ("workspace_created", ReviewTriggerKind.EVENT),
        # workspace_operation_service.py — a work wave finished
        ("work_batch_completed:01JABCDEF0123456789ABCDEF", ReviewTriggerKind.EVENT),
        # monitor_tasks.py — readiness snapshot moved
        ("readiness_changed: agents: 1->2", ReviewTriggerKind.EVENT),
        # workspace_task_actions.py — the chat tool (the silent-skip bug)
        ("user_request: replan after the new lease priorities", ReviewTriggerKind.HUMAN_REQUESTED),
        # workspace_chat.py — "request changes" on a proposal card
        ("user_feedback: these are too expensive", ReviewTriggerKind.HUMAN_REQUESTED),
        # workspace_chat.py — retry button on the failure card
        ("manual_retry_after_failure: scheduled", ReviewTriggerKind.HUMAN_REQUESTED),
    ],
)
def test_every_call_site_trigger_maps_to_its_kind(legacy_trigger, expected_kind):
    # Asserted through the classifier, never by re-matching the string here.
    assert classify_legacy_trigger(legacy_trigger) is expected_kind
    assert ReviewTrigger.coerce(legacy_trigger).kind is expected_kind


def test_only_human_requested_is_human_initiated():
    assert ReviewTriggerKind.HUMAN_REQUESTED.is_human_initiated is True
    assert ReviewTriggerKind.SCHEDULED.is_human_initiated is False
    assert ReviewTriggerKind.EVENT.is_human_initiated is False
    # ``suppressible`` is the rule behavior branches on; it is the inverse.
    assert ReviewTriggerKind.SCHEDULED.suppressible is True
    assert ReviewTriggerKind.EVENT.suppressible is True
    assert ReviewTriggerKind.HUMAN_REQUESTED.suppressible is False


def test_detail_prose_never_changes_the_kind():
    # The exact prose that used to flip behavior is now inert.
    for detail in ("user_feedback: x", "scheduled", "", "anything at all"):
        trigger = ReviewTrigger(kind=ReviewTriggerKind.EVENT, detail=detail)
        assert trigger.kind is ReviewTriggerKind.EVENT
        assert trigger.is_human_initiated is False


# ── 2. HUMAN_REQUESTED + open proposals → structured conflict ─────────

async def test_human_requested_review_with_open_proposals_is_not_silently_skipped(
    db_session, monkeypatch,
):
    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _set_flag(db_session, True)
    llm_calls: list[int] = []
    _install_llm(monkeypatch, llm_calls)
    _quiet_side_effects(monkeypatch)
    blocking = await _seed_open_proposal(db_session, workspace, review_id="rv_prev")

    result = await _execute_strategist_review_cycle(
        db_session,
        workspace.id,
        ReviewTrigger(
            kind=ReviewTriggerKind.HUMAN_REQUESTED,
            detail="replan after the new lease priorities",
        ),
    )

    # It says what blocks it instead of evaporating.
    assert result["needs_decision"] is True
    assert result["reason"] == "open_proposals"
    conflict = result["conflict"]
    assert conflict["open_count"] == 1
    assert conflict["proposals"][0]["task_id"] == blocking.id
    assert conflict["proposals"][0]["title"] == blocking.title
    assert conflict["review_id"] == "rv_prev"
    assert conflict["proposals"][0]["proposed_at"]

    # No LLM call, no proposal, and no review marked succeeded.
    assert llm_calls == []
    runs = (await db_session.execute(
        select(ReviewRun).where(ReviewRun.workspace_id == workspace.id)
    )).scalars().all()
    assert runs and all(run.status != "succeeded" for run in runs)
    assert all(run.trigger_kind == ReviewTriggerKind.HUMAN_REQUESTED.value for run in runs)
    assert runs[0].trigger_detail == "replan after the new lease priorities"
    still_proposed = (await db_session.execute(
        select(Task).where(Task.workspace_id == workspace.id, Task.status == "proposed")
    )).scalars().all()
    assert [t.id for t in still_proposed] == [blocking.id]


# ── 3. supersede=true → cohort rejected, review runs ──────────────────

async def test_supersede_rejects_open_cohort_then_runs_the_review(
    db_session, monkeypatch,
):
    from packages.core.ai.runtime.workspace_task_actions import (
        runtime_workspace_request_strategist_review_action,
    )

    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _set_flag(db_session, True)
    llm_calls: list[int] = []
    _install_llm(monkeypatch, llm_calls)
    _quiet_side_effects(monkeypatch)
    blocking = await _seed_open_proposal(db_session, workspace, review_id="rv_prev")

    enqueued: list[dict] = []

    class _FakeTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            enqueued.append(kwargs)
            return SimpleNamespace(id="celery-superseded")

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeTask)

    # First call: blocked, nothing enqueued, the model is handed the conflict.
    first = json.loads(await runtime_workspace_request_strategist_review_action(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        params={"reason": "New priorities from the owner."},
    ))
    assert first["requested"] is False
    assert first["needs_decision"] is True
    assert first["conflict"]["open_count"] == 1
    assert "supersede=true" in first["next_step"]
    assert enqueued == []

    # Second call, after the user confirmed in chat.
    second = json.loads(await runtime_workspace_request_strategist_review_action(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        params={"reason": "New priorities from the owner.", "supersede": True},
    ))
    assert second["requested"] is True
    assert second["trigger_kind"] == ReviewTriggerKind.HUMAN_REQUESTED.value
    assert second["superseded"]["rejected_task_ids"] == [blocking.id]
    assert second["superseded"]["reason_code"] == "SUPERSEDED"
    assert enqueued and enqueued[0]["kwargs"]["trigger_kind"] == (
        ReviewTriggerKind.HUMAN_REQUESTED.value
    )

    await db_session.refresh(blocking)
    assert blocking.status == "cancelled"

    # The rejection is recorded through the normal path with the new code.
    from packages.core.models.workspace_event import WorkspaceEvent
    events = (await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == et.PROPOSAL_ITEM_REJECTED,
        )
    )).scalars().all()
    assert [e.payload.get("rejection_reason_code") for e in events] == ["SUPERSEDED"]

    # …and the review the tool asked for actually runs and proposes again.
    result = await _execute_strategist_review_cycle(
        db_session,
        workspace.id,
        ReviewTrigger(
            kind=ReviewTriggerKind.HUMAN_REQUESTED, detail="New priorities from the owner.",
        ),
    )
    assert not result.get("needs_decision")
    assert result["task_count"] == 1
    assert llm_calls == [1]
    new_proposals = (await db_session.execute(
        select(Task).where(Task.workspace_id == workspace.id, Task.status == "proposed")
    )).scalars().all()
    assert [t.title for t in new_proposals] == ["Ship the next thing"]


# ── 4. SCHEDULED still suppressed; notice posted once ─────────────────

async def test_scheduled_review_with_open_proposals_is_suppressed_and_announced_once(
    db_session, monkeypatch,
):
    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _set_flag(db_session, True)
    llm_calls: list[int] = []
    _install_llm(monkeypatch, llm_calls)
    _quiet_side_effects(monkeypatch)
    await _seed_open_proposal(db_session, workspace, review_id="rv_prev")

    first = await _execute_strategist_review_cycle(
        db_session, workspace.id, ReviewTriggerKind.SCHEDULED,
    )
    assert first["skipped"] is True
    assert first["reason"] == "open_proposals"
    assert first.get("needs_decision") is None
    assert llm_calls == []

    notices = await _skip_notices(db_session, workspace)
    assert len(notices) == 1
    assert "still awaiting your decision" in notices[0].content
    assert notices[0].message_kind == "proposal"
    assert notices[0].pending_action is None

    # The next cadence tick trips over the same blocked state: still
    # suppressed, and it does NOT post the same line again.
    second = await _execute_strategist_review_cycle(
        db_session, workspace.id, ReviewTriggerKind.SCHEDULED,
    )
    assert second["skipped"] is True
    assert second["reason"] == "open_proposals"
    assert len(await _skip_notices(db_session, workspace)) == 1

    # A different blocking state earns a fresh line.
    await _seed_open_proposal(db_session, workspace, review_id="rv_prev")
    third = await _execute_strategist_review_cycle(
        db_session, workspace.id, ReviewTriggerKind.SCHEDULED,
    )
    assert third["skipped"] is True
    assert len(await _skip_notices(db_session, workspace)) == 2


# ── 5. legacy bare-string wire payload ───────────────────────────────

def test_legacy_bare_string_from_a_queued_task_is_classified_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="packages.core.strategist.triggers"):
        trigger = ReviewTrigger.from_wire(trigger="user_request: replan the quarter")

    assert trigger.kind is ReviewTriggerKind.HUMAN_REQUESTED
    assert trigger.detail == "user_request: replan the quarter"
    assert any("DEPRECATED" in record.message for record in caplog.records)


def test_new_wire_form_carries_kind_and_detail_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="packages.core.strategist.triggers"):
        trigger = ReviewTrigger.from_wire(
            trigger_kind="event", trigger_detail="work batch completed: 01J",
        )

    assert trigger.kind is ReviewTriggerKind.EVENT
    assert trigger.detail == "work batch completed: 01J"
    assert not [r for r in caplog.records if "DEPRECATED" in r.message]
    assert trigger.celery_kwargs() == {
        "trigger_kind": "event", "trigger_detail": "work batch completed: 01J",
    }


def test_unknown_legacy_prose_stays_suppressible():
    # Unrecognised text must never be promoted to "a human is waiting".
    assert classify_legacy_trigger("something nobody wrote down") is ReviewTriggerKind.EVENT


# ── 6/7. SUPERSEDED vocabulary placement ─────────────────────────────

def test_superseded_is_a_valid_but_system_only_reason_code():
    assert "SUPERSEDED" in REASON_CODES
    # Never offered on the operator's reject dialog — it is system-generated,
    # not a human's stated reason.
    assert "SUPERSEDED" not in USER_REASON_CODES
    assert LEARNING_EXCLUDED_REASON_CODES == frozenset({"SUPERSEDED"})


async def test_superseded_is_excluded_from_the_learning_rejection_distribution(
    db_session,
):
    from packages.core.consolidators.base import SnapshotContext
    from packages.core.consolidators.learning_evidence import LearningEvidenceConsolidator
    from packages.core.review import begin_review, events_in_window

    workspace = await _seed_workspace(db_session)
    for idx, (reason, code) in enumerate((
        ("Too expensive for this quarter", "TOO_EXPENSIVE"),
        ("Wrong direction entirely", "WRONG_DIRECTION"),
        ("Superseded: a newer Strategist review was requested", "SUPERSEDED"),
    )):
        await record_event(
            db_session,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            event_type=et.PROPOSAL_ITEM_REJECTED,
            source_kind="proposal",
            source_id=f"task_{idx}",
            idempotency_key=f"trigger-kind-reject:{workspace.id}:{idx}",
            payload={"rejection_reason": reason, "rejection_reason_code": code},
        )
        await db_session.commit()
        await asyncio.sleep(0.002)

    review = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger=ReviewTriggerKind.SCHEDULED,
    )
    await db_session.commit()
    ctx = SnapshotContext(
        review=review,
        workspace=workspace,
        events=await events_in_window(db_session, review),
    )
    report = await LearningEvidenceConsolidator().run(db_session, ctx)

    distribution = report.metrics["rejection_reason_distribution"]
    # Real feedback is counted…
    assert distribution["Too expensive for this quarter"] == 1
    assert distribution["Wrong direction entirely"] == 1
    # …a superseded cohort is not: it says nothing about the proposal.
    assert not any("Superseded" in key for key in distribution)
    assert "2 rejection(s)" in report.summary
