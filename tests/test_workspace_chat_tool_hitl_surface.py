"""A gated tool call in workspace chat must reach the client as a card.

Production incident: a user asked the workspace agent to send an email.
``mcp__email__send_email`` gated correctly and returned a ``__hitl__``
envelope, and ``chat_service`` recorded it into
``messages.metadata->'hitl_requests'`` — the DB shows a pending ``authorize``
request against ``email.send``. The user saw nothing at all.

Two channels carry a blocked action, and workspace chat read only one:

* ``pending_action``          — written by the governance/step gate
* ``metadata.hitl_requests``  — written from a tool's ``__hitl__`` envelope

A tool-call HITL sets ``pending_action = NULL`` (only workspace-operation
review ever populates it), so the workspace message endpoints returned a row
with nothing actionable on it. The field was not merely unrendered — it was
never in the response model, so no client could have rendered it.

These tests pin the field onto both workspace message endpoints, in the same
shape ``apps/api/routers/chat.py`` already returns for the main chat surface.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message

#: The row production actually had, minus the parts nothing reads here.
EMAIL_HITL_REQUEST = {
    "id": "01KZ87N332J6V9XTSY7KTPBX0E",
    "type": "approval",
    "prompt": "Step requires operator approval before dispatching 'email.send'.",
    "action": "email.send",
    "tool": "mcp__email__send_email",
    "options": ["approve", "always_approve", "reject"],
}


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


async def _seed(db_session, client: AsyncClient, headers: dict[str, str], *, meta: dict):
    """A workspace whose main conversation holds one assistant message with
    ``meta`` — and, as in production, no ``pending_action``."""
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _workspace(client, headers)
    base = datetime(2026, 8, 4, 5, 51, 41, tzinfo=timezone.utc)

    conv_id, msg_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=conv_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Message(
        id=msg_id, conversation_id=conv_id, role="assistant",
        content="I need your approval before sending this email.",
        author_kind="agent", message_kind="hitl_request",
        meta=meta,
        pending_action=None,
        created_at=base,
    ))
    await db_session.commit()
    return workspace_id, msg_id


@pytest.mark.asyncio
async def test_tool_call_hitl_is_exposed_on_the_message_list(
    client: AsyncClient, db_session,
):
    headers = await _register(client, "ws_tool_hitl_list")
    workspace_id, msg_id = await _seed(
        db_session, client, headers, meta={"hitl_requests": [EMAIL_HITL_REQUEST]},
    )

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    row = next(item for item in resp.json() if item["id"] == msg_id)

    # The incident in one assertion: this key did not exist in the response.
    assert row["hitl_requests"], "tool-call HITL never reached the client"
    assert [req["id"] for req in row["hitl_requests"]] == [EMAIL_HITL_REQUEST["id"]]
    assert row["hitl_requests"][0]["action"] == "email.send"
    assert row["hitl_requests"][0]["options"] == ["approve", "always_approve", "reject"]
    # And it is genuinely the other channel — nothing to click via pending_action.
    assert row["pending_action"] is None


@pytest.mark.asyncio
async def test_tool_call_hitl_is_exposed_on_the_paginated_endpoint(
    client: AsyncClient, db_session,
):
    """The paginated endpoint is what the workspace UI actually loads."""
    headers = await _register(client, "ws_tool_hitl_page")
    workspace_id, msg_id = await _seed(
        db_session, client, headers, meta={"hitl_requests": [EMAIL_HITL_REQUEST]},
    )

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 75},
    )
    assert resp.status_code == 200, resp.text
    row = next(item for item in resp.json()["items"] if item["id"] == msg_id)
    assert [req["id"] for req in row["hitl_requests"]] == [EMAIL_HITL_REQUEST["id"]]


@pytest.mark.asyncio
async def test_resolved_state_survives_the_round_trip(client: AsyncClient, db_session):
    """``mark_hitl_request_resolved`` writes ``resolved``/``resolution`` back
    into the same metadata blob. If the endpoint dropped them the card would
    offer Approve again on every reload, inviting a second approval."""
    headers = await _register(client, "ws_tool_hitl_resolved")
    workspace_id, msg_id = await _seed(
        db_session, client, headers,
        meta={"hitl_requests": [
            {**EMAIL_HITL_REQUEST, "resolved": True, "resolution": "approve"},
        ]},
    )

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers,
    )
    row = next(item for item in resp.json() if item["id"] == msg_id)
    assert row["hitl_requests"][0]["resolved"] is True
    assert row["hitl_requests"][0]["resolution"] == "approve"


@pytest.mark.asyncio
async def test_concurrent_hitl_resolutions_preserve_every_card(
    client: AsyncClient,
    db_session,
):
    """Two card clicks must not lose one another in the shared JSONB blob.

    A single assistant message can carry more than one tool approval.  Each
    resolution updates ``Message.meta`` with a read/modify/write cycle, so the
    message row must serialize those updates.  Otherwise both workers can read
    the same original list and the last commit reopens the other card.
    """

    headers = await _register(client, "ws_tool_hitl_concurrent")
    first_id = generate_ulid()
    second_id = generate_ulid()
    _workspace_id, message_id = await _seed(
        db_session,
        client,
        headers,
        meta={
            "hitl_requests": [
                {**EMAIL_HITL_REQUEST, "id": first_id},
                {**EMAIL_HITL_REQUEST, "id": second_id},
            ]
        },
    )
    message = await db_session.get(Message, message_id)
    assert message is not None
    conversation_id = message.conversation_id

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    first_updated = asyncio.Event()
    allow_first_commit = asyncio.Event()

    async def resolve_first() -> None:
        from packages.core.services.hitl_requests import mark_hitl_request_resolved

        async with session_factory() as worker_db:
            assert await mark_hitl_request_resolved(
                worker_db,
                conversation_id=conversation_id,
                hitl_id=first_id,
                choice="approve",
            ) == 1
            first_updated.set()
            await allow_first_commit.wait()
            await worker_db.commit()

    async def resolve_second() -> None:
        from packages.core.services.hitl_requests import mark_hitl_request_resolved

        await first_updated.wait()
        async with session_factory() as worker_db:
            update = asyncio.create_task(
                mark_hitl_request_resolved(
                    worker_db,
                    conversation_id=conversation_id,
                    hitl_id=second_id,
                    choice="reject",
                )
            )
            try:
                # Without a row lock this finishes against the same stale
                # metadata snapshot.  With the lock it waits for the first
                # resolution to commit, then observes that committed update.
                await asyncio.wait_for(asyncio.shield(update), timeout=0.25)
            except TimeoutError:
                pass
            finally:
                allow_first_commit.set()
            assert await update == 1
            await worker_db.commit()

    await asyncio.gather(resolve_first(), resolve_second())

    async with session_factory() as check_db:
        persisted = await check_db.get(Message, message_id)
        assert persisted is not None
        requests = {
            str(item.get("id")): item
            for item in (persisted.meta or {}).get("hitl_requests") or []
            if isinstance(item, dict)
        }
        assert requests[first_id]["resolution"] == "approve"
        assert requests[second_id]["resolution"] == "reject"


@pytest.mark.asyncio
async def test_messages_without_tool_hitl_report_none(client: AsyncClient, db_session):
    """No invented cards: an ordinary message must not grow an empty list that
    the client would have to special-case."""
    headers = await _register(client, "ws_tool_hitl_absent")
    workspace_id, msg_id = await _seed(db_session, client, headers, meta={})

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers,
    )
    row = next(item for item in resp.json() if item["id"] == msg_id)
    assert row["hitl_requests"] is None


@pytest.mark.asyncio
async def test_malformed_entries_are_not_surfaced(client: AsyncClient, db_session):
    """An entry with no ``id`` cannot be resolved — the reply is keyed on it —
    so surfacing one would render a button that silently does nothing."""
    headers = await _register(client, "ws_tool_hitl_malformed")
    workspace_id, msg_id = await _seed(
        db_session, client, headers,
        meta={"hitl_requests": [{"type": "approval", "prompt": "no id"}, "garbage"]},
    )

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers,
    )
    row = next(item for item in resp.json() if item["id"] == msg_id)
    assert row["hitl_requests"] is None
