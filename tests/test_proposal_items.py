"""M7/M8 — proposal_items bookkeeping + HitlRequest wiring (v1 slice).

On the strategist_review_v2 path each persisted proposed Task also gets a
``proposal_items`` row, and the cohort's approval is resolved ONCE through
the unified approval core:

* standing grant (policy auto_approve_actions) → auto-approve, items
  ``approved`` with the work-batch execution root
* no grant → items stay ``proposed`` behind one pending HitlRequest;
  the existing chat-card approve/reject mirrors decisions onto items and
  settles the request (grant+consume / deny)
* legacy boolean ``strategist.auto_approve_proposals`` still auto-approves
* flag off → zero proposal rows (legacy path untouched)
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.proposals.service import decide_items
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"

_seq = 0


async def _seed_workspace(db, *, settings: dict | None = None) -> Workspace:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Proposal Items WS",
        status="active",
        settings=settings or {},
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Grow followers",
        metric_key="follower_count",
        target_value=1000,
        status="active",
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
        idempotency_key=f"proposal-items:{workspace.id}:{_seq}",
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


_TWO_TASKS = {
    "summary": "Draft docs, then publish.",
    "tasks": [
        {
            "task_key": "draft_docs",
            "title": "Draft source docs",
            "description": "Write the source docs for this cycle.",
            "owner_service_key": "ops",
            "priority": 3,
            "correlation_key": "draft_docs_weekly",
            "basis": {
                # "goal" is a real briefing domain; "bogus_domain" is not —
                # the v1 validator strips it and keeps the valid ref.
                "report_refs": ["goal", "bogus_domain"],
                "evidence_refs": ["ev_1"],
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
}


def _fake_completion(payload: dict = _TWO_TASKS):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


async def _run_v2_review(db, monkeypatch, workspace: Workspace, *, flag: bool = True) -> dict:
    await _emit(db, workspace)
    await _set_flag(db, flag)
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(),
    )
    # Keep the review off real chat + Celery surfaces.
    async def _noop_post(*args, **kwargs):
        return None
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop_post)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)
    return await _execute_strategist_review_cycle(db, workspace.id, "scheduled")


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
    return {t.details.get("strategist_task_key"): t for t in rows}


async def _proposal_request(db, proposal_id: str) -> HitlRequest | None:
    return (await db.execute(
        select(HitlRequest).where(
            HitlRequest.dedup_key == f"proposal:{proposal_id}",
        ).order_by(HitlRequest.created_at.desc()).limit(1)
    )).scalar_one_or_none()


# ── persistence + validator ────────────────────────────────────────────

async def test_v2_review_persists_proposal_record_and_items(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace)

    assert not result.get("skipped")
    assert result["task_count"] == 2
    assert result["approval_outcome"] == "needs_human"

    record = (await db_session.execute(
        select(ProposalRecord).where(ProposalRecord.review_id == result["review_id"])
    )).scalar_one()
    assert record.workspace_id == workspace.id
    assert record.status == "open"
    assert record.summary == "Draft docs, then publish."
    assert result["proposal_id"] == record.id

    items = await _items(db_session, result["review_id"])
    assert [i.item_key for i in items] == ["draft_docs", "publish_docs"]
    by_key = {i.item_key: i for i in items}
    tasks = await _tasks(db_session, workspace)

    for key, item in by_key.items():
        assert item.kind == "task"
        assert item.action_key == "workspace.proposal.task"
        assert item.risk_level == "low"
        assert item.status == "proposed"
        assert item.payload["task_id"] == tasks[key].id

    # Validator stripped the unknown report ref, kept the valid domain ref.
    assert by_key["draft_docs"].basis["report_refs"] == ["goal"]
    assert by_key["draft_docs"].basis["evidence_refs"] == ["ev_1"]
    assert by_key["draft_docs"].correlation_key == "draft_docs_weekly"
    assert by_key["publish_docs"].basis["report_refs"] == ["execution"]
    assert by_key["publish_docs"].depends_on_item_keys == ["draft_docs"]
    assert result.get("validation_notes")
    assert "bogus_domain" in result["validation_notes"][0]


# ── standing grant → auto-approve ─────────────────────────────────────

async def test_standing_grant_auto_approves_cohort(db_session, monkeypatch):
    from packages.core.governance.service import add_auto_approve_action

    workspace = await _seed_workspace(db_session)
    await add_auto_approve_action(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        action_key="workspace.proposal.task",
        changed_by="operator",
    )
    await db_session.commit()

    result = await _run_v2_review(db_session, monkeypatch, workspace)

    assert result["approval_outcome"] == "allow"
    assert result["auto_approved"] is True
    assert set(result["approved_task_ids"]) == set(result["task_ids"])

    tasks = await _tasks(db_session, workspace)
    assert tasks["draft_docs"].status == "in_progress"
    # Downstream task waits on its dependency (approved-but-not-started).
    assert tasks["publish_docs"].status == "pending"
    batch_id = tasks["draft_docs"].details.get("workspace_work_batch_id")
    assert batch_id

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"approved"}
    assert {i.execution_root_id for i in items} == {batch_id}
    assert all(i.decision["decided_by"] == "system" for i in items)
    assert all(i.decided_at is not None for i in items)

    record = await db_session.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"

    # Standing grant means no request needed to be minted; if one was, it
    # must not be left open.
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is None or req.status == "consumed"

    events = list((await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == et.PROPOSAL_ITEM_APPROVED,
        )
    )).scalars().all())
    assert len(events) == 2


# ── no grant → pending request, then chat-card approve ────────────────

async def test_no_grant_leaves_items_proposed_then_card_approve(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace)

    assert result["approval_outcome"] == "needs_human"
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is not None
    assert req.status == "pending"
    assert req.action_key == "workspace.proposal.task"
    assert req.resource_kind == "proposal"
    assert req.resource_id == result["proposal_id"]
    assert req.origin_kind == "operation"
    assert result["approval_request_id"] == req.id

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"proposed"}
    assert {i.approval_request_id for i in items} == {req.id}
    assert result["auto_approved"] is False

    tasks = await _tasks(db_session, workspace)
    assert {t.status for t in tasks.values()} == {"proposed"}

    # Operator clicks approve on the existing chat card.
    user_id = generate_ulid()
    moved = await strategist_service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        actor_id=user_id,
    )
    await db_session.commit()
    assert set(moved) == set(result["task_ids"])

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"approved"}
    assert all(i.decision["decided_by"] == user_id for i in items)
    assert all(i.execution_root_id for i in items)

    await db_session.refresh(req)
    assert req.status == "consumed"
    assert req.decided_by_user_id == user_id

    record = await db_session.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"


# ── reject mirrors decision + denies the request ──────────────────────

async def test_reject_records_reason_and_denies_request(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace)
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is not None and req.status == "pending"

    user_id = generate_ulid()
    cancelled = await strategist_service.reject_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        reason="Wrong direction this cycle",
        actor_id=user_id,
    )
    await db_session.commit()
    assert set(cancelled) == set(result["task_ids"])

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"rejected"}
    for item in items:
        assert item.decision["reason_code"] == "OTHER"
        assert item.decision["comment"] == "Wrong direction this cycle"
        assert item.decision["decided_by"] == user_id
        assert item.execution_root_id is None

    await db_session.refresh(req)
    assert req.status == "denied"

    record = await db_session.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"

    tasks = await _tasks(db_session, workspace)
    assert {t.status for t in tasks.values()} == {"cancelled"}


# ── legacy boolean auto-approve still works on v2 ─────────────────────

async def test_legacy_boolean_auto_approve_still_works_on_v2(db_session, monkeypatch):
    workspace = await _seed_workspace(
        db_session,
        settings={"strategist": {"auto_approve_proposals": True}},
    )
    result = await _run_v2_review(db_session, monkeypatch, workspace)

    # No standing grant → the resolver wants a human, but the legacy
    # workspace boolean still auto-approves for compat...
    assert result["approval_outcome"] == "needs_human"
    assert result["auto_approved"] is True

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"approved"}

    # ...and the minted request is settled, not orphaned.
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is not None
    assert req.status == "consumed"

    tasks = await _tasks(db_session, workspace)
    assert tasks["draft_docs"].status == "in_progress"
    assert tasks["publish_docs"].status == "pending"


# ── reason_code vocabulary is enforced ────────────────────────────────

async def test_decide_items_rejects_unknown_reason_code(db_session):
    import pytest

    with pytest.raises(ValueError, match="reason_code"):
        await decide_items(
            db_session,
            review_id="rv_bogus",
            task_ids=["t1"],
            decision="rejected",
            reason_code="NOT_A_CODE",
        )
    with pytest.raises(ValueError, match="decision"):
        await decide_items(
            db_session,
            review_id="rv_bogus",
            task_ids=["t1"],
            decision="maybe",
        )


# ── flag off → legacy path untouched ──────────────────────────────────

async def test_flag_off_creates_no_proposal_rows(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace, flag=False)

    assert not result.get("skipped")
    assert result["task_count"] == 2
    assert result["review_id"].startswith("rv_")
    assert "approval_outcome" not in result
    assert "proposal_id" not in result

    records = list((await db_session.execute(
        select(ProposalRecord).where(ProposalRecord.workspace_id == workspace.id)
    )).scalars().all())
    assert records == []
    item_rows = list((await db_session.execute(
        select(ProposalItemRecord).where(ProposalItemRecord.workspace_id == workspace.id)
    )).scalars().all())
    assert item_rows == []

    reqs = list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.workspace_id == workspace.id)
    )).scalars().all())
    assert reqs == []

    tasks = await _tasks(db_session, workspace)
    assert {t.status for t in tasks.values()} == {"proposed"}
