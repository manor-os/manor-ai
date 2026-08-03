"""M9 human participation data layer.

Covers:
* participant profile get-or-create, whitelisted patch + revision bump,
  and both uniqueness guards (workspace-scoped + entity-default partial)
* participant_can precedence: explicit profile authority beats the
  workspace role map beats the tenant role fallback
* commitment lifecycle (open dedupe / fulfil / decline) + ledger events
* resolve_commitments_for_step helper (fulfils, silent no-op)
* human contribution recorded on a user edit of an ai_generated task —
  and NOT on system edits or human-created tasks; diff_summary carries
  field names + length deltas only (no values)
* HumanParticipationConsolidator reports real commitment counts +
  blocking_input_waiting observations + window contributions
* /human-queue endpoint shape, ordering (blocking first, expected_by
  asc) and auth; /participants/me GET + PUT
* approve_proposals authority gate: viewer member 403, owner 200
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth_helpers import register_user_and_get_token
from packages.core.consolidators import REGISTRY, SnapshotContext
from packages.core.humans import (
    get_or_create_profile,
    list_open_commitments,
    open_commitment,
    participant_can,
    record_contribution,
    resolve_commitment,
    resolve_commitments_for_step,
    update_profile,
)
from packages.core.ledger import event_types as et
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import (
    HumanCommitment,
    HumanContribution,
    ParticipantProfile,
)
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.review import begin_review, events_in_window


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _workspace(db, entity_id: str) -> Workspace:
    workspace = Workspace(entity_id=entity_id, name="Humans WS")
    db.add(workspace)
    await db.flush()
    return workspace


async def _events_for(db, entity_id: str, event_type: str) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.entity_id == entity_id,
            WorkspaceEvent.event_type == event_type,
        )
    )).scalars().all())


# ── M9.1 profiles ──────────────────────────────────────────────────────

async def test_profile_get_or_create_and_patch_bumps_revision(db_session):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)

    profile = await get_or_create_profile(
        db_session, entity_id=entity_id, user_id=user_id, workspace_id=workspace.id,
    )
    assert profile.revision == 1
    assert profile.roles == []
    assert profile.authority == {}

    again = await get_or_create_profile(
        db_session, entity_id=entity_id, user_id=user_id, workspace_id=workspace.id,
    )
    assert again.id == profile.id

    # Entity-level default profile is a distinct row.
    default = await get_or_create_profile(
        db_session, entity_id=entity_id, user_id=user_id, workspace_id=None,
    )
    assert default.id != profile.id
    assert default.workspace_id is None

    updated = await update_profile(
        db_session, profile,
        patch={
            "roles": ["content_reviewer"],
            "authority": {"approve_tasks": False},
            "entity_id": "EVIL",             # not whitelisted — ignored
            "revision": 999,                 # not whitelisted — ignored
        },
        updated_by=user_id,
    )
    assert updated.revision == 2
    assert updated.roles == ["content_reviewer"]
    assert updated.authority == {"approve_tasks": False}
    assert updated.entity_id == entity_id
    assert updated.updated_by == user_id

    # No-op patch does not bump the revision.
    untouched = await update_profile(
        db_session, profile, patch={"nonsense": 1}, updated_by=user_id,
    )
    assert untouched.revision == 2


async def test_profile_unique_constraints(db_session):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)

    await get_or_create_profile(
        db_session, entity_id=entity_id, user_id=user_id, workspace_id=workspace.id,
    )
    await get_or_create_profile(
        db_session, entity_id=entity_id, user_id=user_id, workspace_id=None,
    )

    # Duplicate workspace-scoped row → unique (entity, user, workspace).
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ParticipantProfile(
                entity_id=entity_id, user_id=user_id, workspace_id=workspace.id,
            ))
            await db_session.flush()

    # Duplicate entity-default row → partial unique (workspace IS NULL).
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ParticipantProfile(
                entity_id=entity_id, user_id=user_id, workspace_id=None,
            ))
            await db_session.flush()


# ── M9.1 participant_can precedence ───────────────────────────────────

async def test_participant_can_precedence(db_session):
    entity_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)

    editor_id = generate_ulid()
    viewer_id = generate_ulid()
    admin_id = generate_ulid()
    outsider_id = generate_ulid()
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=workspace.id, user_id=editor_id,
            role="editor", status="active",
        ),
        WorkspaceStaff(
            workspace_id=workspace.id, user_id=viewer_id,
            role="viewer", status="active",
        ),
    ])
    await db_session.flush()

    editor = SimpleNamespace(id=editor_id, entity_id=entity_id, role="member")
    viewer = SimpleNamespace(id=viewer_id, entity_id=entity_id, role="member")
    admin = SimpleNamespace(id=admin_id, entity_id=entity_id, role="admin")
    outsider = SimpleNamespace(id=outsider_id, entity_id=entity_id, role="member")

    # Editor role map: approve_tasks + approve_goal_changes only.
    assert await participant_can(
        db_session, user=editor, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="approve_tasks",
    ) is True
    assert await participant_can(
        db_session, user=editor, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="approve_external_publish",
    ) is False

    # Explicit profile authority False overrides the editor role True.
    profile = await get_or_create_profile(
        db_session, entity_id=entity_id, user_id=editor_id, workspace_id=workspace.id,
    )
    await update_profile(
        db_session, profile,
        patch={"authority": {"approve_tasks": False}}, updated_by=admin_id,
    )
    assert await participant_can(
        db_session, user=editor, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="approve_tasks",
    ) is False

    # Viewer member is denied.
    assert await participant_can(
        db_session, user=viewer, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="approve_tasks",
    ) is False

    # Entity admin without a membership row falls back to tenant role.
    assert await participant_can(
        db_session, user=admin, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="approve_tasks",
    ) is True

    # Plain member without membership: denied.
    assert await participant_can(
        db_session, user=outsider, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="approve_tasks",
    ) is False

    # Unknown permission keys are always denied.
    assert await participant_can(
        db_session, user=admin, entity_id=entity_id,
        workspace_id=workspace.id, permission_key="launch_missiles",
    ) is False


# ── M9.2 commitments ──────────────────────────────────────────────────

async def test_commitment_lifecycle_and_ledger_events(db_session):
    entity_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)

    commitment = await open_commitment(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        request_kind="input",
        source_kind="execution_step",
        source_id="step_1",
        expected_input="Pick a thumbnail",
        blocking_execution_ids=["task_1"],
    )
    assert commitment.status == "waiting"

    opened = await _events_for(db_session, entity_id, et.HUMAN_COMMITMENT_OPENED)
    assert len(opened) == 1
    assert opened[0].idempotency_key == f"hc:{commitment.id}:opened"
    assert opened[0].source_kind == "human"

    # Re-opening the same waiting source dedupes.
    again = await open_commitment(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        request_kind="input",
        source_kind="execution_step",
        source_id="step_1",
    )
    assert again.id == commitment.id
    assert len(await _events_for(db_session, entity_id, et.HUMAN_COMMITMENT_OPENED)) == 1

    resolved = await resolve_commitment(
        db_session, commitment,
        status="fulfilled",
        response={"choice": "B"},
        participant_id="participant_x",
    )
    assert resolved.status == "fulfilled"
    assert resolved.fulfilled_at is not None
    assert resolved.response == {"choice": "B"}
    fulfilled = await _events_for(db_session, entity_id, et.HUMAN_COMMITMENT_FULFILLED)
    assert len(fulfilled) == 1
    assert fulfilled[0].actor_id == "participant_x"

    declined = await open_commitment(
        db_session,
        entity_id=entity_id, workspace_id=workspace.id,
        request_kind="review", source_kind="chat", source_id="msg_1",
    )
    await resolve_commitment(db_session, declined, status="declined")
    assert declined.fulfilled_at is None
    assert len(await _events_for(db_session, entity_id, et.HUMAN_COMMITMENT_DECLINED)) == 1

    with pytest.raises(ValueError):
        await resolve_commitment(db_session, declined, status="waiting")


async def test_resolve_commitments_for_step(db_session):
    entity_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)
    step_id = generate_ulid()

    await open_commitment(
        db_session,
        entity_id=entity_id, workspace_id=workspace.id,
        request_kind="input", source_kind="execution_step", source_id=step_id,
    )

    count = await resolve_commitments_for_step(
        db_session, step_id, {"kind": "hitl_response"},
    )
    assert count == 1
    row = (await db_session.execute(
        select(HumanCommitment).where(HumanCommitment.source_id == step_id)
    )).scalar_one()
    assert row.status == "fulfilled"
    assert row.response == {"kind": "hitl_response"}

    # Silent no-op when nothing is open.
    assert await resolve_commitments_for_step(db_session, generate_ulid()) == 0


async def test_list_open_commitments_orders_blocking_then_expected_by(db_session):
    entity_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)
    now = _utcnow()

    no_deadline = await open_commitment(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        request_kind="input", source_kind="chat", source_id="a",
    )
    blocking = await open_commitment(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        request_kind="decision", source_kind="execution_step", source_id="b",
        expected_by=now + timedelta(hours=5),
        blocking_execution_ids=["task_b"],
    )
    soon = await open_commitment(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        request_kind="review", source_kind="chat", source_id="c",
        expected_by=now + timedelta(hours=1),
    )

    ordered = await list_open_commitments(db_session, workspace.id)
    assert [c.id for c in ordered] == [blocking.id, soon.id, no_deadline.id]


# ── M9.4 contributions via update_task ────────────────────────────────

async def test_contribution_recorded_on_user_edit_of_ai_task(db_session):
    from packages.core.services.task_service import update_task

    entity_id = generate_ulid()
    user_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)
    task = Task(
        entity_id=entity_id, workspace_id=workspace.id,
        title="AI draft", description="v1",
        task_type="ai_generated", status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    await update_task(
        db_session, task.id, entity_id, user_id=user_id,
        title="AI draft (edited)", description="v2 longer text",
        details={"x": 1},  # not a tracked contribution field
    )

    contributions = list((await db_session.execute(
        select(HumanContribution).where(HumanContribution.entity_id == entity_id)
    )).scalars().all())
    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.kind == "edit"
    assert contribution.target_kind == "task"
    assert contribution.target_id == task.id
    assert set(contribution.diff_summary) == {"title", "description"}
    # Privacy: field names + length deltas only — never values.
    for field_diff in contribution.diff_summary.values():
        assert set(field_diff) == {"changed", "len_delta"}

    events = await _events_for(db_session, entity_id, et.HUMAN_CONTRIBUTION_RECORDED)
    assert len(events) == 1
    # Participant profile auto-created for the editing user.
    profile = (await db_session.execute(
        select(ParticipantProfile).where(
            ParticipantProfile.entity_id == entity_id,
            ParticipantProfile.user_id == user_id,
        )
    )).scalar_one()
    assert contribution.participant_id == profile.id


async def test_no_contribution_for_system_edits_or_human_tasks(db_session):
    from packages.core.services.task_service import update_task

    entity_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)
    ai_task = Task(
        entity_id=entity_id, workspace_id=workspace.id,
        title="AI task", task_type="ai_generated", status="pending",
    )
    human_task = Task(
        entity_id=entity_id, workspace_id=workspace.id,
        title="Manual task", task_type="general", status="pending",
    )
    db_session.add_all([ai_task, human_task])
    await db_session.flush()

    # System edit (no user_id) of an AI task → no contribution.
    await update_task(db_session, ai_task.id, entity_id, title="AI task v2")
    # User edit of a human-created task → no contribution.
    await update_task(
        db_session, human_task.id, entity_id,
        user_id=generate_ulid(), title="Manual task v2",
    )

    contributions = list((await db_session.execute(
        select(HumanContribution).where(HumanContribution.entity_id == entity_id)
    )).scalars().all())
    assert contributions == []


# ── M4.5/M9 consolidator ──────────────────────────────────────────────

async def test_consolidator_reports_commitments_and_contributions(db_session):
    entity_id = generate_ulid()
    workspace = await _workspace(db_session, entity_id)

    blocking = await open_commitment(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        request_kind="input", source_kind="execution_step", source_id="step_x",
        blocking_execution_ids=["task_x"],
    )
    await open_commitment(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        request_kind="review", source_kind="chat", source_id="msg_x",
    )
    fulfilled = await open_commitment(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        request_kind="input", source_kind="chat", source_id="msg_y",
    )
    await resolve_commitment(db_session, fulfilled, status="fulfilled")
    await record_contribution(
        db_session,
        entity_id=entity_id, workspace_id=workspace.id,
        participant_id=generate_ulid(),
        kind="edit", target_kind="task", target_id="task_x",
        diff_summary={"title": {"changed": True, "len_delta": 3}},
    )
    await asyncio.sleep(0.002)  # ULID ordering before the review watermark

    review = await begin_review(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        trigger="scheduled",
    )
    ctx = SnapshotContext(
        review=review,
        workspace=workspace,
        events=await events_in_window(db_session, review),
    )
    report = await REGISTRY["human_participation"].run(db_session, ctx)

    assert report.metrics["active_human_commitments"] == 2
    assert report.metrics["blocking_commitments"] == 1
    assert report.metrics["contributions_since_last_review"] == 1
    assert report.coverage.sources["open_commitments"] == 2
    assert "human_commitments" not in report.coverage.sources

    waiting = [o for o in report.observations if o.type == "blocking_input_waiting"]
    assert len(waiting) == 1
    assert waiting[0].evidence_refs == [f"human_commitment:{blocking.id}"]


# ── M9.5 API — human queue + own profile ──────────────────────────────

async def _register(client: AsyncClient, username: str) -> dict:
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


async def test_human_queue_endpoint_shape_ordering_auth(client: AsyncClient):
    import packages.core.database as db_module

    owner = await _register(client, "hq_owner")
    stranger = await _register(client, "hq_stranger")

    ws_resp = await client.post(
        "/api/v1/workspaces", headers=owner["headers"], json={"name": "Queue WS"},
    )
    assert ws_resp.status_code in (200, 201), ws_resp.text
    workspace_id = ws_resp.json()["id"]

    now = _utcnow()
    async with db_module.async_session() as db:
        non_blocking = await open_commitment(
            db, entity_id=owner["entity_id"], workspace_id=workspace_id,
            request_kind="input", source_kind="chat", source_id="m1",
        )
        blocking = await open_commitment(
            db, entity_id=owner["entity_id"], workspace_id=workspace_id,
            request_kind="decision", source_kind="execution_step", source_id="s1",
            expected_by=now + timedelta(hours=8),
            blocking_execution_ids=["task_1"],
        )
        db.add(HitlRequest(
            entity_id=owner["entity_id"], workspace_id=workspace_id,
            action_key="external.publish", origin_kind="step",
            status="pending", dedup_key=f"dk_hq_{workspace_id}",
        ))
        db.add(Task(
            entity_id=owner["entity_id"], workspace_id=workspace_id,
            title="Assigned to me", status="pending",
            assignee_id=owner["user_id"],
        ))
        db.add(Task(
            entity_id=owner["entity_id"], workspace_id=workspace_id,
            title="Done long ago", status="completed",
            assignee_id=owner["user_id"],
        ))
        await db.commit()

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/human-queue", headers=owner["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "commitments", "approvals", "information_requests", "assigned_tasks",
    }

    assert [c["id"] for c in body["commitments"]] == [blocking.id, non_blocking.id]
    assert body["commitments"][0]["blocking"] is True
    assert body["commitments"][0]["blocking_execution_ids"] == ["task_1"]
    assert body["approvals"][0]["action_key"] == "external.publish"
    assert [t["title"] for t in body["assigned_tasks"]] == ["Assigned to me"]

    # Cross-entity access → 404; unauthenticated → 401.
    denied = await client.get(
        f"/api/v1/workspaces/{workspace_id}/human-queue", headers=stranger["headers"],
    )
    assert denied.status_code == 404
    anonymous = await client.get(f"/api/v1/workspaces/{workspace_id}/human-queue")
    assert anonymous.status_code == 401


async def test_participants_me_get_and_put(client: AsyncClient):
    owner = await _register(client, "pp_owner")
    ws_resp = await client.post(
        "/api/v1/workspaces", headers=owner["headers"], json={"name": "Profile WS"},
    )
    workspace_id = ws_resp.json()["id"]

    got = await client.get(
        f"/api/v1/workspaces/{workspace_id}/participants/me", headers=owner["headers"],
    )
    assert got.status_code == 200, got.text
    assert got.json()["revision"] == 1
    assert got.json()["workspace_id"] == workspace_id

    put = await client.put(
        f"/api/v1/workspaces/{workspace_id}/participants/me",
        headers=owner["headers"],
        json={
            "roles": ["workspace_owner"],
            "authority": {"approve_tasks": True},
            "availability": {"timezone": "America/Los_Angeles"},
        },
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["revision"] == 2
    assert body["roles"] == ["workspace_owner"]
    assert body["authority"] == {"approve_tasks": True}

    bad = await client.put(
        f"/api/v1/workspaces/{workspace_id}/participants/me",
        headers=owner["headers"],
        json={"authority": {"launch_missiles": True}},
    )
    assert bad.status_code == 400


# ── M9.1 authority gate on proposal approve ───────────────────────────

async def test_proposal_approve_authority_gate(client: AsyncClient):
    import packages.core.database as db_module
    from packages.core.services.auth_service import create_access_token, hash_password
    from packages.core.models.user import User

    owner = await _register(client, "gate_owner")

    ws_resp = await client.post(
        "/api/v1/workspaces", headers=owner["headers"], json={"name": "Gate WS"},
    )
    workspace_id = ws_resp.json()["id"]

    async with db_module.async_session() as db:
        viewer = User(
            entity_id=owner["entity_id"],
            email="gate_viewer@test.com",
            display_name="gate_viewer",
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

        conversation = Conversation(
            id=generate_ulid(),
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            scope="workspace_main",
        )
        message = Message(
            id=generate_ulid(),
            conversation_id=conversation.id,
            role="assistant",
            content="Proposal card",
            author_kind="system",
            message_kind="proposal",
            pending_action={
                "kind": "approve_proposals",
                "review_id": generate_ulid(),
                "task_ids": [],
            },
        )
        db.add_all([conversation, message])
        await db.commit()
        message_id = message.id

    viewer_headers = {
        "Authorization": f"Bearer {create_access_token(viewer_id, owner['entity_id'], 'member')}"
    }

    denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
        headers=viewer_headers,
        json={"choice": "approve"},
    )
    assert denied.status_code == 403, denied.text
    assert "approve_tasks" in denied.json()["detail"]

    # The card stays actionable — the owner (entity admin fallback) succeeds.
    approved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
        headers=owner["headers"],
        json={"choice": "approve"},
    )
    assert approved.status_code == 200, approved.text
