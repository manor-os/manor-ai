"""M14 Human Participation view — GET /workspaces/{id}/human-participation.

Covers:
* queue shape + ordering (blocking first, then expected_by ascending) and
  that it is workspace-wide, not caller-scoped;
* blocking entries resolving blocked execution ids to task titles;
* participants built from ParticipantProfile ∪ WorkspaceStaff, with
  declared roles/capabilities, availability, and an open-commitment COUNT;
* the M9.6 privacy boundary: no per-person latency / response-time /
  efficiency / performance key anywhere in the response, and
  contributions exposing changed FIELD NAMES only (no values/diffs);
* decisions read off the proposal_item_approved/rejected ledger events
  with the reason code from the item's stored decision;
* non-member → 404 (sibling-router convention).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from auth_helpers import register_user_and_get_token
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.participant import (
    HumanCommitment,
    HumanContribution,
    ParticipantProfile,
)
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.review_run import ReviewRun
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.workspace import Workspace, WorkspaceStaff

pytestmark = pytest.mark.asyncio

# Vocabulary that must never appear as a key in this response — the M4.5 /
# M9.6 red line (invariant 8: human data never becomes a hidden scorecard).
FORBIDDEN_SUBSTRINGS = (
    "response_time",
    "efficiency",
    "performance",
    "latency",
)


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, dict]:
    resp = await register_user_and_get_token(
        client,
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()


async def _seed(client: AsyncClient, username: str) -> dict:
    import packages.core.database as dbmod

    headers, me = await _register_owner(client, username)
    entity_id, owner_id = me["entity_id"], me["id"]
    now = datetime.now(timezone.utc)

    async with dbmod.async_session() as db:
        ws = Workspace(
            id=generate_ulid(), entity_id=entity_id,
            name="Humans WS", status="active", settings={},
        )
        other_ws = Workspace(
            id=generate_ulid(), entity_id=entity_id,
            name="Other WS", status="active", settings={},
        )
        db.add_all([ws, other_ws])

        # A second member with a staff row but no participant profile.
        teammate = User(
            id=generate_ulid(), entity_id=entity_id,
            email=f"{username}_mate@test.com", display_name="Ada Teammate",
            password_hash="x", role="member",
        )
        db.add(teammate)
        db.add_all([
            WorkspaceStaff(
                workspace_id=ws.id, user_id=owner_id, role="owner", status="active",
            ),
            WorkspaceStaff(
                workspace_id=ws.id, user_id=teammate.id, role="editor",
                status="active",
            ),
        ])
        # Owner has a declared profile; the teammate deliberately does not.
        db.add(ParticipantProfile(
            entity_id=entity_id, user_id=owner_id, workspace_id=ws.id,
            roles=["workspace_owner", "content_reviewer"],
            declared_capabilities=["brand_voice"],
            authority={"approve_tasks": True},
            availability={
                "timezone": "Europe/Berlin",
                "out_of_office": True,
                "available_windows": [{"day": "mon"}],
            },
            capacity_preferences={"max_open_requests": 3},
        ))

        blocked_task = Task(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            title="Blocked on brand input", status="in_progress",
        )
        db.add(blocked_task)

        # Queue: blocking first, then expected_by ascending. Seeded out of
        # order on purpose.
        late = HumanCommitment(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            request_kind="review", source_kind="proposal_item",
            source_id="pi_late", status="waiting",
            requested_at=now - timedelta(hours=5),
            expected_by=now + timedelta(days=2),
            participant_id=owner_id,
        )
        soon = HumanCommitment(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            request_kind="input", source_kind="chat", source_id="msg_1",
            status="waiting",
            requested_at=now - timedelta(hours=4),
            expected_by=now + timedelta(hours=6),
            role_required="content_reviewer",
        )
        blocking = HumanCommitment(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            request_kind="decision", source_kind="execution_step",
            source_id="step_9", status="waiting",
            requested_at=now - timedelta(hours=3),
            expected_by=now + timedelta(days=5),
            participant_id=owner_id,
            blocking_execution_ids=[blocked_task.id, "exec_unknown"],
        )
        resolved = HumanCommitment(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            request_kind="input", source_kind="chat", source_id="msg_old",
            status="fulfilled", requested_at=now - timedelta(days=1),
        )
        # Another workspace's waiting commitment must not leak in.
        elsewhere = HumanCommitment(
            id=generate_ulid(), entity_id=entity_id, workspace_id=other_ws.id,
            request_kind="input", source_kind="chat", source_id="msg_other",
            status="waiting", requested_at=now,
        )
        db.add_all([late, soon, blocking, resolved, elsewhere])

        # Contributions — newest first, field names only.
        db.add_all([
            HumanContribution(
                id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
                participant_id=owner_id, kind="edit", target_kind="task",
                target_id=blocked_task.id,
                diff_summary={
                    "title": {"changed": True, "len_delta": 12},
                    "description": {"changed": True, "len_delta": -40},
                },
                created_at=now - timedelta(hours=2),
            ),
            HumanContribution(
                id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
                participant_id=owner_id, kind="upload", target_kind="document",
                target_id="doc_1", diff_summary=None,
                created_at=now - timedelta(hours=9),
            ),
        ])

        # Decisions: one approved + one rejected proposal item.
        review = ReviewRun(
            entity_id=entity_id, workspace_id=ws.id,
            trigger_kind="scheduled", status="succeeded",
            watermark_start=None, watermark_end=generate_ulid(),
            window_start=now - timedelta(hours=1), window_end=now,
            briefing={}, completed_at=now,
        )
        db.add(review)
        await db.flush()
        proposal = ProposalRecord(
            entity_id=entity_id, workspace_id=ws.id, review_id=review.id,
            summary="Cycle proposal", status="resolved",
        )
        db.add(proposal)
        await db.flush()
        approved_item = ProposalItemRecord(
            proposal_id=proposal.id, entity_id=entity_id, workspace_id=ws.id,
            item_key="draft_docs", kind="task", status="succeeded",
            payload={"title": "Draft docs"},
            basis={"report_refs": ["goal"], "evidence_refs": ["evt:1"]},
            risk_level="low", action_key="workspace.proposal.task",
            decision={"decision": "approved", "decided_by": owner_id},
            decided_at=now,
        )
        rejected_item = ProposalItemRecord(
            proposal_id=proposal.id, entity_id=entity_id, workspace_id=ws.id,
            item_key="spam_task", kind="task", status="rejected",
            payload={"title": "Spam"},
            basis={"report_refs": ["execution"], "evidence_refs": []},
            risk_level="low", action_key="workspace.proposal.task",
            decision={"decision": "rejected", "reason_code": "NOT_RELEVANT"},
            decided_at=now,
        )
        db.add_all([approved_item, rejected_item])
        await db.flush()

        for item, event_type in (
            (approved_item, et.PROPOSAL_ITEM_APPROVED),
            (rejected_item, et.PROPOSAL_ITEM_REJECTED),
        ):
            await record_event(
                db,
                entity_id=entity_id, workspace_id=ws.id,
                event_type=event_type, source_kind="proposal",
                source_id=item.id, status=item.status,
                actor_kind="user", actor_id=owner_id,
                payload={"title": (item.payload or {}).get("title")},
                occurred_at=now,
                idempotency_key=f"item:{item.id}:{event_type}",
            )
        await db.commit()

        return {
            "headers": headers,
            "entity_id": entity_id,
            "owner_id": owner_id,
            "teammate_id": teammate.id,
            "workspace_id": ws.id,
            "other_workspace_id": other_ws.id,
            "blocked_task_id": blocked_task.id,
            "commitments": {
                "blocking": blocking.id, "soon": soon.id, "late": late.id,
                "resolved": resolved.id,
            },
            "items": {
                "approved": approved_item.id, "rejected": rejected_item.id,
            },
        }


async def _get_view(client: AsyncClient, seed: dict) -> dict:
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/human-participation",
        headers=seed["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── queue + blocking ──────────────────────────────────────────────────

async def test_queue_is_workspace_wide_and_ordered(client: AsyncClient):
    seed = await _seed(client, "hpv_queue")
    body = await _get_view(client, seed)
    assert set(body) == {
        "queue", "blocking", "participants", "recent_contributions", "decisions",
    }

    # Blocking first, then expected_by ascending. Fulfilled + other-workspace
    # commitments never appear.
    assert [c["id"] for c in body["queue"]] == [
        seed["commitments"]["blocking"],
        seed["commitments"]["soon"],
        seed["commitments"]["late"],
    ]

    head = body["queue"][0]
    assert head["blocking"] is True
    assert head["status"] == "waiting"
    assert head["participant_id"] == seed["owner_id"]
    assert head["role_required"] is None
    assert head["expected_by"]
    assert head["age_hours"] >= 3.0  # request age, not a person's latency

    role_scoped = body["queue"][1]
    assert role_scoped["participant_id"] is None
    assert role_scoped["role_required"] == "content_reviewer"
    assert role_scoped["blocking"] is False


async def test_blocking_resolves_task_titles(client: AsyncClient):
    seed = await _seed(client, "hpv_blocking")
    body = await _get_view(client, seed)

    assert len(body["blocking"]) == 1
    entry = body["blocking"][0]
    assert entry["commitment_id"] == seed["commitments"]["blocking"]
    assert entry["request_kind"] == "decision"
    assert entry["execution_ids"] == [seed["blocked_task_id"], "exec_unknown"]

    blocked = {b["id"]: b for b in entry["blocked"]}
    task_ref = blocked[seed["blocked_task_id"]]
    assert task_ref["kind"] == "task"
    assert task_ref["title"] == "Blocked on brand input"
    assert task_ref["status"] == "in_progress"
    # An id that is not a Task stays an opaque execution reference.
    assert blocked["exec_unknown"]["kind"] == "execution"
    assert blocked["exec_unknown"]["title"] is None


# ── participants ──────────────────────────────────────────────────────

async def test_participants_from_profiles_and_staff(client: AsyncClient):
    seed = await _seed(client, "hpv_people")
    body = await _get_view(client, seed)

    people = {p["user_id"]: p for p in body["participants"]}
    assert set(people) == {seed["owner_id"], seed["teammate_id"]}

    owner = people[seed["owner_id"]]
    assert owner["roles"] == ["workspace_owner", "content_reviewer"]
    assert owner["declared_capabilities"] == ["brand_voice"]
    assert owner["availability"] == {
        "timezone": "Europe/Berlin", "out_of_office": True,
    }
    # Two waiting commitments are addressed to the owner by name.
    assert owner["open_commitments_count"] == 2

    # Staff row without a profile: role falls back to the workspace role.
    teammate = people[seed["teammate_id"]]
    assert teammate["display_name"] == "Ada Teammate"
    assert teammate["roles"] == ["editor"]
    assert teammate["declared_capabilities"] == []
    assert teammate["availability"] == {"timezone": None, "out_of_office": False}
    assert teammate["open_commitments_count"] == 0


# ── privacy (M9.6 / invariant 8) ──────────────────────────────────────

def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys |= _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _all_keys(child)
    return keys


async def test_no_per_person_performance_metric_anywhere(client: AsyncClient):
    seed = await _seed(client, "hpv_privacy")
    body = await _get_view(client, seed)

    offenders = [
        key for key in _all_keys(body)
        if any(bad in key.lower() for bad in FORBIDDEN_SUBSTRINGS)
    ]
    assert offenders == [], f"per-person performance keys leaked: {offenders}"

    # Participants carry declared facts + a count, nothing else.
    for person in body["participants"]:
        assert set(person) == {
            "user_id", "display_name", "roles", "declared_capabilities",
            "availability", "open_commitments_count",
        }
        assert isinstance(person["open_commitments_count"], int)

    # Nor is a participant identity attached to any decision or contribution.
    serialized = json.dumps(
        {"decisions": body["decisions"],
         "recent_contributions": body["recent_contributions"]}
    )
    assert seed["owner_id"] not in serialized


async def test_contributions_expose_field_names_only(client: AsyncClient):
    seed = await _seed(client, "hpv_contrib")
    body = await _get_view(client, seed)

    contributions = body["recent_contributions"]
    assert [c["kind"] for c in contributions] == ["edit", "upload"]  # newest first

    edit = contributions[0]
    assert set(edit) == {
        "kind", "target_kind", "target_id", "fields_changed", "created_at",
    }
    assert edit["target_kind"] == "task"
    assert edit["target_id"] == seed["blocked_task_id"]
    assert edit["fields_changed"] == ["description", "title"]
    # No values, no deltas, no diff bodies.
    serialized = json.dumps(edit)
    assert "len_delta" not in serialized
    assert '"changed"' not in serialized

    assert contributions[1]["fields_changed"] == []


# ── decisions ─────────────────────────────────────────────────────────

async def test_decisions_carry_kind_and_reason_code(client: AsyncClient):
    seed = await _seed(client, "hpv_decisions")
    body = await _get_view(client, seed)

    decisions = {d["item_id"]: d for d in body["decisions"]}
    assert set(decisions) == {seed["items"]["approved"], seed["items"]["rejected"]}

    approved = decisions[seed["items"]["approved"]]
    assert approved["decision"] == "approved"
    assert approved["kind"] == "task"
    assert approved["reason_code"] is None
    assert approved["decided_at"]

    rejected = decisions[seed["items"]["rejected"]]
    assert rejected["decision"] == "rejected"
    assert rejected["reason_code"] == "NOT_RELEVANT"


# ── auth ──────────────────────────────────────────────────────────────

async def test_non_member_gets_404(client: AsyncClient):
    seed = await _seed(client, "hpv_owner")
    outsider_headers, _ = await _register_owner(client, "hpv_outsider")

    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/human-participation",
        headers=outsider_headers,
    )
    assert resp.status_code == 404
