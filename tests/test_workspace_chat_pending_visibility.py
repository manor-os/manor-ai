"""Unresolved action cards must reach the workspace chat, however old.

Production incident: a workspace badge counted 13 approvals while the chat
showed nothing to do. Background plans post their approval/input cards into
their own ``workspace_thread`` conversation; the main view already pinned
those cards into its first page — but the paginated endpoint appended them
*after* windowing and then truncated to ``limit``, cutting every pinned card
back off. Work sat blocked for five days behind a non-zero badge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{username}@example.com",
            "password": "test-password-123",
            "entity_name": f"{username} Co",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Content Engine"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_old_plan_thread_card_survives_main_chat_pagination(
    client: AsyncClient, db_session,
):
    """The exact prod shape: a full main conversation plus an older approval
    card filed in a plan thread. The first page must still carry the card."""
    headers = await _register(client, "ws_pending_vis")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    main_id, thread_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=main_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Conversation(
        id=thread_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="plan 01PLAN", channel="workspace", scope="workspace_thread",
        thread_ref_kind="plan", thread_ref_id="01PLAN",
    ))

    # The card is OLDER than everything in the main window — the condition
    # that made it fall off the page.
    card_id = generate_ulid()
    db_session.add(Message(
        id=card_id, conversation_id=thread_id, role="assistant",
        content="Approval needed — publish_devto", author_kind="system",
        message_kind="hitl_request",
        pending_action={"kind": "governance_approval", "step_id": "01STEP", "plan_id": "01PLAN"},
        created_at=base,
    ))
    # A main conversation deep enough to fill the page window.
    for index in range(90):
        db_session.add(Message(
            id=generate_ulid(), conversation_id=main_id, role="assistant",
            content=f"activity-{index}", author_kind="agent",
            message_kind="step_event",
            created_at=base + timedelta(minutes=index + 1),
        ))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 75},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]

    ids = {row["id"] for row in items}
    assert card_id in ids, "unresolved approval card was truncated off the first page"
    card = next(row for row in items if row["id"] == card_id)
    assert card["pending_action"]["kind"] == "governance_approval"
    # Ordering contract the client renders on: oldest → newest.
    stamps = [row["created_at"] for row in items]
    assert stamps == sorted(stamps)


@pytest.mark.asyncio
async def test_resolved_cards_are_not_pinned(client: AsyncClient, db_session):
    """Only OPEN work is force-surfaced; answered cards stay in their thread."""
    headers = await _register(client, "ws_pending_resolved")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    main_id, thread_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=main_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Conversation(
        id=thread_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="plan 02PLAN", channel="workspace", scope="workspace_thread",
        thread_ref_kind="plan", thread_ref_id="02PLAN",
    ))
    resolved_id = generate_ulid()
    db_session.add(Message(
        id=resolved_id, conversation_id=thread_id, role="assistant",
        content="Approval needed", author_kind="system",
        message_kind="hitl_request",
        pending_action={"kind": "governance_approval", "step_id": "02STEP"},
        resolved_at=base + timedelta(hours=1),
        resolution={"choice": "approve"},
        created_at=base,
    ))
    for index in range(80):
        db_session.add(Message(
            id=generate_ulid(), conversation_id=main_id, role="assistant",
            content=f"activity-{index}", author_kind="agent",
            message_kind="step_event",
            created_at=base + timedelta(minutes=index + 1),
        ))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 75},
    )
    assert resp.status_code == 200, resp.text
    assert resolved_id not in {row["id"] for row in resp.json()["items"]}


@pytest.mark.asyncio
async def test_pagination_still_walks_history(client: AsyncClient, db_session):
    """Pinning must not corrupt the cursor walk: page 2 continues cleanly and
    does not re-pin the card."""
    headers = await _register(client, "ws_pending_paging")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    main_id, thread_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=main_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Conversation(
        id=thread_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="plan 03PLAN", channel="workspace", scope="workspace_thread",
        thread_ref_kind="plan", thread_ref_id="03PLAN",
    ))
    card_id = generate_ulid()
    db_session.add(Message(
        id=card_id, conversation_id=thread_id, role="assistant",
        content="Approval needed", author_kind="system",
        message_kind="hitl_request",
        pending_action={"kind": "human_input", "step_id": "03STEP"},
        created_at=base,
    ))
    for index in range(10):
        db_session.add(Message(
            id=generate_ulid(), conversation_id=main_id, role="user",
            content=f"msg-{index}", author_kind="user", message_kind="text",
            created_at=base + timedelta(minutes=index + 1),
        ))
    await db_session.commit()

    first = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 4},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert card_id in {row["id"] for row in first_body["items"]}
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers,
        params={"limit": 4, "before": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_items = second.json()["items"]
    assert second_items, "cursor walk returned nothing"
    # Older page carries only history — the pin belongs to page one.
    assert card_id not in {row["id"] for row in second_items}
