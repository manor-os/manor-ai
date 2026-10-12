"""An agent may relay the user's verdict on a review card, never pre-empt it.

Local E2E, 2026-09-07: in the same turn that it raised a workspace-operation
review card, the master agent called ``workspace.resolve_hitl`` with
``action=approve`` on that card. It only failed because the draft was
stale. ``resolve_hitl`` exists so the agent can carry a verdict the user
gave in chat ("可以") back to the card; the card must therefore have been in
front of the user before their latest message. Approving a card the user
has not yet seen is refused; the agent is told to show the card and end its
turn.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.auth_helpers import register_user_and_get_token

from packages.core.ai.runtime.workspace_operation_actions import (
    _operation_review_payload,
    runtime_workspace_resolve_hitl_action,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services.hitl_requests import workspace_operation_pending_action_from_data


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await register_user_and_get_token(
        client,
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "SeenReviewApprovalTest123!",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed(client: AsyncClient, db_session, username: str):
    from packages.core.models.user import User

    headers = await _register(client, username)
    user = (await db_session.execute(select(User).where(User.email == f"{username}@test.com"))).scalar_one()
    ws = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Seen Review"})).json()
    draft = await client.post(
        f"/api/v1/workspaces/{ws['id']}/operation/drafts",
        headers=headers,
        json={"patches": [{"op": "rule.add", "payload": {"rule": {
            "rule_key": "review_social",
            "description": "发帖前审批",
            "rule_type": "approval_required",
            "action_patterns": ["social_post.publish"],
        }}}]},
    )
    assert draft.status_code == 200, draft.text
    draft_id = draft.json()["id"]

    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    conv_id = generate_ulid()
    card_id = generate_ulid()
    db_session.add(Conversation(
        id=conv_id, entity_id=ws["entity_id"], workspace_id=ws["id"],
        title="Workspace operation review", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Message(
        id=generate_ulid(), conversation_id=conv_id, role="user", content="把硬禁止改成审批",
        author_kind="user", message_kind="text", meta={"author_user_id": user.id}, created_at=t0,
    ))
    db_session.add(Message(
        id=card_id, conversation_id=conv_id, role="assistant", content="Review workspace operation",
        author_kind="agent", message_kind="hitl_request", created_at=t0 + timedelta(seconds=30),
        pending_action=workspace_operation_pending_action_from_data(
            _operation_review_payload(draft.json(), prompt="Review changes", content={}),
        ),
    ))
    await db_session.commit()
    return headers, user, ws, draft_id, conv_id, card_id, t0


async def _draft_status(db_session, draft_id: str) -> str:
    from packages.core.models.workspace import WorkspaceOperationDraft

    db_session.expire_all()
    row = (
        await db_session.execute(select(WorkspaceOperationDraft).where(WorkspaceOperationDraft.id == draft_id))
    ).scalar_one()
    return str(row.status)


@pytest.mark.asyncio
async def test_agent_cannot_approve_a_card_the_user_has_not_seen(client: AsyncClient, db_session):
    headers, user, ws, draft_id, conv_id, card_id, _ = await _seed(client, db_session, "ws_unseen_card")

    raw = await runtime_workspace_resolve_hitl_action(
        entity_id=ws["entity_id"], workspace_id=ws["id"], conversation_id=conv_id,
        user_id=user.id, params={"hitl_id": draft_id, "action": "approve"},
    )
    result = json.loads(raw)

    assert result["resolved"] is False
    assert result["reason"] == "review_not_seen_by_user"
    assert "end your turn" in result["message"].lower() or "show" in result["message"].lower()
    assert await _draft_status(db_session, draft_id) == "open"


@pytest.mark.asyncio
async def test_agent_can_relay_the_users_verdict_after_they_replied(client: AsyncClient, db_session):
    headers, user, ws, draft_id, conv_id, card_id, t0 = await _seed(client, db_session, "ws_seen_card")
    db_session.add(Message(
        id=generate_ulid(), conversation_id=conv_id, role="user", content="可以，应用吧",
        author_kind="user", message_kind="text", meta={"author_user_id": user.id},
        created_at=t0 + timedelta(minutes=1),
    ))
    await db_session.commit()

    raw = await runtime_workspace_resolve_hitl_action(
        entity_id=ws["entity_id"], workspace_id=ws["id"], conversation_id=conv_id,
        user_id=user.id, params={"hitl_id": draft_id, "action": "approve"},
    )
    result = json.loads(raw)

    assert result["resolved"] is True, result
    assert result["action"] == "approve"
    assert await _draft_status(db_session, draft_id) == "applied"


@pytest.mark.asyncio
async def test_agent_may_withdraw_its_own_unseen_card(client: AsyncClient, db_session):
    headers, user, ws, draft_id, conv_id, card_id, _ = await _seed(client, db_session, "ws_reject_card")

    raw = await runtime_workspace_resolve_hitl_action(
        entity_id=ws["entity_id"], workspace_id=ws["id"], conversation_id=conv_id,
        user_id=user.id, params={"hitl_id": draft_id, "action": "reject"},
    )
    result = json.loads(raw)

    assert result["resolved"] is True, result
    assert await _draft_status(db_session, draft_id) != "applied"
