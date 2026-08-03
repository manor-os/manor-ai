"""The chat banner and the sidebar badge must be the SAME number.

Production report: the sidebar showed 10, the banner showed 18. The user
answered a card and only the left-hand number moved.

Two independent causes, both fixed here:

1. The banner counted the cards the client happened to be holding in memory.
   That list only grows — ``unresolved_pending_messages`` pins cards from far
   outside the page window, and the moment one is answered it leaves the pinned
   set rather than coming back marked resolved, so the client's merge-by-id has
   nothing to overwrite. The badge re-counted in the DB and dropped. Hence one
   answer, two numbers. The endpoint now returns the authoritative count.

2. The badge's own ``if kind in {...} / elif / elif`` chain had no ``else``, so
   any kind added later counted as zero — ``external_message_approval``
   (approve a post/tweet) and ``needs_login`` were rendered by the chat as
   actionable cards while being invisible to the badge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
        "/api/v1/workspaces", headers=headers, json={"name": "Content Engine"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _badge_total(stats: dict) -> int:
    """What the sidebar renders — see workspaceChatActionCount in the web app."""
    return (
        (stats.get("chat_pending_actions") or 0)
        + (stats.get("proposal_actions") or 0)
        + (stats.get("failed_actions") or 0)
    )


async def _sidebar_stats(client: AsyncClient, headers: dict, workspace_id: str) -> dict:
    resp = await client.get("/api/v1/workspaces", headers=headers)
    assert resp.status_code == 200, resp.text
    row = next(w for w in resp.json() if w["id"] == workspace_id)
    return row.get("stats") or {}


async def _page(client: AsyncClient, headers: dict, workspace_id: str, **params):
    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 75, **params},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed(db_session, entity_id: str, workspace_id: str):
    main_id, thread_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=main_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Conversation(
        id=thread_id, entity_id=entity_id, workspace_id=workspace_id,
        title="plan 01PLAN", channel="workspace", scope="workspace_thread",
        thread_ref_kind="plan", thread_ref_id="01PLAN",
    ))
    return main_id, thread_id


def _card(conversation_id: str, kind: str, *, created_at: datetime, **extra) -> Message:
    return Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant",
        content=f"Action needed — {kind}", author_kind="system",
        message_kind="hitl_request",
        pending_action={"kind": kind, "step_id": generate_ulid()},
        created_at=created_at, **extra,
    )


@pytest.mark.asyncio
async def test_banner_count_equals_badge_across_every_kind(
    client: AsyncClient, db_session,
):
    """The number the chat shows and the number the sidebar shows are one number.

    Includes the two kinds the badge used to drop on the floor.
    """
    headers = await _register(client, "ws_count_kinds")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)

    kinds = [
        "human_input",
        "governance_approval",
        "workspace_operation_review",
        "needs_input",
        "needs_confirmation",
        "approve_proposals",
        "retry_strategist_review",
        "external_message_approval",   # was invisible to the badge
        "needs_login",                 # was invisible to the badge
    ]
    for index, kind in enumerate(kinds):
        db_session.add(_card(
            thread_id, kind, created_at=BASE + timedelta(minutes=index),
        ))
    await db_session.commit()

    body = await _page(client, headers, workspace_id)
    stats = await _sidebar_stats(client, headers, workspace_id)

    assert body["open_action_count"] == len(kinds)
    assert _badge_total(stats) == len(kinds)
    assert body["open_action_count"] == _badge_total(stats)
    # Every card is reachable from the chat, so the count is never a dead end.
    assert body["open_actions_complete"] is True
    assert len([r for r in body["items"] if r.get("pending_action")]) == len(kinds)


@pytest.mark.asyncio
async def test_answering_a_pinned_card_moves_both_numbers(
    client: AsyncClient, db_session,
):
    """The reported symptom: answer a card, only the sidebar moved.

    The card is older than the whole page window, so it is present ONLY because
    it was pinned. Once answered it leaves the pinned set and the page stops
    returning it — which is exactly why a client-side count cannot notice.
    """
    headers = await _register(client, "ws_count_answer")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    main_id, thread_id = await _seed(db_session, me["entity_id"], workspace_id)

    old_card = _card(thread_id, "governance_approval", created_at=BASE)
    db_session.add(old_card)
    db_session.add(_card(
        thread_id, "human_input", created_at=BASE + timedelta(minutes=1),
    ))
    # Bury the card under a full page of newer chatter.
    for index in range(90):
        db_session.add(Message(
            id=generate_ulid(), conversation_id=main_id, role="assistant",
            content=f"activity-{index}", author_kind="agent",
            message_kind="step_event",
            created_at=BASE + timedelta(hours=1, minutes=index),
        ))
    await db_session.commit()

    before = await _page(client, headers, workspace_id)
    assert before["open_action_count"] == 2
    assert old_card.id in {r["id"] for r in before["items"]}
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 2

    old_card.resolved_at = BASE + timedelta(hours=2)
    old_card.resolution = {"choice": "approve"}
    await db_session.commit()

    after = await _page(client, headers, workspace_id)
    # The page drops it — nothing comes back for the client to overwrite.
    assert old_card.id not in {r["id"] for r in after["items"]}
    # ...so the count has to come from the server, and it does.
    assert after["open_action_count"] == 1
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == 1
    # The flag that lets the client retire its stale copy.
    assert after["open_actions_complete"] is True


@pytest.mark.asyncio
async def test_open_actions_complete_is_false_when_pins_are_capped(
    client: AsyncClient, db_session,
):
    """Past the pin cap the client is NOT holding the whole open set, so it must
    not treat an absent card as answered."""
    from apps.api.routers.workspace_chat import _PINNED_ACTION_LIMIT

    headers = await _register(client, "ws_count_capped")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)

    total = _PINNED_ACTION_LIMIT + 3
    for index in range(total):
        db_session.add(_card(
            thread_id, "human_input", created_at=BASE + timedelta(minutes=index),
        ))
    await db_session.commit()

    body = await _page(client, headers, workspace_id)
    # The count is still exact even though the card list is capped.
    assert body["open_action_count"] == total
    assert body["open_actions_complete"] is False
    assert _badge_total(await _sidebar_stats(client, headers, workspace_id)) == total


@pytest.mark.asyncio
async def test_thread_view_does_not_claim_a_complete_open_set(
    client: AsyncClient, db_session,
):
    """Only the main view pins. A thread view shows one plan's messages, so it
    must not license the client to retire cards it cannot see."""
    headers = await _register(client, "ws_count_thread")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    _, thread_id = await _seed(db_session, me["entity_id"], workspace_id)
    db_session.add(_card(thread_id, "human_input", created_at=BASE))
    await db_session.commit()

    body = await _page(
        client, headers, workspace_id,
        thread_ref_kind="plan", thread_ref_id="01PLAN",
    )
    assert body["open_actions_complete"] is False
    # The workspace-wide count stays honest regardless of which view asked.
    assert body["open_action_count"] == 1
