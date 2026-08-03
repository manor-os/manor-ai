"""M10 human_request proposal items — end-to-end (v2 path only).

Covers:
* v2 review whose LLM output carries tasks + human_requests → one
  kind="human_request" proposal item per request, auto-approved (no
  HitlRequest minted for them) and immediately executing with an
  open HumanCommitment as its execution root; ledger carries
  proposal_item_approved (decided_via=auto) + human_commitment_opened
  with causation_id = item id; chat surfacing is invoked
* commitment fulfilment flips the item to succeeded (finished_at set);
  decline flips it to cancelled — both via the resolve_commitment hook
* _post_human_request_chat posts plain informational messages
  (message_kind="hitl_request", NO pending_action)
* POST /human-queue/commitments/{id}/respond: fulfill + decline flows,
  409 on double-respond, 403 for a viewer on role-required commitments,
  fallback-allow for role-free commitments, 404 cross-entity
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import AsyncClient
from sqlalchemy import select

from packages.core.humans import open_commitment, resolve_commitment
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.participant import HumanCommitment
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceStaff
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"

_seq = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_workspace(db) -> Workspace:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Human Request WS",
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
        idempotency_key=f"human-request-items:{workspace.id}:{_seq}",
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


_TASKS_AND_HUMAN_REQUESTS = {
    "summary": "One doc task; two things need a human.",
    "tasks": [
        {
            "task_key": "draft_docs",
            "title": "Draft source docs",
            "description": "Write the source docs for this cycle.",
            "owner_service_key": "ops",
            "priority": 3,
            "deliverables": [{
                "name": "docs",
                "kind": "value",
                "shape": "TextResult",
                "acceptance": "docs drafted",
                "usage": "operator review",
            }],
        },
    ],
    "human_requests": [
        {
            "request_key": "confirm_direction",
            "request_kind": "decision",
            "role_required": "workspace_owner",
            "question": "Confirm the pivot to short-form video for this month's content plan?",
            "expected_by_hours": 48,
            "context": "Engagement dropped 30% on long-form posts.",
        },
        {
            "request_key": "provide_brand_assets",
            "request_kind": "input",
            "question": "Upload the refreshed brand asset pack for the docs restyle.",
        },
    ],
}


def _fake_completion(payload: dict):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


async def _run_v2_review(db, monkeypatch, workspace: Workspace, *, payload: dict) -> tuple[dict, list]:
    await _emit(db, workspace)
    await _set_flag(db, True)
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(payload),
    )

    async def _noop_post(*args, **kwargs):
        return None
    posted_human_requests: list = []

    async def _capture_hr_post(workspace_arg, entries):
        posted_human_requests.extend(entries)
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop_post)
    monkeypatch.setattr(strategist_service, "_post_human_request_chat", _capture_hr_post)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)
    result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
    return result, posted_human_requests


async def _human_request_items(db, review_id: str) -> list[ProposalItemRecord]:
    return list((await db.execute(
        select(ProposalItemRecord)
        .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
        .where(
            ProposalRecord.review_id == review_id,
            ProposalItemRecord.kind == "human_request",
        )
        .order_by(ProposalItemRecord.item_key.asc())
    )).scalars().all())


# ── v2 review → items + commitments + ledger + chat ───────────────────

async def test_v2_review_creates_human_request_items_and_commitments(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result, posted = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_TASKS_AND_HUMAN_REQUESTS,
    )

    assert not result.get("skipped")
    assert result["task_count"] == 1
    assert len(result["human_requests"]) == 2

    items = await _human_request_items(db_session, result["review_id"])
    assert len(items) == 2
    by_key = {i.item_key: i for i in items}
    assert set(by_key) == {"hr_confirm_direction", "hr_provide_brand_assets"}

    for item in items:
        assert item.status == "executing"
        assert item.action_key == "workspace.proposal.human_request"
        assert item.approval_request_id is None
        assert item.decision["decision"] == "auto"
        assert item.decision["decided_by"] is None
        assert item.decided_at is not None
        assert item.execution_root_id

    decision_item = by_key["hr_confirm_direction"]
    assert decision_item.payload["request_kind"] == "decision"
    assert decision_item.payload["role_required"] == "workspace_owner"

    # One waiting commitment per item; item's execution root = commitment.
    commitments = {
        c.source_id: c
        for c in (await db_session.execute(
            select(HumanCommitment).where(
                HumanCommitment.workspace_id == workspace.id,
                HumanCommitment.source_kind == "proposal_item",
            )
        )).scalars().all()
    }
    assert set(commitments) == {i.id for i in items}
    for item in items:
        commitment = commitments[item.id]
        assert item.execution_root_id == commitment.id
        assert commitment.status == "waiting"
        assert commitment.expected_input == item.payload["question"]
        assert commitment.role_required == item.payload.get("role_required")
        assert commitment.participant_id is None

    # expected_by_hours=48 → expected_by roughly two days out; None stays None.
    decision_commitment = commitments[decision_item.id]
    assert decision_commitment.expected_by is not None
    delta = decision_commitment.expected_by - _utcnow()
    assert timedelta(hours=47) < delta < timedelta(hours=49)
    assert commitments[by_key["hr_provide_brand_assets"].id].expected_by is None

    # No HitlRequest exists for human_request items (only the task
    # cohort request may exist).
    reqs = list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.workspace_id == workspace.id)
    )).scalars().all())
    assert all(r.action_key == "workspace.proposal.task" for r in reqs)

    # Ledger: commitment opened with causation = item id; item approved
    # via auto decision.
    opened = list((await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.entity_id == workspace.entity_id,
            WorkspaceEvent.event_type == et.HUMAN_COMMITMENT_OPENED,
        )
    )).scalars().all())
    assert {e.causation_id for e in opened} == {i.id for i in items}

    approved_events = list((await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.entity_id == workspace.entity_id,
            WorkspaceEvent.event_type == et.PROPOSAL_ITEM_APPROVED,
            WorkspaceEvent.source_id.in_([i.id for i in items]),
        )
    )).scalars().all())
    assert len(approved_events) == 2
    for event in approved_events:
        assert event.payload["decided_via"] == "auto"
        assert event.payload["kind"] == "human_request"
        assert event.root_execution_id in {c.id for c in commitments.values()}

    # Chat surfacing was invoked with both requests.
    assert len(posted) == 2
    assert {p["commitment_id"] for p in posted} == {c.id for c in commitments.values()}
    assert all(p["question"] for p in posted)


# ── commitment terminal states drive the item ─────────────────────────

async def test_commitment_fulfilment_and_decline_drive_item_status(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result, _ = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_TASKS_AND_HUMAN_REQUESTS,
    )
    items = await _human_request_items(db_session, result["review_id"])
    by_key = {i.item_key: i for i in items}
    fulfil_item = by_key["hr_confirm_direction"]
    decline_item = by_key["hr_provide_brand_assets"]

    user_id = generate_ulid()

    fulfil_commitment = await db_session.get(HumanCommitment, fulfil_item.execution_root_id)
    await resolve_commitment(
        db_session, fulfil_commitment,
        status="fulfilled",
        response={"text": "Yes — go short-form."},
        participant_id=user_id,
    )
    await db_session.refresh(fulfil_item)
    assert fulfil_commitment.status == "fulfilled"
    assert fulfil_commitment.fulfilled_at is not None
    assert fulfil_item.status == "succeeded"
    assert fulfil_item.finished_at is not None

    decline_commitment = await db_session.get(HumanCommitment, decline_item.execution_root_id)
    await resolve_commitment(
        db_session, decline_commitment,
        status="declined",
        response={"text": "Assets are not ready yet."},
        participant_id=user_id,
    )
    await db_session.refresh(decline_item)
    assert decline_commitment.status == "declined"
    assert decline_item.status == "cancelled"
    assert decline_item.finished_at is not None

    # Terminal commitment events carry causation back to the item.
    for event_type, item in (
        (et.HUMAN_COMMITMENT_FULFILLED, fulfil_item),
        (et.HUMAN_COMMITMENT_DECLINED, decline_item),
    ):
        events = list((await db_session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.entity_id == workspace.entity_id,
                WorkspaceEvent.event_type == event_type,
            )
        )).scalars().all())
        assert len(events) == 1
        assert events[0].causation_id == item.id
        assert events[0].actor_id == user_id


# ── chat surface (plain informational message) ────────────────────────

async def test_post_human_request_chat_posts_plain_messages(db_session, monkeypatch):
    import packages.core.database as db_module
    from packages.core.workspace_chat import service as chat_service

    posted: list[dict] = []

    async def _capture_post_message(db, **kwargs):
        posted.append(kwargs)

    class _StubSession:
        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(chat_service, "post_message", _capture_post_message)
    monkeypatch.setattr(db_module, "async_session", lambda: _StubSession())

    workspace = SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid())
    await strategist_service._post_human_request_chat(workspace, [
        {
            "commitment_id": "hc_1",
            "request_kind": "decision",
            "question": "Confirm the pivot?",
            "role_required": "workspace_owner",
            "expected_by": "2026-07-26T00:00:00+00:00",
        },
        {
            "commitment_id": "hc_2",
            "request_kind": "input",
            "question": "Upload the asset pack.",
            "role_required": None,
            "expected_by": None,
        },
    ])

    assert len(posted) == 2
    for call in posted:
        assert call["message_kind"] == "hitl_request"
        assert call.get("pending_action") is None  # informational only
        assert "Human queue" in call["body"]
    assert "Confirm the pivot?" in posted[0]["body"]
    assert posted[0]["refs"] == [{"type": "human_commitment", "id": "hc_1"}]
    assert "Role: workspace_owner" in posted[0]["body"]


# ── respond endpoint ──────────────────────────────────────────────────

async def _register(client: AsyncClient, username: str) -> dict:
    from auth_helpers import register_user_and_get_token

    resp = await register_user_and_get_token(
        client,
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return {
        "headers": headers,
        "user_id": data["user_id"],
        "entity_id": me.json()["entity_id"],
    }


async def test_respond_endpoint_fulfill_decline_and_auth(client: AsyncClient):
    import packages.core.database as db_module
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token, hash_password

    owner = await _register(client, "hrr_owner")
    stranger = await _register(client, "hrr_stranger")

    ws_resp = await client.post(
        "/api/v1/workspaces", headers=owner["headers"], json={"name": "Respond WS"},
    )
    assert ws_resp.status_code in (200, 201), ws_resp.text
    workspace_id = ws_resp.json()["id"]

    # Seed: a proposal item + its commitment (role-required), a role-free
    # commitment, and a viewer member for the authority checks.
    async with db_module.async_session() as db:
        record = ProposalRecord(
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            review_id="rv_" + generate_ulid(),
            summary="hr respond test",
            status="open",
        )
        db.add(record)
        await db.flush()
        item = ProposalItemRecord(
            proposal_id=record.id,
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            item_key="hr_confirm",
            kind="human_request",
            payload={"request_kind": "decision", "question": "Confirm?"},
            action_key="workspace.proposal.human_request",
            status="executing",
            decision={"decided_by": None, "decision": "auto", "reason_code": None},
        )
        db.add(item)
        await db.flush()

        role_commitment = await open_commitment(
            db, entity_id=owner["entity_id"], workspace_id=workspace_id,
            request_kind="decision", source_kind="proposal_item", source_id=item.id,
            expected_input="Confirm?", role_required="workspace_owner",
            causation_id=item.id,
        )
        item.execution_root_id = role_commitment.id
        free_commitment = await open_commitment(
            db, entity_id=owner["entity_id"], workspace_id=workspace_id,
            request_kind="input", source_kind="chat", source_id="m_free",
            expected_input="Any info welcome.",
        )

        viewer = User(
            entity_id=owner["entity_id"],
            email="hrr_viewer@test.com",
            display_name="hrr_viewer",
            password_hash=hash_password("pass123"),
            role="member",
            status="active",
        )
        db.add(viewer)
        await db.flush()
        viewer_id = viewer.id
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, user_id=viewer_id,
            role="viewer", status="active",
            added_by=owner["user_id"], added_at=_utcnow(),
        ))
        await db.commit()
        item_id = item.id

    viewer_headers = {
        "Authorization": f"Bearer {create_access_token(viewer_id, owner['entity_id'], 'member')}"
    }
    base = f"/api/v1/workspaces/{workspace_id}/human-queue/commitments"

    # Viewer lacks approve_tasks → 403 on the role-required commitment.
    denied = await client.post(
        f"{base}/{role_commitment.id}/respond",
        headers=viewer_headers,
        json={"action": "fulfill", "response": "yes"},
    )
    assert denied.status_code == 403, denied.text

    # Cross-entity → 404. Unauthenticated → 401.
    not_found = await client.post(
        f"{base}/{role_commitment.id}/respond",
        headers=stranger["headers"],
        json={"action": "fulfill"},
    )
    assert not_found.status_code == 404
    anonymous = await client.post(
        f"{base}/{role_commitment.id}/respond", json={"action": "fulfill"},
    )
    assert anonymous.status_code == 401

    # Owner (entity admin fallback) fulfills → commitment + item resolve.
    fulfilled = await client.post(
        f"{base}/{role_commitment.id}/respond",
        headers=owner["headers"],
        json={"action": "fulfill", "response": "Yes, proceed."},
    )
    assert fulfilled.status_code == 200, fulfilled.text
    assert fulfilled.json()["status"] == "fulfilled"

    async with db_module.async_session() as db:
        commitment = await db.get(HumanCommitment, role_commitment.id)
        assert commitment.status == "fulfilled"
        assert commitment.response == {"text": "Yes, proceed."}
        assert commitment.participant_id == owner["user_id"]
        refreshed_item = await db.get(ProposalItemRecord, item_id)
        assert refreshed_item.status == "succeeded"
        assert refreshed_item.finished_at is not None

    # Double-respond → 409.
    again = await client.post(
        f"{base}/{role_commitment.id}/respond",
        headers=owner["headers"],
        json={"action": "fulfill"},
    )
    assert again.status_code == 409

    # Role-free commitment: any member may respond — the viewer declines.
    declined = await client.post(
        f"{base}/{free_commitment.id}/respond",
        headers=viewer_headers,
        json={"action": "decline", "response": "Not my area."},
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["status"] == "declined"
