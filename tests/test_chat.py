"""E2E tests: chat SSE streaming, conversations, messages."""

import json
import pytest
from types import SimpleNamespace
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routers.chat import _resolve_chat_workspace_scope, _visible_chat_messages
from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from packages.core.services.auth_service import create_access_token, hash_password
from packages.core.services.hitl_requests import user_visible_hitl_action_text


async def _auth(client: AsyncClient, username: str = "chatuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_chat_stream_sse(client: AsyncClient):
    """Send a message → receive SSE stream with text_delta events."""
    headers = await _auth(client)

    # POST /chat/stream returns SSE
    resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Hello AI",
        },
        timeout=10.0,
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # Parse SSE events
    events = []
    for line in resp.text.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
            events.append({"event": event_type, "data": data})

    # Should have: stream_start, text_delta(s), stream_end
    event_types = [e["event"] for e in events]
    assert "stream_start" in event_types
    assert "text_delta" in event_types
    assert "stream_end" in event_types

    # stream_end should contain conversation_id
    end_event = [e for e in events if e["event"] == "stream_end"][0]
    assert "conversation_id" in end_event["data"]


@pytest.mark.asyncio
async def test_chat_creates_conversation(client: AsyncClient):
    """Chat stream creates a conversation and saves messages."""
    headers = await _auth(client)

    # Send a message (creates conversation)
    stream_resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "First message",
        },
    )
    stream_message_id = None
    event_type = ""
    for line in stream_resp.text.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:") :].strip())
            if event_type == "stream_end":
                stream_message_id = data.get("message_id")

    # List conversations
    resp = await client.get("/api/v1/chat/conversations", headers=headers)
    assert resp.status_code == 200
    convs = resp.json()
    assert len(convs) >= 1

    # Get messages for the conversation
    conv_id = convs[0]["id"]
    msg_resp = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers)
    assert msg_resp.status_code == 200
    msgs = msg_resp.json()
    # Should have at least the user message
    assert len(msgs) >= 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "First message"
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["id"] == stream_message_id
    assert "still working" not in (assistant_msgs[-1]["content"] or "")


@pytest.mark.asyncio
async def test_workspace_chat_user_message_records_author(client: AsyncClient):
    """Regression: a user message sent via /chat/stream into a workspace must
    persist its author_user_id. Without it the message reads back with no
    author, and the workspace chat UI renders every member's message as the
    viewer's own ("you")."""
    headers = await _auth(client, "ws_author_member")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    workspace = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Author Attribution WS"},
        )
    ).json()
    ws_id = workspace["id"]

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Hello from a workspace member",
            "workspace_context": "true",
            "workspace_id": ws_id,
        },
        timeout=15.0,
    )
    assert resp.status_code == 200
    # Drain the SSE stream so the user message is committed.
    _ = resp.text

    msgs = (
        await client.get(
            f"/api/v1/workspaces/{ws_id}/chat/messages",
            headers=headers,
        )
    ).json()
    user_msgs = [m for m in msgs if m["author_kind"] == "user"]
    assert user_msgs, "expected the user message to be persisted in workspace chat"
    assert all(m["author_user_id"] == me["id"] for m in user_msgs)


