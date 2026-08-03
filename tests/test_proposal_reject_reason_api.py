"""M9.3/M8 chat-card API — reject reason codes + always-approve standing grant.

The proposal card resolve endpoint
(``POST /workspaces/{id}/chat/messages/{mid}/resolve``) now:

* threads an optional ``payload.reason_code`` (+ ``note``) into
  ``reject_proposal`` so the item decision carries the machine-readable
  vocabulary — unknown / system-only codes fall back to ``OTHER``;
* on ``always_approve`` with ``strategist_review_v2`` enabled writes the
  auditable standing grant (``workspace.proposal.task`` in the workspace
  governance policy) in addition to the legacy workspace boolean.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.proposals.constants import TASK_ACTION_KEY
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"

_ONE_TASK = {
    "summary": "Draft docs.",
    "tasks": [
        {
            "task_key": "draft_docs",
            "title": "Draft source docs",
            "description": "Write the source docs for this cycle.",
            "owner_service_key": "ops",
            "priority": 3,
            "basis": {"report_refs": ["goal"], "evidence_refs": []},
            "deliverables": [{
                "name": "docs",
                "kind": "value",
                "shape": "TextResult",
                "acceptance": "docs drafted",
                "usage": "operator review",
            }],
        },
    ],
}


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str, str]:
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
    return headers, data["user_id"], me.json()["entity_id"]


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


def _fake_completion(payload: dict = _ONE_TASK):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


async def _seed_review_card(client: AsyncClient, monkeypatch, username: str) -> dict:
    """Register an owner, run a v2 review in their entity, and post the
    proposal chat card the resolve endpoint will act on."""
    import packages.core.database as dbmod

    headers, user_id, entity_id = await _register_owner(client, username)

    async with dbmod.async_session() as db:
        workspace = Workspace(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Reject Reason WS",
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
        agent = Agent(
            id=generate_ulid(), entity_id=entity_id, name="Ops Agent", status="active",
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

        await _set_flag(db, True)
        event = await record_event(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            event_type=et.EXECUTION_COMPLETED,
            source_kind="task",
            source_id=f"task_{username}",
            idempotency_key=f"reject-reason:{workspace.id}:1",
        )
        assert event is not None
        await db.commit()
        await asyncio.sleep(0.002)

        monkeypatch.setattr(
            "packages.core.strategist.prompt.runtime_execute_strategist_completion",
            _fake_completion(),
        )

        async def _noop_post(*args, **kwargs):
            return None
        monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop_post)
        monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)

        result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
        assert not result.get("skipped")
        assert result["approval_outcome"] == "needs_human"

        conversation = Conversation(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace.id,
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
                "review_id": result["review_id"],
                "task_ids": result["task_ids"],
            },
        )
        db.add_all([conversation, message])
        await db.commit()

    return {
        "headers": headers,
        "entity_id": entity_id,
        "workspace_id": workspace.id,
        "review_id": result["review_id"],
        "task_ids": result["task_ids"],
        "message_id": message.id,
    }


async def _item_decisions(review_id: str) -> list[dict]:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        items = list((await db.execute(
            select(ProposalItemRecord)
            .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
            .where(ProposalRecord.review_id == review_id)
        )).scalars().all())
        assert items, f"no proposal items for review {review_id}"
        return [
            {"status": item.status, "decision": dict(item.decision or {})}
            for item in items
        ]


@pytest.mark.asyncio
async def test_reject_with_reason_code_lands_in_item_decision(client: AsyncClient, monkeypatch):
    ctx = await _seed_review_card(client, monkeypatch, "reject_reason_owner")

    resp = await client.post(
        f"/api/v1/workspaces/{ctx['workspace_id']}/chat/messages/{ctx['message_id']}/resolve",
        headers=ctx["headers"],
        json={
            "choice": "reject",
            "note": "Too costly this quarter",
            "payload": {"reason_code": "TOO_EXPENSIVE"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution"]["payload"]["reason_code"] == "TOO_EXPENSIVE"

    for row in await _item_decisions(ctx["review_id"]):
        assert row["status"] == "rejected"
        assert row["decision"]["reason_code"] == "TOO_EXPENSIVE"
        assert row["decision"]["comment"] == "Too costly this quarter"


@pytest.mark.asyncio
async def test_reject_unknown_or_system_reason_code_falls_back_other(client: AsyncClient, monkeypatch):
    # Unknown vocabulary must not 500 — it degrades to OTHER. The same
    # guard covers system-only codes (POLICY_BLOCKED etc.), which are
    # valid REASON_CODES but never user-offerable.
    ctx = await _seed_review_card(client, monkeypatch, "reject_bogus_owner")

    resp = await client.post(
        f"/api/v1/workspaces/{ctx['workspace_id']}/chat/messages/{ctx['message_id']}/resolve",
        headers=ctx["headers"],
        json={"choice": "reject", "payload": {"reason_code": "POLICY_BLOCKED"}},
    )
    assert resp.status_code == 200, resp.text

    for row in await _item_decisions(ctx["review_id"]):
        assert row["status"] == "rejected"
        assert row["decision"]["reason_code"] == "OTHER"


@pytest.mark.asyncio
async def test_always_approve_with_flag_writes_boolean_and_standing_grant(client: AsyncClient, monkeypatch):
    import packages.core.database as dbmod

    ctx = await _seed_review_card(client, monkeypatch, "always_grant_owner")

    resp = await client.post(
        f"/api/v1/workspaces/{ctx['workspace_id']}/chat/messages/{ctx['message_id']}/resolve",
        headers=ctx["headers"],
        json={"choice": "always_approve"},
    )
    assert resp.status_code == 200, resp.text

    async with dbmod.async_session() as db:
        workspace = await db.get(Workspace, ctx["workspace_id"])
        # Legacy boolean kept for compat with the flag-off path.
        assert workspace.settings["strategist"]["auto_approve_proposals"] is True

        policy_row = (await db.execute(
            select(GovernancePolicy).where(
                GovernancePolicy.workspace_id == ctx["workspace_id"]
            )
        )).scalar_one()
        assert TASK_ACTION_KEY in policy_row.policy["auto_approve_actions"]

        revisions = list((await db.execute(
            select(GovernanceRevision).where(
                GovernanceRevision.workspace_id == ctx["workspace_id"]
            )
        )).scalars().all())
        assert any(
            (r.change_summary or "").startswith("always-approve action:")
            for r in revisions
        )

    for row in await _item_decisions(ctx["review_id"]):
        assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_always_approve_with_flag_off_skips_standing_grant(client: AsyncClient, monkeypatch):
    import packages.core.database as dbmod

    ctx = await _seed_review_card(client, monkeypatch, "always_legacy_owner")

    # The review ran with the flag on (so a card exists), but the entity
    # has the flag off by the time the operator clicks Always approve.
    async with dbmod.async_session() as db:
        await _set_flag(db, False)

    resp = await client.post(
        f"/api/v1/workspaces/{ctx['workspace_id']}/chat/messages/{ctx['message_id']}/resolve",
        headers=ctx["headers"],
        json={"choice": "always_approve"},
    )
    assert resp.status_code == 200, resp.text

    async with dbmod.async_session() as db:
        workspace = await db.get(Workspace, ctx["workspace_id"])
        assert workspace.settings["strategist"]["auto_approve_proposals"] is True

        policy_row = (await db.execute(
            select(GovernancePolicy).where(
                GovernancePolicy.workspace_id == ctx["workspace_id"]
            )
        )).scalar_one_or_none()
        granted = (policy_row.policy.get("auto_approve_actions") if policy_row else []) or []
        assert TASK_ACTION_KEY not in granted