@pytest.mark.asyncio
async def test_workspace_chat_resolution_records_approver(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Resolving a workspace action must record AND surface who approved it,
    both in the resolve response and when re-listing messages."""
    from packages.core.models.task import Conversation, Message

    headers = await _auth(client, "ws_approver")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Approver Attribution WS"},
        )
    ).json()
    ws_id = ws["id"]

    conv_id = generate_ulid()
    msg_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            workspace_id=ws_id,
            title="Workspace main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=msg_id,
            conversation_id=conv_id,
            role="assistant",
            content="Approve this action?",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={"kind": "approval"},
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{msg_id}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_by_user_id"] == me["id"]
    assert body["resolved_by_user_name"]

    msgs = (
        await client.get(
            f"/api/v1/workspaces/{ws_id}/chat/messages",
            headers=headers,
        )
    ).json()
    resolved = next(m for m in msgs if m["id"] == msg_id)
    assert resolved["resolved_by_user_id"] == me["id"]
    assert resolved["resolved_by_user_name"]


def test_hitl_action_text_is_user_visible():
    assert user_visible_hitl_action_text("approve") == "Approved the requested action."
    assert user_visible_hitl_action_text("always_approve") == "Approved the requested action."
    assert user_visible_hitl_action_text("reject") == "Rejected the requested action."


def test_visible_chat_messages_hides_stale_stream_placeholder():
    rows = [
        SimpleNamespace(role="user", content="hello", meta={}),
        SimpleNamespace(
            role="assistant",
            content="Codex is still working on that...",
            meta={"stream_status": "running"},
        ),
        SimpleNamespace(role="assistant", content="done", meta={}),
    ]

    visible = _visible_chat_messages(rows)

    assert [m.content for m in visible] == ["hello", "done"]


@pytest.mark.asyncio
async def test_chat_reuse_conversation(client: AsyncClient):
    """Send multiple messages to same conversation."""
    headers = await _auth(client)

    # First message — creates conversation
    resp1 = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Message 1",
        },
    )
    # Extract conversation_id from stream_start event
    conv_id = None
    for line in resp1.text.split("\n"):
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[len("data:") :].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break
    assert conv_id

    # Second message — reuse conversation
    await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Message 2",
            "conversation_id": conv_id,
        },
    )

    # Should have 2 user messages in one conversation
    msg_resp = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers)
    msgs = msg_resp.json()
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 2


@pytest.mark.asyncio
async def test_chat_no_auth(client: AsyncClient):
    resp = await client.post("/api/v1/chat/stream", data={"message": "test"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_conversation_isolation(client: AsyncClient):
    """User A can't see User B's conversations."""
    headers_a = await _auth(client, "chat_a")
    headers_b = await _auth(client, "chat_b")

    # A chats
    resp = await client.post("/api/v1/chat/stream", headers=headers_a, data={"message": "A's message"})
    conv_id = None
    for line in resp.text.split("\n"):
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[len("data:") :].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break

    # B can't see A's messages
    resp2 = await client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=headers_b)
    assert resp2.status_code == 404

    # B's conversation list is empty
    resp3 = await client.get("/api/v1/chat/conversations", headers=headers_b)
    assert len(resp3.json()) == 0


@pytest.mark.asyncio
async def test_personal_conversation_is_private_within_same_entity(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """A teammate in the same organization cannot read or append to a personal chat."""
    headers_a = await _auth(client, "same_entity_a")
    me_resp = await client.get("/api/v1/auth/me", headers=headers_a)
    me = me_resp.json()

    user_b = User(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        email="same_entity_b@test.com",
        display_name="same_entity_b",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(user_b)
    await db_session.commit()
    headers_b = {"Authorization": f"Bearer {create_access_token(user_b.id, user_b.entity_id, user_b.role)}"}

    resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers_a,
        data={"message": "A private same-entity message"},
    )
    conv_id = None
    for line in resp.text.split("\n"):
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[len("data:") :].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break
    assert conv_id

    read_resp = await client.get(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        headers=headers_b,
    )
    assert read_resp.status_code == 404

    append_resp = await client.post(
        "/api/v1/chat/stream",
        headers=headers_b,
        data={"message": "B should not append", "conversation_id": conv_id},
    )
    assert append_resp.status_code == 404

    list_resp = await client.get("/api/v1/chat/conversations", headers=headers_b)
    assert all(conv["id"] != conv_id for conv in list_resp.json())


@pytest.mark.asyncio
async def test_workspace_chat_rejects_mismatched_conversation_scope(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.task import Conversation
    from packages.core.services.conversation_lifecycle import get_or_create_conversation

    headers = await _auth(client, "workspace_scope_guard")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws_a = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Scope A"},
        )
    ).json()
    ws_b = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Scope B"},
        )
    ).json()

    conv_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            workspace_id=ws_a["id"],
            title="Workspace A main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    await db_session.commit()

    with pytest.raises(PermissionError):
        await get_or_create_conversation(
            db_session,
            me["entity_id"],
            me["id"],
            conversation_id=conv_id,
            workspace_id=ws_b["id"],
        )

    with pytest.raises(PermissionError):
        await get_or_create_conversation(
            db_session,
            me["entity_id"],
            me["id"],
            conversation_id=conv_id,
            workspace_id=ws_a["id"],
            thread_ref_kind="task",
            thread_ref_id=generate_ulid(),
        )


@pytest.mark.asyncio
async def test_chat_workspace_scope_requires_workspace_chat_source(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.task import Conversation

    headers = await _auth(client, "workspace_source_guard")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Workspace Source Guard"},
        )
    ).json()
    user = SimpleNamespace(
        id=me["id"],
        entity_id=me["entity_id"],
        role=me.get("role"),
    )

    assert await _resolve_chat_workspace_scope(
        db_session,
        user,
        conversation_id=None,
        workspace_id=ws["id"],
        thread_ref_kind=None,
        thread_ref_id=None,
        workspace_context=False,
    ) == (ws["id"], None, None)

    assert await _resolve_chat_workspace_scope(
        db_session,
        user,
        conversation_id=None,
        workspace_id=ws["id"],
        thread_ref_kind=None,
        thread_ref_id=None,
        workspace_context=True,
    ) == (ws["id"], None, None)

    conv_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=me["entity_id"],
            workspace_id=ws["id"],
            title="Workspace main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    await db_session.commit()

    assert await _resolve_chat_workspace_scope(
        db_session,
        user,
        conversation_id=conv_id,
        workspace_id=None,
        thread_ref_kind=None,
        thread_ref_id=None,
        workspace_context=False,
    ) == (ws["id"], None, None)


@pytest.mark.asyncio
async def test_members_only_workspace_chat_requires_workspace_read_access(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from datetime import UTC, datetime

    from apps.api.routers import workspace_chat
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workspace import WorkspaceStaff

    monkeypatch.setattr(
        workspace_chat,
        "_schedule_workspace_chat_processing",
        lambda **_kwargs: None,
    )

    owner_headers = await _auth(client, "workspace_chat_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace = (
        await client.post(
            "/api/v1/workspaces",
            headers=owner_headers,
            json={"name": "Private Workspace Chat"},
        )
    ).json()
    workspace_id = workspace["id"]
    assert workspace["settings"]["access_mode"] == "members_only"

    outsider = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="workspace_chat_outsider@test.com",
        display_name="workspace_chat_outsider",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    conv_id = generate_ulid()
    channel_conv_id = generate_ulid()
    action_message_id = generate_ulid()
    db_session.add(outsider)
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            title="Workspace main",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Conversation(
            id=channel_conv_id,
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            title="Private customer channel",
            channel="webchat",
            scope="channel",
        )
    )
    db_session.add(
        Message(
            id=generate_ulid(),
            conversation_id=conv_id,
            role="user",
            content="workspace-only note",
            author_kind="user",
            message_kind="text",
        )
    )
    db_session.add(
        Message(
            id=action_message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Review this private action",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={"kind": "approve_proposals", "review_id": "review_private"},
        )
    )
    await db_session.commit()
    outsider_headers = {
        "Authorization": f"Bearer {create_access_token(outsider.id, outsider.entity_id, outsider.role)}"
    }

    assert (
        await client.get(
            f"/api/v1/workspaces/{workspace_id}/chat/messages",
            headers=outsider_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages",
            headers=outsider_headers,
            json={"body": "should not enter"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{action_message_id}/resolve",
            headers=outsider_headers,
            json={"choice": "approve"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{action_message_id}/feedback",
            headers=outsider_headers,
            json={"rating": "up"},
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/chat/conversations/{conv_id}/messages",
            headers=outsider_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/chat/stream",
            headers=outsider_headers,
            data={
                "message": "open private workspace",
                "workspace_context": "true",
                "workspace_id": workspace_id,
            },
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/chat/stream",
            headers=outsider_headers,
            data={"message": "append private workspace", "conversation_id": conv_id},
        )
    ).status_code == 404
    hidden_history = await client.get("/api/v1/chat/conversations", headers=outsider_headers)
    assert all(row["id"] != channel_conv_id for row in hidden_history.json())

    db_session.add(
        WorkspaceStaff(
            workspace_id=workspace_id,
            user_id=outsider.id,
            role="viewer",
            added_by=owner["id"],
            added_at=datetime.now(UTC),
            status="active",
        )
    )
    await db_session.commit()

    allowed_list = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=outsider_headers,
    )
    assert allowed_list.status_code == 200
    allowed_post = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=outsider_headers,
        json={"body": "viewer can participate after membership is granted"},
    )
    assert allowed_post.status_code == 201
    visible_history = await client.get("/api/v1/chat/conversations", headers=outsider_headers)
    assert any(row["id"] == channel_conv_id for row in visible_history.json())


@pytest.mark.asyncio
async def test_workspace_chat_rejects_deleted_workspace_runtime(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.services.conversation_lifecycle import get_or_create_conversation

    headers = await _auth(client, "workspace_deleted_guard")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Deleted Runtime"},
        )
    ).json()
    delete = await client.delete(f"/api/v1/workspaces/{ws['id']}", headers=headers)
    assert delete.status_code == 204

    with pytest.raises(PermissionError):
        await get_or_create_conversation(
            db_session,
            me["entity_id"],
            me["id"],
            workspace_id=ws["id"],
        )
