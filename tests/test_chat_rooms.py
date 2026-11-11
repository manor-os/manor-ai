"""Cross-entity human chat never grants Workspace or company membership."""
from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from time import perf_counter
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.chat_room import ChatRoom, ChatRoomAgentBinding, ChatRoomAgentRun, ChatRoomFile, ChatRoomInvite, ChatRoomMember, ChatRoomMessage, ChatRoomMessageReaction
from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.notification import Notification
from packages.core.models.permission import Capability, GrantStatus, ResourceGrant, ResourceType, SubjectType
from packages.core.models.staff import Staff
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.user_session import UserSessionLease, UserSessionLog
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.constants.agents import MANOR_AGENT_ID
from tests.approval_settings_test_utils import (
    initialize_test_approval_owners, room_reply_rule, set_workspace_test_approval,
)
from packages.core.services.upload_security import UploadSecurityError


async def _register(client: AsyncClient, name: str) -> tuple[dict[str, str], dict]:
    response = await client.post("/api/v1/auth/register", json={
        "email": f"{name}@example.com", "password": "test-password-123",
        "entity_name": f"{name} Co",
    })
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()


async def _connect_group_mailbox(client: AsyncClient, headers: dict, address: str) -> str:
    response = await client.post("/api/v1/integrations", headers=headers, json={
        "provider": "email", "credentials": {
            "smtp_host": "smtp.example.com", "smtp_port": 587,
            "username": address, "password": "test-app-password", "from_address": address,
        },
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]



async def _set_workspace_room_reply(db_session, owner, workspace_id, room_id, *, allowed):
    from packages.core.governance.approval_policy import ApprovalScope
    from packages.core.governance.approval_policy_service import read_approval_settings
    from packages.core.services.room_agent_runtime import room_agent_reply_policy

    workspace = await db_session.get(Workspace, workspace_id)
    await initialize_test_approval_owners(db_session, workspace, user_id=owner["id"])
    actor = await db_session.get(User, owner["id"])
    current = await read_approval_settings(db_session, ApprovalScope(
        kind="workspace", id=workspace_id, entity_id=owner["entity_id"]), actor)
    rule = room_reply_rule(room_id)
    rules = [{key: item[key] for key in ("id", "effect", "selector")}
        for item in current["saved_rules"] if item["editable"] and item["selector"] != rule["selector"]]
    if allowed:
        rules.append(rule)
    await set_workspace_test_approval(db_session, entity_id=owner["entity_id"],
        workspace_id=workspace_id, actor_id=owner["id"],
        mode=current["policy"]["mode"], user_rules=rules)
    decision = await room_agent_reply_policy(db_session, workspace_id=workspace_id, room_id=room_id)
    assert decision.allowed is allowed
    # The grant covers this audience only, never every room using this Agent.
    other = await room_agent_reply_policy(db_session, workspace_id=workspace_id, room_id=generate_ulid())
    assert other.pause_for_hitl and not other.allowed
    await db_session.commit()


async def _allow_workspace_room_reply(db_session, owner: dict, workspace_id: str, room_id: str) -> None:
    await _set_workspace_room_reply(db_session, owner, workspace_id, room_id, allowed=True)


async def _require_workspace_reply_review(db_session, owner, workspace):
    await initialize_test_approval_owners(db_session, workspace, user_id=owner["id"])
    await set_workspace_test_approval(db_session, entity_id=owner["entity_id"],
        workspace_id=workspace.id, actor_id=owner["id"], mode="confirm_changes")
    await db_session.commit()


@pytest.mark.asyncio
async def test_group_usage_remembers_joined_left_and_closed_rooms(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "feature_usage_owner")
    guest_headers, guest = await _register(client, "feature_usage_guest")
    usage_url = "/api/v1/chat/rooms/group-usage"
    assert (await client.get(usage_url, headers=owner_headers)).json() == {"used": False}
    assert (await client.get(usage_url, headers=guest_headers)).json() == {"used": False}

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Feature usage", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    assert (await client.get(usage_url, headers=owner_headers)).json() == {"used": True}
    # Being invited is not usage until the recipient joins.
    assert (await client.get(usage_url, headers=guest_headers)).json() == {"used": False}

    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    assert (await client.get(usage_url, headers=guest_headers)).json() == {"used": True}
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}/membership", headers=guest_headers)).status_code == 204
    assert (await client.get(usage_url, headers=guest_headers)).json() == {"used": True}
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)).status_code == 204
    assert (await client.get(usage_url, headers=owner_headers)).json() == {"used": True}


@pytest.mark.asyncio
async def test_group_reply_and_other_sender_reactions_are_scoped(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "reply_reaction_owner")
    guest_headers, guest = await _register(client, "reply_reaction_guest")
    outsider_headers, _ = await _register(client, "reply_reaction_outsider")
    owner_avatar = f"/api/v1/fs/{owner['entity_id']}/avatars/owner.png"
    guest_avatar = f"/api/v1/fs/{guest['entity_id']}/avatars/guest-team-photo.png"
    owner_user = (await db_session.execute(select(User).where(User.id == owner["id"]))).scalar_one()
    guest_user = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    owner_user.avatar_url = owner_avatar
    # The guest's only photo is on their team page; the old generated placeholder never wins.
    guest_user.avatar_url = (
        "https://api.dicebear.com/9.x/initials/svg?seed=0a1b2c3d"
        "&backgroundColor=0f766e,14b8a6,0d9488,0ea5e9,6366f1,8b5cf6"
    )
    db_session.add(Staff(
        entity_id=guest["entity_id"], user_id=guest["id"], name="Guest", avatar_url=guest_avatar,
    ))
    await db_session.commit()
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Replies", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    room_view = await client.get(f"/api/v1/chat/rooms/{room_id}", headers=guest_headers)
    assert room_view.status_code == 200, room_view.text
    assert {member["user_id"]: member["avatar_url"] for member in room_view.json()["members"]} == {
        owner["id"]: owner_avatar, guest["id"]: guest_avatar,
    }
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=outsider_headers)).status_code == 404
    base = f"/api/v1/chat/rooms/{room_id}/messages"
    original = await client.post(base, headers=owner_headers, json={"content": "First message"})
    assert original.status_code == 201, original.text
    assert original.json()["sender_user_avatar_url"] == owner_avatar
    target_id = original.json()["id"]
    replied = await client.post(base, headers=guest_headers, json={
        "content": "Specific answer", "reply_to_message_id": target_id,
    })
    assert replied.status_code == 201, replied.text
    assert replied.json()["reply_to_message_id"] == target_id
    assert replied.json()["sender_user_avatar_url"] == guest_avatar
    assert (await client.post(base, headers=guest_headers, json={
        "content": "Wrong target", "reply_to_message_id": generate_ulid(),
    })).status_code == 404
    reaction_url = f"{base}/{target_id}/reactions"
    assert (await client.put(reaction_url, headers=owner_headers, json={"emoji": "👍"})).status_code == 403
    assert (await client.put(reaction_url, headers=outsider_headers, json={"emoji": "👍"})).status_code == 404
    assert (await client.put(reaction_url, headers=guest_headers, json={"emoji": "not an emoji"})).status_code == 422
    for _ in range(2):
        assert (await client.put(reaction_url, headers=guest_headers, json={"emoji": "👍"})).status_code == 204
    visible = (await client.get(base, headers=guest_headers)).json()
    original_view = next(item for item in visible if item["id"] == target_id)
    reply_view = next(item for item in visible if item["id"] == replied.json()["id"])
    assert original_view["sender_user_avatar_url"] == owner_avatar
    assert reply_view["sender_user_avatar_url"] == guest_avatar
    assert original_view["reactions"] == [{"emoji": "👍", "count": 1, "reacted_by_me": True}]
    assert reply_view["reply_to"] == {
        "id": target_id, "sender_name": owner.get("display_name") or owner["email"], "content": "First message",
    }
    assert (await client.delete(f"{reaction_url}/%F0%9F%91%8D", headers=guest_headers)).status_code == 204
    for emoji in ("🥳", "👩🏽‍💻"):
        assert (await client.put(reaction_url, headers=guest_headers, json={"emoji": emoji})).status_code == 204
        reacted = (await client.get(base, headers=guest_headers)).json()
        assert next(item for item in reacted if item["id"] == target_id)["reactions"] == [
            {"emoji": emoji, "count": 1, "reacted_by_me": True},
        ]
        assert (await client.delete(f"{reaction_url}/{emoji}", headers=guest_headers)).status_code == 204
    assert (await db_session.execute(select(ChatRoomMessageReaction))).scalars().all() == []


@pytest.mark.asyncio
async def test_group_message_retraction_redacts_history_search_and_agent_source(
    client: AsyncClient, db_session,
):
    from packages.core.services.room_agent_runtime import _admit_run

    owner_headers, owner = await _register(client, "retract_owner")
    guest_headers, guest = await _register(client, "retract_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Retract", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    room_id = created.json()["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    base = f"/api/v1/chat/rooms/{room_id}/messages"
    sent = await client.post(base, headers=owner_headers, json={"content": "private retraction marker"})
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["id"]
    reply = await client.post(base, headers=guest_headers, json={
        "content": "response", "reply_to_message_id": message_id,
    })
    assert reply.status_code == 201, reply.text
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=message_id,
        binding_id=created.json()["agents"][0]["id"], agent_name="Manor AI",
        audience_fingerprint="test", status="pending",
    )
    db_session.add(run)
    await db_session.commit()
    assert (await client.delete(f"{base}/{message_id}", headers=guest_headers)).status_code == 403
    assert (await client.delete(f"{base}/{message_id}", headers=owner_headers)).status_code == 204
    assert (await client.delete(f"{base}/{message_id}", headers=owner_headers)).status_code == 204
    history = (await client.get(base, headers=guest_headers)).json()
    tombstone = next(item for item in history if item["id"] == message_id)
    assert tombstone["deleted_at"] and tombstone["content"] == ""
    assert tombstone["agent_runs"] == [] and tombstone["attachments"] == []
    assert next(item for item in history if item["id"] == reply.json()["id"])["reply_to"]["content"] == "Message removed"
    assert (await client.get(base, headers=guest_headers, params={"q": "private retraction marker"})).json() == []
    retractions = await client.post(f"/api/v1/chat/rooms/{room_id}/message-retractions", headers=guest_headers, json={
        "message_ids": [message_id, reply.json()["id"]],
    })
    assert retractions.status_code == 200 and retractions.json() == {"message_ids": [message_id]}
    assert (await client.put(f"{base}/{message_id}/reactions", headers=guest_headers, json={"emoji": "👍"})).status_code == 404
    assert (await client.post(base, headers=guest_headers, json={"content": "again", "reply_to_message_id": message_id})).status_code == 404
    await db_session.refresh(run)
    assert run.status == "interrupted" and run.error_code == "source_retracted"
    turn, reason = await _admit_run(db_session, run)
    assert turn is None and reason == "source_unavailable"
    stored = (await db_session.execute(select(ChatRoomMessage).where(ChatRoomMessage.id == message_id))).scalar_one()
    assert stored.content == "private retraction marker" and stored.deleted_by_user_id == owner["id"]
    audit = (await db_session.execute(select(AuditLog).where(
        AuditLog.action == "chat_room.message.retracted", AuditLog.resource_id == message_id,
    ))).scalar_one()
    assert audit.user_id == owner["id"]


@pytest.mark.asyncio
async def test_group_mention_reply_notifications_are_targeted_deduplicated_and_mutable(
    client: AsyncClient, db_session,
):
    owner_headers, owner = await _register(client, "notify_owner")
    guest_headers, guest = await _register(client, "notify_guest")
    outsider_headers, outsider = await _register(client, "notify_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Alerts", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    room_id = created.json()["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    base = f"/api/v1/chat/rooms/{room_id}"
    assert (await client.patch(f"{base}/notifications", headers=outsider_headers, json={"muted": True})).status_code == 404
    first = await client.post(f"{base}/messages", headers=guest_headers, json={"content": "first"})
    assert first.status_code == 201
    mentioned = await client.post(f"{base}/messages", headers=owner_headers, json={
        "content": "@guest reply", "mentioned_user_ids": [guest["id"]],
        "reply_to_message_id": first.json()["id"],
    })
    assert mentioned.status_code == 201, mentioned.text
    notifications = (await db_session.execute(select(Notification).where(
        Notification.idempotency_key == f"chat-room-message:{mentioned.json()['id']}:{guest['id']}",
    ))).scalars().all()
    assert len(notifications) == 1 and notifications[0].type == "chat_room_reply"
    assert notifications[0].content and "@guest" not in notifications[0].content
    assert (await client.patch(f"{base}/notifications", headers=guest_headers, json={"muted": True})).json() == {"muted": True}
    assert (await client.get(base, headers=guest_headers)).json()["notifications_muted"] is True
    muted = await client.post(f"{base}/messages", headers=owner_headers, json={
        "content": "@guest again", "mentioned_user_ids": [guest["id"]],
    })
    assert muted.status_code == 201, muted.text
    guest_rooms = (await client.get("/api/v1/chat/rooms", headers=guest_headers)).json()
    assert next(item for item in guest_rooms if item["id"] == room_id)["unread_count"] >= 2
    assert (await db_session.execute(select(Notification).where(
        Notification.idempotency_key == f"chat-room-message:{muted.json()['id']}:{guest['id']}",
    ))).scalar_one_or_none() is None
    assert (await client.delete(f"{base}/messages/{mentioned.json()['id']}", headers=owner_headers)).status_code == 204
    await db_session.refresh(notifications[0])
    assert notifications[0].dispatch_status == "canceled"
    assert (await db_session.execute(select(Notification).where(
        Notification.user_id == outsider["id"], Notification.type.in_(("chat_room_reply", "chat_room_mention")),
    ))).scalars().all() == []


@pytest.mark.asyncio
async def test_member_can_leave_group_but_owner_must_transfer_or_close(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "leave_owner")
    guest_headers, guest = await _register(client, "leave_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Leave", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    room_id = created.json()["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    endpoint = f"/api/v1/chat/rooms/{room_id}/membership"
    assert (await client.delete(endpoint, headers=owner_headers)).status_code == 409
    assert (await client.delete(endpoint, headers=guest_headers)).status_code == 204
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=guest_headers)).status_code == 404


@pytest.mark.asyncio
async def test_member_new_image_attachment_can_be_mentioned_to_ai_only_in_same_message(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)
    async def hold_run(_run_id: str) -> None:
        pass
    async def clean_image(_payload, **_kwargs) -> str:
        return "image/png"
    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    monkeypatch.setattr(chat_rooms, "inspect_upload_content", clean_image)
    owner_headers, _ = await _register(client, "image_ai_owner")
    member_headers, member = await _register(client, "image_ai_member")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Image AI", "owner_scope": "personal",
        "recipient_email": member["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=member["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/sfoAAAAASUVORK5CYII=")
    uploaded = await client.post(f"/api/v1/chat/rooms/{room_id}/files", headers=member_headers, data={
        "content": "@Manor AI what is in this picture?",
        "mentioned_agent_binding_ids": json.dumps([binding_id]),
    }, files={"file": ("picture.png", png, "image/png")})
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert len(body["agent_runs"]) == 1
    run = (await db_session.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == body["agent_runs"][0]["id"],
    ))).scalar_one()
    run.status = "running"
    await db_session.commit()
    turn, reason = await room_agent_runtime._admit_run(db_session, run)
    assert reason is None and turn is not None
    assert turn.requester_is_manager is False
    assert turn.source_file_id == body["attachments"][0]["id"]
    await db_session.commit()
    multimodal = await room_agent_runtime._current_attachment_input(turn, "current message")
    assert isinstance(multimodal, list)
    assert multimodal[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_member_ai_attachment_read_cannot_expand_to_other_group_files(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime, room_file_read_tools

    _isolate_room_agent_worker(db_session, monkeypatch)
    monkeypatch.setattr(
        room_file_read_tools, "async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    async def hold_run(_run_id: str) -> None:
        pass
    async def clean_text(_payload, **_kwargs) -> str:
        return "text/plain"
    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    monkeypatch.setattr(chat_rooms, "inspect_upload_content", clean_text)
    owner_headers, _ = await _register(client, "text_ai_owner")
    member_headers, member = await _register(client, "text_ai_member")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Text AI", "owner_scope": "personal",
        "recipient_email": member["email"],
    })
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=member["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    enabled = await client.patch(f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=owner_headers,
                                 json={"enabled": True, "expected_revision": 0})
    assert enabled.status_code == 200, enabled.text
    upload_url = f"/api/v1/chat/rooms/{room_id}/files"
    other = await client.post(upload_url, headers=owner_headers, files={
        "file": ("private-to-current-turn.txt", b"another sender's file", "text/plain"),
    })
    assert other.status_code == 201, other.text
    own = await client.post(upload_url, headers=member_headers, data={
        "content": "@Manor AI summarize the attached text",
        "mentioned_agent_binding_ids": json.dumps([binding_id]),
    }, files={"file": ("current.txt", b"only this current attachment", "text/plain")})
    assert own.status_code == 201, own.text
    run = (await db_session.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == own.json()["agent_runs"][0]["id"],
    ))).scalar_one()
    run.status = "running"
    await db_session.commit()
    turn, reason = await room_agent_runtime._admit_run(db_session, run)
    assert reason is None and turn is not None
    await db_session.commit()
    context = await room_agent_runtime._current_attachment_input(turn, "current message")
    assert isinstance(context, str)
    assert "only this current attachment" in context
    assert "another sender's file" not in context
    denied = await room_file_read_tools.read_current_source_attachment(
        room_id=room_id, run_id=run.id, source_message_id=own.json()["id"],
        file_id=other.json()["attachments"][0]["id"],
    )
    assert denied == {"error": "room_file_unavailable"}
    own_file_id = own.json()["attachments"][0]["id"]
    source = await room_file_read_tools.read_room_file_source_bytes(
        room_id=room_id, run_id=run.id,
        expected_execution_revision=turn.file_tool_execution_revision,
        file_id=own_file_id, source_message_id=own.json()["id"],
    )
    assert source is not None and source[1] == b"only this current attachment"
    assert await room_file_read_tools.read_room_file_source_bytes(
        room_id=room_id, run_id=run.id,
        expected_execution_revision=turn.file_tool_execution_revision,
        file_id=other.json()["attachments"][0]["id"],
        source_message_id=own.json()["id"],
    ) is None
    assert await room_file_read_tools.find_named_room_file_source_ids(
        room_id=room_id, run_id=run.id,
        expected_execution_revision=turn.file_tool_execution_revision,
        current_message="请参考 current.txt",
    ) == (own_file_id,)


async def _knowledge_file(db_session, tmp_path, monkeypatch, owner: dict, *, name="source.txt", content=b"Knowledge bytes"):
    from packages.core.services import entity_fs

    monkeypatch.setattr(
        entity_fs, "get_settings",
        lambda: SimpleNamespace(MANOR_FS_ROOT=str(tmp_path), MANOR_FS_ENABLED=True, DEPLOYMENT_MODE="oss"),
    )
    source = tmp_path / owner["entity_id"] / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    document = Document(
        id=generate_ulid(), entity_id=owner["entity_id"], owner_id=owner["id"],
        name=name, fs_path=name, file_size=len(content), mime_type="text/plain",
        visibility="private", classification="internal", quarantine_status="clean",
        vector_status="ready",
    )
    db_session.add(document)
    await db_session.commit()
    return document, source


def _isolate_room_agent_worker(db_session, monkeypatch) -> None:
    from packages.core.services import room_agent_runtime

    monkeypatch.setattr(
        room_agent_runtime, "async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )


@pytest.mark.asyncio
async def test_only_active_group_owner_can_change_revisioned_ai_execution_setting(
    client: AsyncClient, db_session,
):
    owner_headers, owner = await _register(client, "ai_execution_owner")
    admin_headers, admin = await _register(client, "ai_execution_admin")
    member_headers, member = await _register(client, "ai_execution_member")
    outsider_headers, _ = await _register(client, "ai_execution_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Tool boundary", "owner_scope": "entity",
        "recipient_email": admin["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    settings_url = f"/api/v1/chat/rooms/{room_id}/ai-execution"
    now = datetime.now(timezone.utc)
    for account in (admin, member):
        db_session.add(ChatRoomMember(
            id=generate_ulid(), room_id=room_id, user_id=account["id"],
            role="member", status="active", visible_from=now,
        ))
    await db_session.commit()
    promoted = await client.patch(
        f"/api/v1/chat/rooms/{room_id}/members/{admin['id']}",
        headers=owner_headers, json={"role": "admin"},
    )
    assert promoted.status_code == 200, promoted.text

    room = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)).json()
    assert room["ai_tool_execution_enabled"] is False
    assert room["ai_file_actions_direct_enabled"] is False
    assert room["ai_tool_execution_revision"] == 0
    assert room["can_manage_ai_execution"] is True
    owner_list = (await client.get("/api/v1/chat/rooms", headers=owner_headers)).json()
    assert next(item for item in owner_list if item["id"] == room_id)["can_manage_ai_execution"] is True
    for headers in (admin_headers, member_headers):
        detail = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=headers)).json()
        assert detail["can_manage_ai_execution"] is False
        denied = await client.patch(settings_url, headers=headers, json={
            "enabled": True, "expected_revision": 0,
        })
        assert denied.status_code == 403, denied.text
    assert (await client.patch(settings_url, headers=outsider_headers, json={
        "enabled": True, "expected_revision": 0,
    })).status_code == 404

    body = {"enabled": True, "expected_revision": 0}
    first, duplicate = await asyncio.gather(
        client.patch(settings_url, headers=owner_headers, json=body),
        client.patch(settings_url, headers=owner_headers, json=body),
    )
    assert first.status_code == duplicate.status_code == 200, (first.text, duplicate.text)
    assert first.json()["ai_tool_execution_revision"] == duplicate.json()["ai_tool_execution_revision"] == 1
    assert first.json()["ai_tool_execution_enabled"] is True
    assert first.json()["ai_file_actions_direct_enabled"] is True
    assert (await client.patch(settings_url, headers=owner_headers, json={
        "enabled": False, "expected_revision": 0,
    })).status_code == 409
    disabled = await client.patch(settings_url, headers=owner_headers, json={
        "enabled": False, "expected_revision": 1,
    })
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["ai_tool_execution_enabled"] is False
    assert disabled.json()["ai_file_actions_direct_enabled"] is False
    assert disabled.json()["ai_tool_execution_revision"] == 2
    assert (await client.patch(settings_url, headers=owner_headers, json={
        "enabled": "true", "expected_revision": 2,
    })).status_code == 422
    stored = (await db_session.execute(select(ChatRoom).where(ChatRoom.id == room_id))).scalar_one()
    await db_session.refresh(stored)
    assert stored.ai_tool_execution_enabled is False
    assert stored.ai_file_actions_direct_enabled is False
    assert stored.ai_tool_execution_revision == 2
    audit = (await db_session.execute(select(AuditLog).where(
        AuditLog.resource_type == "chat_room", AuditLog.resource_id == room_id,
    ))).scalars().all()
    assert len(audit) == 2
    assert all(entry.user_id == owner["id"] and entry.entity_id == owner["entity_id"] for entry in audit)
    assert {entry.details["revision"]: entry.details["enabled"] for entry in audit} == {1: True, 2: False}
    assert all(entry.created_at is not None for entry in audit)

    dm = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "dm", "recipient_email": member["email"],
    })
    assert dm.status_code == 201, dm.text
    assert dm.json()["can_manage_ai_execution"] is False
    assert (await client.patch(f"/api/v1/chat/rooms/{dm.json()['id']}/ai-execution",
                               headers=owner_headers, json={
                                   "enabled": True, "expected_revision": 0,
                               })).status_code == 403
    dm_row = (await db_session.execute(select(ChatRoom).where(ChatRoom.id == dm.json()["id"]))).scalar_one()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            dm_row.ai_tool_execution_enabled = True
            await db_session.flush()
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)).status_code == 204
    assert (await client.patch(settings_url, headers=owner_headers, json={
        "enabled": True, "expected_revision": 2,
    })).status_code == 404
    assert all(item["id"] != room_id for item in (await client.get(
        "/api/v1/chat/rooms", headers=owner_headers,
    )).json())


@pytest.mark.asyncio
async def test_existing_on_group_requires_explicit_owner_opt_in_for_direct_file_actions(
    client: AsyncClient, db_session,
):
    headers, _ = await _register(client, "legacy_file_actions_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Existing On group", "owner_scope": "personal",
        "recipient_email": "pending@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    room = (await db_session.execute(select(ChatRoom).where(ChatRoom.id == room_id))).scalar_one()
    room.ai_tool_execution_enabled = True
    room.ai_tool_execution_revision = 1
    await db_session.commit()
    before = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=headers)).json()
    assert before["ai_tool_execution_enabled"] is True
    assert before["ai_file_actions_direct_enabled"] is False
    url = f"/api/v1/chat/rooms/{room_id}/ai-execution"
    enabled = await client.patch(url, headers=headers, json={
        "enabled": True, "expected_revision": 1,
    })
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["ai_file_actions_direct_enabled"] is True
    assert enabled.json()["ai_tool_execution_revision"] == 2
    duplicate = await client.patch(url, headers=headers, json={
        "enabled": True, "expected_revision": 1,
    })
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["ai_tool_execution_revision"] == 2
    assert (await client.patch(url, headers=headers, json={
        "enabled": False, "expected_revision": 1,
    })).status_code == 409


@pytest.mark.asyncio
async def test_personal_group_ai_execution_audit_is_not_assigned_to_company(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "personal_ai_execution_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Personal scope", "owner_scope": "personal",
        "recipient_email": "personal_ai_guest@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    enabled = await client.patch(f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=owner_headers, json={
        "enabled": True, "expected_revision": 0,
    })
    assert enabled.status_code == 200, enabled.text
    audit = (await db_session.execute(select(AuditLog).where(
        AuditLog.resource_type == "chat_room", AuditLog.resource_id == room_id,
    ))).scalar_one()
    assert audit.entity_id is None
    assert audit.user_id == owner["id"]
    assert audit.details["owner_scope"] == "personal"


@pytest.mark.asyncio
@pytest.mark.parametrize("inline_limit", [100 * 1024 * 1024, 4])
async def test_group_can_copy_own_knowledge_as_room_only_file(client: AsyncClient, db_session, tmp_path, monkeypatch, inline_limit):
    monkeypatch.setattr("apps.api.routers.chat_rooms.ROOM_FILE_INLINE_MAX_BYTES", inline_limit)
    owner_headers, owner = await _register(client, "knowledge_room_owner")
    guest_headers, guest = await _register(client, "knowledge_room_guest")
    guest_avatar = f"/api/v1/fs/{guest['entity_id']}/avatars/guest.png"
    guest_user = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    guest_user.avatar_url = guest_avatar
    await db_session.commit()
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Knowledge copy", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    endpoint = f"/api/v1/chat/rooms/{room_id}/files/from-knowledge"
    saved_guest = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    saved_guest.verified_email = guest["email"]
    saved_guest.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text

    # The external guest shares their own Knowledge, not the room owner's entity.
    guest_doc, source = await _knowledge_file(db_session, tmp_path, monkeypatch, guest)
    owner_doc, _ = await _knowledge_file(db_session, tmp_path, monkeypatch, owner, name="owner.txt")
    denied = await client.post(endpoint, headers=guest_headers, json={"document_id": owner_doc.id})
    assert denied.status_code == 404, denied.text
    added = await client.post(endpoint, headers=guest_headers, json={
        "document_id": guest_doc.id, "content": "Shared from Knowledge",
        "mentioned_user_ids": [owner["id"]],
    })
    assert added.status_code == 201, added.text
    message = added.json()
    assert message["content"] == "Shared from Knowledge"
    assert message["sender_user_avatar_url"] == guest_avatar
    assert message["mentioned_user_ids"] == [owner["id"]]
    assert len(message["attachments"]) == 1
    attachment = message["attachments"][0]
    assert attachment["name"] == "source.txt"
    assert attachment["size"] == len(b"Knowledge bytes")
    from packages.core.services.plan_gate import _count_storage

    assert await _count_storage(db_session, owner["entity_id"]) == pytest.approx(
        2 * len(b"Knowledge bytes") / (1024 * 1024)
    )
    assert await _count_storage(db_session, guest["entity_id"]) == pytest.approx(
        len(b"Knowledge bytes") / (1024 * 1024)
    )
    assert (await client.get(attachment["download_url"], headers=owner_headers)).content == b"Knowledge bytes"
    source.write_bytes(b"Knowledge changed after publishing")
    guest_doc.is_trashed = True
    await db_session.commit()
    # Publishing snapshots bytes; later edits/trash do not redirect group access.
    download = await client.get(attachment["download_url"], headers=owner_headers)
    assert download.status_code == 200, download.text
    assert download.content == b"Knowledge bytes"
    assert (await db_session.execute(select(Document).where(Document.source == "chat_upload"))).scalars().all() == []


@pytest.mark.asyncio
async def test_group_knowledge_share_rechecks_policy_after_scan(client: AsyncClient, db_session, tmp_path, monkeypatch):
    owner_headers, owner = await _register(client, "knowledge_policy_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Knowledge policy", "owner_scope": "personal",
        "recipient_email": "not-joined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    endpoint = f"/api/v1/chat/rooms/{room_id}/files/from-knowledge"
    document, source = await _knowledge_file(db_session, tmp_path, monkeypatch, owner)
    request = {"document_id": document.id}
    document.classification = "confidential"
    await db_session.commit()
    assert (await client.post(endpoint, headers=owner_headers, json=request)).status_code == 403
    document.classification = "internal"
    document.fs_path = None
    await db_session.commit()
    assert (await client.post(endpoint, headers=owner_headers, json=request)).status_code == 415
    document.fs_path = "source.txt"
    source.write_bytes(b"x" * 5)
    await db_session.commit()
    with monkeypatch.context() as limit_patch:
        limit_patch.setattr("apps.api.routers.chat_rooms._ROOM_FILE_MAX_BYTES", 4)
        assert (await client.post(endpoint, headers=owner_headers, json=request)).status_code == 413
    source.write_bytes(b"safe source")

    async def unavailable_scan(*args, **kwargs):
        raise UploadSecurityError("Malware scanner is unavailable", status_code=503)

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", unavailable_scan)
    assert (await client.post(endpoint, headers=owner_headers, json=request)).status_code == 503

    async def change_source_while_scanning(*args, **kwargs):
        source.write_bytes(b"different source")
        return "text/plain"

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", change_source_while_scanning)
    changed_bytes = await client.post(endpoint, headers=owner_headers, json=request)
    assert changed_bytes.status_code == 409, changed_bytes.text

    async def classify_while_scanning(*args, **kwargs):
        document.classification = "restricted"
        await db_session.commit()
        return "text/plain"

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", classify_while_scanning)
    changed = await client.post(endpoint, headers=owner_headers, json=request)
    assert changed.status_code == 403, changed.text
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json() == []
    assert (await db_session.execute(select(ChatRoomFile))).scalars().all() == []


@pytest.mark.asyncio
async def test_group_knowledge_requires_external_share_even_with_download(client: AsyncClient, db_session, tmp_path, monkeypatch):
    from tests.test_document_permissions import _create_entity_user

    owner_headers, owner = await _register(client, "knowledge_grant_owner")
    member = await _create_entity_user(owner["entity_id"], "knowledge_download_member")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Grant boundary", "owner_scope": "entity",
        "recipient_email": "pending@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=member["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    document, _ = await _knowledge_file(db_session, tmp_path, monkeypatch, owner)
    grant = ResourceGrant(
        id=generate_ulid(), entity_id=owner["entity_id"],
        resource_type=ResourceType.DOCUMENT, resource_id=document.id,
        subject_type=SubjectType.USER, subject_id=member["id"],
        capabilities=[Capability.VIEW, Capability.DOWNLOAD],
        granted_by=owner["id"], granted_at=datetime.now(timezone.utc),
        status=GrantStatus.ACTIVE,
    )
    db_session.add(grant)
    await db_session.commit()
    endpoint = f"/api/v1/chat/rooms/{room_id}/files/from-knowledge"
    body = {"document_id": document.id}
    denied = await client.post(endpoint, headers=member["headers"], json=body)
    assert denied.status_code == 403, denied.text
    grant.capabilities = [Capability.VIEW, Capability.DOWNLOAD, Capability.SHARE_EXTERNAL]
    await db_session.commit()
    shared = await client.post(endpoint, headers=member["headers"], json=body)
    assert shared.status_code == 201, shared.text
    assert shared.json()["attachments"][0]["name"] == "source.txt"

    async def revoke_share_while_scanning(*args, **kwargs):
        grant.capabilities = [Capability.VIEW, Capability.DOWNLOAD]
        await db_session.commit()
        return "text/plain"

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", revoke_share_while_scanning)
    revoked = await client.post(endpoint, headers=member["headers"], json=body)
    assert revoked.status_code == 403, revoked.text
    assert len((await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()) == 1


@pytest.mark.asyncio
async def test_group_knowledge_selected_entity_does_not_inherit_primary_admin(
    client: AsyncClient, db_session, tmp_path, monkeypatch,
):
    from packages.core.services.auth_service import create_access_token

    primary_headers, primary_owner = await _register(client, "knowledge_primary_admin")
    target_headers, target_owner = await _register(client, "knowledge_target_owner")
    created = await client.post("/api/v1/chat/rooms", headers=target_headers, json={
        "kind": "group", "title": "Target entity", "owner_scope": "entity",
        "recipient_email": "pending@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    db_session.add(UserMembership(
        id=generate_ulid(), user_id=primary_owner["id"],
        entity_id=target_owner["entity_id"], role="member", status="active",
    ))
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=primary_owner["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    document, _ = await _knowledge_file(db_session, tmp_path, monkeypatch, target_owner)
    db_session.add(ResourceGrant(
        id=generate_ulid(), entity_id=target_owner["entity_id"],
        resource_type=ResourceType.DOCUMENT, resource_id=document.id,
        subject_type=SubjectType.USER, subject_id=primary_owner["id"],
        capabilities=[Capability.VIEW, Capability.DOWNLOAD],
        granted_by=target_owner["id"], granted_at=datetime.now(timezone.utc),
        status=GrantStatus.ACTIVE,
    ))
    await db_session.commit()
    selected_token = create_access_token(primary_owner["id"], target_owner["entity_id"], "member")
    selected_headers = {"Authorization": f"Bearer {selected_token}"}
    endpoint = f"/api/v1/chat/rooms/{room_id}/files/from-knowledge"
    denied = await client.post(endpoint, headers=selected_headers, json={"document_id": document.id})
    assert denied.status_code == 403, denied.text
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/files", headers=target_headers)).json() == []


@pytest.mark.asyncio
async def test_group_knowledge_rechecks_room_after_scan(client: AsyncClient, db_session, tmp_path, monkeypatch):
    owner_headers, owner = await _register(client, "knowledge_room_closed")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Closing room", "owner_scope": "personal",
        "recipient_email": "pending@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    document, _ = await _knowledge_file(db_session, tmp_path, monkeypatch, owner)

    async def close_while_scanning(*args, **kwargs):
        closed = await client.delete(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)
        assert closed.status_code == 204, closed.text
        return "text/plain"

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", close_while_scanning)
    rejected = await client.post(f"/api/v1/chat/rooms/{room_id}/files/from-knowledge",
                                 headers=owner_headers, json={"document_id": document.id})
    assert rejected.status_code == 404, rejected.text
    assert (await db_session.execute(select(ChatRoomFile))).scalars().all() == []


@pytest.mark.asyncio
async def test_group_files_are_room_only_and_join_time_scoped(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "file_owner")
    guest_headers, guest = await _register(client, "file_guest")
    outsider_headers, outsider = await _register(client, "file_outsider")
    owner_avatar = f"/api/v1/fs/{owner['entity_id']}/avatars/owner.png"
    owner_user = (await db_session.execute(select(User).where(User.id == owner["id"]))).scalar_one()
    owner_user.avatar_url = owner_avatar
    await db_session.commit()
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Files", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    base = f"/api/v1/chat/rooms/{room_id}"

    old = await client.post(f"{base}/files", headers=owner_headers,
                            files={"file": ("before.txt", b"old private bytes", "text/plain")})
    assert old.status_code == 201, old.text
    old_file = old.json()["attachments"][0]
    assert old.json()["content"] == ""
    assert old.json()["sender_user_avatar_url"] == owner_avatar
    assert old_file["message_id"] == old.json()["id"]
    assert old_file["sender_name"] == (owner.get("display_name") or owner["email"])
    assert old_file["size"] == len(b"old private bytes")
    assert (await client.get(f"{base}/files", headers=guest_headers)).status_code == 404
    assert (await client.get(old_file["download_url"], headers=outsider_headers)).status_code == 404

    saved_guest = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    saved_guest.verified_email = guest["email"]
    saved_guest.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    assert (await client.get(f"{base}/files", headers=guest_headers)).json() == []
    assert (await client.get(old_file["download_url"], headers=guest_headers)).status_code == 404
    assert (await client.get(f"{base}/messages?before={old.json()['id']}", headers=guest_headers)).status_code == 404

    fresh = await client.post(f"{base}/files", headers=owner_headers,
                              files={"file": ("visible.html", b"<b>hello</b>", "text/html")},
                              data={"content": "Here is the file", "mentioned_user_ids": json.dumps([guest["id"]])})
    assert fresh.status_code == 201, fresh.text
    file = fresh.json()["attachments"][0]
    assert fresh.json()["content"] == "Here is the file"
    assert fresh.json()["sender_user_avatar_url"] == owner_avatar
    assert fresh.json()["mentioned_user_ids"] == [guest["id"]]
    assert file["sender_name"] == (owner.get("display_name") or owner["email"])
    assert (await db_session.execute(select(Document).where(Document.source == "chat_upload"))).scalars().all() == []
    messages = await client.get(f"{base}/messages", headers=guest_headers)
    assert messages.status_code == 200, messages.text
    assert [message["id"] for message in messages.json()] == [fresh.json()["id"]]
    assert messages.json()[0]["sender_user_avatar_url"] == owner_avatar
    assert messages.json()[0]["attachments"][0]["id"] == file["id"]
    assert messages.json()[0]["mentioned_user_ids"] == [guest["id"]]
    listed = await client.get(f"{base}/files", headers=guest_headers)
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "private, no-store"
    assert [item["id"] for item in listed.json()] == [file["id"]]
    assert (await client.get(f"{base}/files?q=visible", headers=guest_headers)).json()[0]["id"] == file["id"]
    assert (await client.get(f"{base}/files?q=missing", headers=guest_headers)).json() == []
    assert (await client.get(f"{base}/files?before={old_file['id']}", headers=guest_headers)).status_code == 404
    invalid_mention = await client.post(f"{base}/files", headers=owner_headers,
                                        files={"file": ("ignored.txt", b"ignored", "text/plain")},
                                        data={"mentioned_user_ids": json.dumps([outsider["id"]])})
    assert invalid_mention.status_code == 422
    guest_upload = await client.post(f"{base}/files", headers=guest_headers,
                                     files={"file": ("second.txt", b"second", "text/plain")})
    assert guest_upload.status_code == 201, guest_upload.text
    page = await client.get(f"{base}/files?limit=1", headers=guest_headers)
    assert [item["id"] for item in page.json()] == [guest_upload.json()["attachments"][0]["id"]]
    next_page = await client.get(f"{base}/files?limit=1&before={page.json()[0]['id']}", headers=guest_headers)
    assert [item["id"] for item in next_page.json()] == [file["id"]]
    download = await client.get(file["download_url"], headers=guest_headers)
    assert download.status_code == 200, download.text
    assert download.content == b"<b>hello</b>"
    assert download.headers["content-type"].startswith("application/octet-stream")
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"
    assert (await client.get(file["download_url"], headers=outsider_headers)).status_code == 404

    another = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Another", "owner_scope": "personal",
        "recipient_email": outsider["email"],
    })
    assert another.status_code == 201, another.text
    assert (await client.get(f"/api/v1/chat/rooms/{another.json()['id']}/files/{file['id']}",
                             headers=owner_headers)).status_code == 404
    removed = await client.delete(f"{base}/members/{guest['id']}", headers=owner_headers)
    assert removed.status_code == 204, removed.text
    assert (await client.get(f"{base}/files", headers=guest_headers)).status_code == 404
    assert (await client.get(file["download_url"], headers=guest_headers)).status_code == 404
    reinvite = await client.post(f"{base}/invites", headers=owner_headers, json={"email": guest["email"]})
    assert reinvite.status_code == 201, reinvite.text
    assert reinvite.json()["outcome"] == "invited"
    assert reinvite.json()["invite_next_step"] == "accept"
    assert (await client.get(base, headers=guest_headers)).status_code == 404
    accepted_again = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                       json={"token": reinvite.json()["invite_token"]})
    assert accepted_again.status_code == 200, accepted_again.text
    assert (await client.get(base, headers=guest_headers)).status_code == 200
    assert (await client.get(f"{base}/files", headers=guest_headers)).json() == []
    assert (await client.get(file["download_url"], headers=guest_headers)).status_code == 404
    assert (await client.delete(base, headers=owner_headers)).status_code == 204
    assert (await client.get(f"{base}/files", headers=owner_headers)).status_code == 404
    assert (await client.get(file["download_url"], headers=owner_headers)).status_code == 404
    assert (await db_session.execute(select(ChatRoomFile).where(ChatRoomFile.id == file["id"]))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_group_file_removal_respects_sender_and_manager_roles(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "remove_file_owner")
    admin_headers, admin = await _register(client, "remove_file_admin")
    sender_headers, sender = await _register(client, "remove_file_sender")
    member_headers, member = await _register(client, "remove_file_member")
    outsider_headers, _ = await _register(client, "remove_file_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "File removal", "owner_scope": "entity",
        "recipient_email": "pending-remove@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    base = f"/api/v1/chat/rooms/{room_id}"
    now = datetime.now(timezone.utc)
    for account, role in ((admin, "admin"), (sender, "member"), (member, "member")):
        db_session.add(ChatRoomMember(
            id=generate_ulid(), room_id=room_id, user_id=account["id"],
            role=role, status="active", visible_from=now,
        ))
    await db_session.commit()
    own_upload = await client.post(f"{base}/files", headers=sender_headers,
                                   files={"file": ("mine.txt", b"my bytes", "text/plain")})
    owner_upload = await client.post(f"{base}/files", headers=owner_headers,
                                     files={"file": ("owner.txt", b"owner bytes", "text/plain")})
    assert own_upload.status_code == 201 and owner_upload.status_code == 201
    own_file = own_upload.json()["attachments"][0]
    owner_file = owner_upload.json()["attachments"][0]
    files = f"{base}/files"
    assert [(item["name"], item["can_delete"]) for item in (await client.get(files, headers=sender_headers)).json()] == [
        ("owner.txt", False), ("mine.txt", True),
    ]
    assert all(item["can_delete"] for item in (await client.get(files, headers=admin_headers)).json())
    assert all(not item["can_delete"] for item in (await client.get(files, headers=member_headers)).json())
    assert (await client.delete(f"{files}/{owner_file['id']}", headers=sender_headers)).status_code == 403
    assert (await client.delete(f"{files}/{own_file['id']}", headers=member_headers)).status_code == 403
    assert (await client.delete(f"{files}/{own_file['id']}", headers=outsider_headers)).status_code == 404
    assert (await client.delete(f"{files}/{own_file['id']}", headers=sender_headers)).status_code == 204
    assert [item["id"] for item in (await client.get(files, headers=owner_headers)).json()] == [owner_file["id"]]
    assert (await client.get(f"{files}/{own_file['id']}", headers=owner_headers)).status_code == 404
    messages = (await client.get(f"{base}/messages", headers=owner_headers)).json()
    assert next(item for item in messages if item["id"] == own_upload.json()["id"])["attachments"] == []
    stored = (await db_session.execute(select(ChatRoomFile).where(ChatRoomFile.id == own_file["id"]))).scalar_one()
    assert stored.deleted_at is not None and stored.content == b"my bytes"
    assert (await client.delete(f"{files}/{own_file['id']}", headers=sender_headers)).status_code == 404
    assert (await client.delete(f"{files}/{owner_file['id']}", headers=admin_headers)).status_code == 204
    assert (await client.get(files, headers=owner_headers)).json() == []


@pytest.mark.asyncio
async def test_group_file_validation_and_scan_failure_create_no_message(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms

    assert chat_rooms._ROOM_FILE_MAX_BYTES == 1024 * 1024 * 1024
    assert chat_rooms._ROOM_FILES_TOTAL_MAX_BYTES == 10 * 1024 * 1024 * 1024
    owner_headers, owner = await _register(client, "file_validation_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Validation", "owner_scope": "personal",
        "recipient_email": "someone@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    endpoint = f"/api/v1/chat/rooms/{room_id}/files"
    assert (await client.post(endpoint, headers=owner_headers,
                              files={"file": ("empty.txt", b"", "text/plain")})).status_code == 422
    assert (await client.post(endpoint, headers=owner_headers,
                              files={"file": ("script.exe", b"MZbad", "application/octet-stream")})).status_code == 400
    assert (await client.post(endpoint, headers=owner_headers,
                              files={"file": ("wrong.png", b"not an image", "image/png")})).status_code == 400
    with monkeypatch.context() as limit_patch:
        limit_patch.setattr(chat_rooms, "_ROOM_FILE_MAX_BYTES", 4)
        assert (await client.post(endpoint, headers=owner_headers,
                                  files={"file": ("large.txt", b"a" * 5, "text/plain")})).status_code == 413

    async def unavailable_scan(*args, **kwargs):
        raise UploadSecurityError("Malware scanner is unavailable", status_code=503)

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", unavailable_scan)
    unavailable = await client.post(endpoint, headers=owner_headers,
                                    files={"file": ("safe.txt", b"safe", "text/plain")})
    assert unavailable.status_code == 503, unavailable.text
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json() == []
    assert (await client.get(endpoint, headers=owner_headers)).json() == []
    assert (await db_session.execute(select(ChatRoomFile))).scalars().all() == []
    direct = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "dm", "recipient_email": "dm-file@example.com",
    })
    assert direct.status_code == 201, direct.text
    assert (await client.post(f"/api/v1/chat/rooms/{direct.json()['id']}/files", headers=owner_headers,
                              files={"file": ("safe.txt", b"safe", "text/plain")})).status_code == 403


@pytest.mark.asyncio
async def test_group_file_larger_than_old_limit_is_accepted(client: AsyncClient, monkeypatch):
    owner_headers, _ = await _register(client, "file_100m_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Larger file", "owner_scope": "personal",
        "recipient_email": "file-100m-guest@example.com",
    })
    assert created.status_code == 201, created.text

    async def accept_scan(*args, **kwargs):
        return "text/plain"

    monkeypatch.setattr("apps.api.routers.chat_rooms.inspect_upload_content", accept_scan)
    content = b"a" * (10 * 1024 * 1024 + 1)
    uploaded = await client.post(f"/api/v1/chat/rooms/{created.json()['id']}/files",
                                 headers=owner_headers,
                                 files={"file": ("larger.txt", content, "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["attachments"][0]["size"] == len(content)


@pytest.mark.asyncio
async def test_group_chunk_file_upload_download_and_scan_failure(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms

    monkeypatch.setattr(chat_rooms, "ROOM_FILE_INLINE_MAX_BYTES", 4)
    monkeypatch.setattr(chat_rooms, "_ROOM_FILE_MAX_BYTES", 11)
    owner_headers, _ = await _register(client, "file_chunk_owner")
    outsider_headers, _ = await _register(client, "file_chunk_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Chunk file", "owner_scope": "personal",
        "recipient_email": "chunk-guest@example.com",
    })
    assert created.status_code == 201, created.text
    endpoint = f"/api/v1/chat/rooms/{created.json()['id']}/files"
    scanned = []

    async def scan(source, **kwargs):
        scanned.append(source.read(11))
        source.seek(0)
        return "text/plain"

    monkeypatch.setattr(chat_rooms, "inspect_upload_stream", scan)
    uploaded = await client.post(endpoint, headers=owner_headers,
                                 files={"file": ("chunked.txt", b"hello world", "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    assert scanned == [b"hello world"]
    attachment = uploaded.json()["attachments"][0]
    stored = await db_session.get(ChatRoomFile, attachment["id"])
    assert stored.content is None and stored.chunk_count == 1 and stored.content_sha256
    downloaded = await client.get(attachment["download_url"], headers=owner_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"hello world"
    assert downloaded.headers["content-length"] == "11"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert (await client.get(attachment["download_url"], headers=outsider_headers)).status_code == 404
    assert (await client.post(endpoint, headers=owner_headers,
                              files={"file": ("oversize.txt", b"hello world!", "text/plain")})).status_code == 413

    async def reject(*args, **kwargs):
        raise UploadSecurityError("Malware scanner is unavailable", status_code=503)

    monkeypatch.setattr(chat_rooms, "inspect_upload_stream", reject)
    assert (await client.post(endpoint, headers=owner_headers,
                              files={"file": ("rejected.txt", b"not clean", "text/plain")})).status_code == 503
    assert [item["id"] for item in (await client.get(endpoint, headers=owner_headers)).json()] == [attachment["id"]]
    assert (await client.delete(attachment["download_url"], headers=owner_headers)).status_code == 204
    assert (await client.get(attachment["download_url"], headers=owner_headers)).status_code == 404


@pytest.mark.asyncio
async def test_group_file_ingress_is_bounded_before_multipart_parse(client: AsyncClient, db_session, monkeypatch):
    from apps.api.middleware.room_file_ingress import ROOM_FILE_BODY_MAX_BYTES
    from apps.api.routers import chat_rooms

    assert ROOM_FILE_BODY_MAX_BYTES == 1024 * 1024 * 1024 + 128 * 1024

    owner_headers, _ = await _register(client, "file_ingress_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Ingress", "owner_scope": "personal",
        "recipient_email": "ingress-guest@example.com",
    })
    assert created.status_code == 201, created.text
    endpoint = f"/api/v1/chat/rooms/{created.json()['id']}/files"

    async def unexpected_scan(*args, **kwargs):
        raise AssertionError("oversized body reached the upload scanner")

    original_scan = chat_rooms.inspect_upload_content
    monkeypatch.setattr(chat_rooms, "inspect_upload_content", unexpected_scan)
    declared = await client.post(endpoint, headers={
        **owner_headers, "Content-Length": str(ROOM_FILE_BODY_MAX_BYTES + 1),
        "Content-Type": "multipart/form-data; boundary=declared",
    }, content=b"x")
    assert declared.status_code == 413, declared.text
    # Exercise the same receive-side boundary without allocating a GiB fixture.
    test_body_limit = 4096
    monkeypatch.setattr("apps.api.middleware.room_file_ingress.ROOM_FILE_BODY_MAX_BYTES", test_body_limit)

    async def oversized_chunks():
        yield b"--stream\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.txt\"\r\n\r\n"
        yield b"x" * test_body_limit

    # Streaming content has no Content-Length, so the receive-side count must
    # reject it before Starlette parses/spools the multipart file.
    chunked = await client.post(endpoint, headers={
        **owner_headers, "Content-Type": "multipart/form-data; boundary=stream",
    }, content=oversized_chunks())
    assert chunked.status_code == 413, chunked.text
    assert (await db_session.execute(select(ChatRoomFile))).scalars().all() == []
    assert (await db_session.execute(select(ChatRoomMessage))).scalars().all() == []

    async def valid_chunks():
        yield b"--ok\r\nContent-Disposition: form-data; name=\"file\"; filename=\"small.txt\"\r\n"
        yield b"Content-Type: text/plain\r\n\r\nsmall\r\n--ok--\r\n"

    monkeypatch.setattr(chat_rooms, "inspect_upload_content", original_scan)
    accepted = await client.post(endpoint, headers={
        **owner_headers, "Content-Type": "multipart/form-data; boundary=ok",
    }, content=valid_chunks())
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["attachments"][0]["size"] == 5


@pytest.mark.asyncio
async def test_group_file_quota_serializes_concurrent_uploads(client: AsyncClient, db_session, monkeypatch):
    # Scale the ceiling so legacy-sized fixture rows can exercise the race.
    monkeypatch.setattr("apps.api.routers.chat_rooms._ROOM_FILES_TOTAL_MAX_BYTES", 500 * 1024 * 1024)
    owner_headers, owner = await _register(client, "file_quota_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Quota", "owner_scope": "personal",
        "recipient_email": "quota-guest@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    now = datetime.now(timezone.utc)
    seed_message = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id,
        sender_user_id=owner["id"], content="Earlier files", mentioned_user_ids=[], created_at=now,
    )
    db_session.add(seed_message)
    await db_session.flush()
    # Seed existing logical sizes without allocating a 500 MiB test payload.
    for i in range(50):
        db_session.add(ChatRoomFile(
            id=generate_ulid(), message_id=seed_message.id,
            name=f"earlier-{i}.txt", mime_type="text/plain",
            size=10 * 1024 * 1024 if i < 49 else 10 * 1024 * 1024 - 5,
            content=b"fixture", created_at=now,
        ))
    await db_session.commit()

    endpoint = f"/api/v1/chat/rooms/{room_id}/files"

    async def upload(name: str):
        return await client.post(endpoint, headers=owner_headers,
                                 files={"file": (name, b"four", "text/plain")})

    first, second = await asyncio.gather(upload("first.txt"), upload("second.txt"))
    assert sorted((first.status_code, second.status_code)) == [201, 413]
    assert (await client.post(endpoint, headers=owner_headers,
                              files={"file": ("over.txt", b"two", "text/plain")})).status_code == 413
    visible = await client.get(f"{endpoint}?limit=100", headers=owner_headers)
    assert visible.status_code == 200, visible.text
    assert len(visible.json()) == 51
    assert sum(item["size"] for item in visible.json()) <= 500 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_scope", ["personal", "entity"])
async def test_entity_knowledge_quota_serializes_distinct_rooms_and_counts_closed_rooms(
    client: AsyncClient, db_session, monkeypatch, owner_scope: str,
):
    from packages.core.services import plan_enforcement, plan_gate

    owner_headers, _ = await _register(client, f"file_aggregate_{owner_scope}")
    room_ids = []
    for index in range(2):
        created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
            "kind": "group", "title": f"Aggregate {index}", "owner_scope": owner_scope,
            "recipient_email": f"aggregate-guest-{index}@example.com",
        })
        assert created.status_code == 201, created.text
        room_ids.append(created.json()["id"])

    async def storage_plan(*args, **kwargs):
        return {"name": "Test", "storage_mb": 9 / (1024 * 1024)}

    monkeypatch.setattr(plan_gate, "is_cloud", lambda: True)
    monkeypatch.setattr(plan_enforcement, "get_entity_plan", storage_plan)

    async def upload(room_id: str):
        return await client.post(f"/api/v1/chat/rooms/{room_id}/files", headers=owner_headers,
                                 files={"file": ("five.txt", b"12345", "text/plain")})

    first, second = await asyncio.gather(*(upload(room_id) for room_id in room_ids))
    assert sorted((first.status_code, second.status_code)) == [201, 402]
    successful_room = room_ids[0] if first.status_code == 201 else room_ids[1]
    failed_room = room_ids[1] if first.status_code == 201 else room_ids[0]
    assert (second if second.status_code == 402 else first).json()["detail"]["kind"] == "storage"
    assert (await client.delete(f"/api/v1/chat/rooms/{successful_room}", headers=owner_headers)).status_code == 204
    assert (await upload(failed_room)).status_code == 402
    assert (await client.get(f"/api/v1/chat/rooms/{failed_room}/files", headers=owner_headers)).json() == []
    assert len((await db_session.execute(select(ChatRoomFile))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_personal_and_entity_group_files_share_knowledge_capacity(client: AsyncClient, db_session, monkeypatch):
    from packages.core.services import plan_enforcement, plan_gate

    owner_headers, owner = await _register(client, "file_scope_separation")
    db_session.add(Document(
        id=generate_ulid(), entity_id=owner["entity_id"], name="existing.txt",
        file_size=3, fs_path="existing.txt", source="upload",
    ))
    await db_session.commit()
    room_ids = []
    for owner_scope in ("personal", "entity"):
        created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
            "kind": "group", "title": owner_scope, "owner_scope": owner_scope,
            "recipient_email": f"separate-{owner_scope}@example.com",
        })
        assert created.status_code == 201, created.text
        room_ids.append(created.json()["id"])

    async def storage_plan(*args, **kwargs):
        return {"name": "Test", "storage_mb": 11 / (1024 * 1024)}

    monkeypatch.setattr(plan_gate, "is_cloud", lambda: True)
    monkeypatch.setattr(plan_enforcement, "get_entity_plan", storage_plan)

    for room_id, content in zip(room_ids, (b"12345", b"123")):
        uploaded = await client.post(f"/api/v1/chat/rooms/{room_id}/files", headers=owner_headers,
                                     files={"file": ("shared.txt", content, "text/plain")})
        assert uploaded.status_code == 201, uploaded.text
    gate = await plan_gate.check(db_session, owner["entity_id"], "storage_mb", use_cache=False)
    assert gate.current == pytest.approx(11 / (1024 * 1024))
    assert (await client.post(f"/api/v1/chat/rooms/{room_ids[0]}/files", headers=owner_headers,
                              files={"file": ("one.txt", b"x", "text/plain")})).status_code == 402


@pytest.mark.asyncio
async def test_group_members_can_invite_and_manage_only_their_own_invites(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "member_invite_owner")
    member_headers, member = await _register(client, "member_invite_member")
    outsider_headers, _ = await _register(client, "member_invite_outsider")
    saved_member = (await db_session.execute(select(User).where(User.id == member["id"]))).scalar_one()
    saved_member.verified_email = member["email"]
    saved_member.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Member invites", "owner_scope": "personal",
        "recipient_email": member["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    base = f"/api/v1/chat/rooms/{room_id}"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=member_headers,
                              json={"token": created.json()["invite_token"]})).status_code == 200
    assert (await client.get(f"{base}/invites", headers=member_headers)).json() == []

    owner_invite = await client.post(f"{base}/invites", headers=owner_headers,
                                     json={"email": "owner-pending@example.com"})
    assert owner_invite.status_code == 201, owner_invite.text
    owner_invite_id = (await client.get(f"{base}/invites", headers=owner_headers)).json()[0]["id"]
    assert (await client.get(f"{base}/invites", headers=member_headers)).json() == []
    assert (await client.post(f"{base}/invites", headers=member_headers,
                              json={"email": "owner-pending@example.com"})).status_code == 409
    assert (await client.delete(f"{base}/invites/{owner_invite_id}", headers=member_headers)).status_code == 404

    member_invite = await client.post(f"{base}/invites", headers=member_headers,
                                      json={"email": "member-pending@example.com"})
    assert member_invite.status_code == 201, member_invite.text
    own_invites = (await client.get(f"{base}/invites", headers=member_headers)).json()
    assert [invite["email"] for invite in own_invites] == ["member-pending@example.com"]
    assert len((await client.get(f"{base}/invites", headers=owner_headers)).json()) == 2
    assert (await client.delete(f"{base}/invites/{own_invites[0]['id']}", headers=member_headers)).status_code == 204
    assert (await client.get(f"{base}/invites", headers=member_headers)).json() == []
    assert [invite["email"] for invite in (await client.get(f"{base}/invites", headers=owner_headers)).json()] == [
        "owner-pending@example.com",
    ]
    assert (await client.patch(base, headers=member_headers, json={"title": "Not allowed"})).status_code == 403
    assert (await client.post(f"{base}/invites", headers=outsider_headers,
                              json={"email": "outsider-pending@example.com"})).status_code == 404
    assert (await client.delete(f"{base}/membership", headers=member_headers)).status_code == 204
    assert (await client.post(f"{base}/invites", headers=member_headers,
                              json={"email": "after-leaving@example.com"})).status_code == 404


@pytest.mark.asyncio
async def test_group_invitation_is_separate_from_company_access(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "room_owner")
    guest_headers, guest = await _register(client, "room_guest")
    stranger_headers, stranger = await _register(client, "room_stranger")

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Customer room", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["recipient_outcome"] == "invited"
    assert created.json()["invite_next_step"] == "verify_email"
    room_id = created.json()["id"]
    token = created.json()["invite_token"]
    assert token
    pending = await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)
    assert pending.status_code == 200, pending.text
    assert pending.headers["cache-control"] == "private, no-store"
    assert pending.json()[0]["email"] == guest["email"]
    assert pending.json()[0]["status"] == "pending"
    assert "invite_token" not in pending.json()[0]
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=guest_headers)).status_code == 404
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=stranger_headers)).status_code == 404
    assert created.json()["owner_scope"] == "entity"
    assert "owner_entity_id" not in created.json()
    default_agent = created.json()["agents"]
    assert len(default_agent) == 1
    assert default_agent[0]["name"] == "Manor AI"
    assert default_agent[0]["is_default"] is True
    assert default_agent[0]["context_scope"] == "room_only"
    default_sub = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.id == default_agent[0]["subscription_id"],
    ))).scalar_one()
    assert default_sub.agent_id == MANOR_AGENT_ID
    assert default_sub.workspace_id is None
    assert default_sub.owner_user_id is None
    assert (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.agent_id == MANOR_AGENT_ID,
        AgentSubscription.owner_user_id == owner["id"],
    ))).scalars().all() == []
    assert (await client.delete(
        f"/api/v1/chat/rooms/{room_id}/agents/{default_agent[0]['id']}", headers=owner_headers,
    )).status_code == 403

    old = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers,
                            json={"content": "Before joining"})
    assert old.status_code == 201, old.text
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=guest_headers)).status_code == 404
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=stranger_headers)).status_code == 404
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=stranger_headers,
                              json={"content": "No access"})).status_code == 404

    unverified = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                   json={"token": token})
    assert unverified.status_code == 403
    saved_stranger = (await db_session.execute(select(User).where(User.id == stranger["id"]))).scalar_one()
    saved_stranger.verified_email = stranger["email"]
    saved_stranger.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    wrong_email = await client.post("/api/v1/chat/rooms/invites/accept", headers=stranger_headers,
                                    json={"token": token})
    assert wrong_email.status_code == 403
    saved_guest = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    saved_guest.verified_email = guest["email"]
    saved_guest.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": token})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["id"] == room_id
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)).json() == []
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=guest_headers)).json() == []
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                              json={"token": token})).status_code == 404
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers)).json() == []

    fresh = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers,
                              json={"content": "After joining", "mentioned_user_ids": [guest["id"]]})
    assert fresh.status_code == 201, fresh.text
    visible = await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers)
    assert visible.status_code == 200, visible.text
    assert [item["content"] for item in visible.json()] == ["After joining"]
    assert visible.json()[0]["mentioned_user_ids"] == [guest["id"]]
    invalid_mention = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers,
                                        json={"content": "@stranger", "mentioned_user_ids": [stranger["id"]]})
    assert invalid_mention.status_code == 422

    memberships = (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id == guest["id"], UserMembership.entity_id == owner["entity_id"],
    ))).scalars().all()
    assert memberships == []
    assert guest["entity_id"] != owner["entity_id"]

    removed = await client.delete(f"/api/v1/chat/rooms/{room_id}/members/{guest['id']}", headers=owner_headers)
    assert removed.status_code == 204, removed.text
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers)).status_code == 404


@pytest.mark.asyncio
async def test_verified_manor_users_accept_group_invites_without_company_access(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "direct_group_owner")
    guest_headers, guest = await _register(client, "direct_group_guest")
    third_headers, third = await _register(client, "direct_group_third")
    for person in (guest, third):
        saved = (await db_session.execute(select(User).where(User.id == person["id"]))).scalar_one()
        saved.verified_email = person["email"]
        saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Verified team", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room = created.json()
    assert room["recipient_outcome"] == "invited"
    assert room["invite_token"]
    assert room["invite_next_step"] == "accept"
    assert room["member_count"] == 1
    notification = (await db_session.execute(select(Notification).where(
        Notification.user_id == guest["id"], Notification.type == "chat_room_invite",
    ))).scalar_one()
    assert notification.entity_id == guest["entity_id"]
    assert notification.workspace_id is None
    assert notification.meta["link"].startswith("/chat?invite=")
    url = f"/api/v1/chat/rooms/{room['id']}"
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": room["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["member_count"] == 2
    await db_session.refresh(notification)
    assert notification.dispatch_status == "canceled"
    assert (await client.get(f"{url}/invites", headers=owner_headers)).json() == []

    invited = await client.post(f"{url}/invites", headers=owner_headers, json={"email": third["email"]})
    assert invited.status_code == 201, invited.text
    assert invited.json()["outcome"] == "invited"
    assert invited.json()["invite_token"]
    assert invited.json()["invite_next_step"] == "accept"
    assert invited.json()["email"] == third["email"]
    assert (await client.get(url, headers=third_headers)).status_code == 404
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=third_headers,
                              json={"token": invited.json()["invite_token"]})).status_code == 200
    assert (await client.get(url, headers=third_headers)).json()["member_count"] == 3
    assert (await client.post(f"{url}/invites", headers=owner_headers,
                              json={"email": third["email"]})).status_code == 409
    assert (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id.in_([guest["id"], third["id"]]),
        UserMembership.entity_id == owner["entity_id"],
    ))).scalars().all() == []


@pytest.mark.asyncio
async def test_group_invite_email_and_cancel_are_scoped(client: AsyncClient, db_session, monkeypatch):
    from packages.core.services import email_service

    sent = []

    async def record_email(credentials, **kwargs):
        sent.append((kwargs["to"], kwargs))
        assert kwargs["expected_sender"] == "inviter@example.com"
        assert credentials["username"] == "inviter@example.com"
        return True

    monkeypatch.setattr(email_service, "send_connected_email", record_email)
    owner_headers, _ = await _register(client, "invite_delivery_owner")
    await _connect_group_mailbox(client, owner_headers, "inviter@example.com")
    outsider_headers, outsider = await _register(client, "invite_delivery_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Invite delivery", "owner_scope": "personal",
        "recipient_email": "new-invitee@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    assert created.json()["invite_email_sent"] is True
    assert sent[0][0] == "new-invitee@example.com"
    assert f"/chat?invite={created.json()['invite_token']}" in sent[0][1]["content"]
    pending = (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)).json()
    assert len(pending) == 1 and pending[0]["id"]
    cancel_url = f"/api/v1/chat/rooms/{room_id}/invites/{pending[0]['id']}"
    assert (await client.delete(cancel_url, headers=outsider_headers)).status_code == 404
    assert (await client.delete(cancel_url, headers=owner_headers)).status_code == 204
    assert (await client.delete(cancel_url, headers=owner_headers)).status_code == 404
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)).json() == []
    invite = (await db_session.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.id == pending[0]["id"],
    ))).scalar_one()
    assert invite.status == "revoked"
    saved_outsider = (await db_session.execute(select(User).where(User.id == outsider["id"]))).scalar_one()
    saved_outsider.verified_email = outsider["email"]
    saved_outsider.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    in_app = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers,
                               json={"email": outsider["email"]})
    assert in_app.status_code == 201 and in_app.json()["invite_next_step"] == "accept"
    assert in_app.json()["email_sent"] is True
    assert len(sent) == 2  # Verified users receive both email and an in-app invitation.
    assert sent[1][0] == outsider["email"]
    pending = (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)).json()
    notification = (await db_session.execute(select(Notification).where(
        Notification.user_id == outsider["id"], Notification.type == "chat_room_invite",
    ))).scalar_one()
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}/invites/{pending[0]['id']}",
                                headers=owner_headers)).status_code == 204
    await db_session.refresh(notification)
    assert notification.dispatch_status == "canceled"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=outsider_headers,
                              json={"token": in_app.json()["invite_token"]})).status_code == 404



@pytest.mark.asyncio
async def test_group_unread_counts_follow_displayed_messages(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "room_unread_owner")
    guest_headers, guest = await _register(client, "room_unread_guest")
    saved_guest = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    saved_guest.verified_email = guest["email"]
    saved_guest.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Unread room", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    room_id = created.json()["id"]
    base = f"/api/v1/chat/rooms/{room_id}"
    before_join = await client.post(f"{base}/messages", headers=owner_headers, json={"content": "Before join"})
    assert before_join.status_code == 201
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                              json={"token": created.json()["invite_token"]})).status_code == 200
    first = await client.post(f"{base}/messages", headers=owner_headers, json={"content": "First"})
    second = await client.post(f"{base}/messages", headers=owner_headers, json={"content": "Second"})
    assert first.status_code == second.status_code == 201
    mine = await client.post(f"{base}/messages", headers=guest_headers, json={"content": "My reply"})
    assert mine.status_code == 201
    listed = (await client.get("/api/v1/chat/rooms", headers=guest_headers)).json()
    assert next(room for room in listed if room["id"] == room_id)["unread_count"] == 2
    assert (await client.post(f"{base}/read", headers=guest_headers,
                              json={"through_message_id": before_join.json()["id"]})).status_code == 404
    assert (await client.post(f"{base}/read", headers=guest_headers,
                              json={"through_message_id": first.json()["id"]})).status_code == 204
    listed = (await client.get("/api/v1/chat/rooms", headers=guest_headers)).json()
    assert next(room for room in listed if room["id"] == room_id)["unread_count"] == 1
    assert (await client.post(f"{base}/read", headers=guest_headers,
                              json={"through_message_id": mine.json()["id"]})).status_code == 204
    listed = (await client.get("/api/v1/chat/rooms", headers=guest_headers)).json()
    assert next(room for room in listed if room["id"] == room_id)["unread_count"] == 0


@pytest.mark.asyncio
async def test_direct_group_add_revokes_old_invite_and_rejoin_resets_access(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "direct_rejoin_owner")
    guest_headers, guest = await _register(client, "direct_rejoin_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Rejoin test", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["recipient_outcome"] == "invited"
    assert created.json()["invite_next_step"] == "verify_email"
    old_token = created.json()["invite_token"]
    assert old_token
    url = f"/api/v1/chat/rooms/{created.json()['id']}"
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    assert (await client.post(f"{url}/messages", headers=owner_headers,
                              json={"content": "Before first join"})).status_code == 201

    saved_guest = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    saved_guest.verified_email = guest["email"]
    saved_guest.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    first_add = await client.post(f"{url}/invites", headers=owner_headers, json={"email": guest["email"]})
    assert first_add.status_code == 201, first_add.text
    assert first_add.json()["outcome"] == "invited"
    assert first_add.json()["invite_next_step"] == "accept"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                              json={"token": old_token})).status_code == 404
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                              json={"token": first_add.json()["invite_token"]})).status_code == 200
    assert (await client.get(f"{url}/invites", headers=owner_headers)).json() == []
    assert (await client.get(f"{url}/messages", headers=guest_headers)).json() == []

    first_visible = await client.post(f"{url}/messages", headers=owner_headers,
                                      json={"content": "First membership"})
    assert first_visible.status_code == 201, first_visible.text
    assert (await client.get(f"{url}/messages", headers=guest_headers)).json()[0]["id"] == first_visible.json()["id"]
    assert (await client.patch(f"{url}/members/{guest['id']}", headers=owner_headers,
                               json={"role": "admin"})).status_code == 200
    original_member = (await db_session.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == created.json()["id"], ChatRoomMember.user_id == guest["id"],
    ))).scalar_one()
    original_member_id = original_member.id
    assert (await client.delete(f"{url}/members/{guest['id']}", headers=owner_headers)).status_code == 204
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    missed = await client.post(f"{url}/messages", headers=owner_headers,
                               json={"content": "While absent"})
    assert missed.status_code == 201, missed.text

    readded = await client.post(f"{url}/invites", headers=owner_headers, json={"email": guest["email"]})
    assert readded.status_code == 201, readded.text
    assert readded.json()["outcome"] == "invited"
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                              json={"token": readded.json()["invite_token"]})).status_code == 200
    room = (await client.get(url, headers=guest_headers)).json()
    assert next(person for person in room["members"] if person["user_id"] == guest["id"])["role"] == "member"
    member = (await db_session.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == created.json()["id"], ChatRoomMember.user_id == guest["id"],
    ))).scalar_one()
    await db_session.refresh(member)
    assert member.id == original_member_id
    assert member.status == "active"
    assert member.left_at is None
    assert member.visible_from >= datetime.fromisoformat(missed.json()["created_at"].replace("Z", "+00:00"))
    assert (await client.get(f"{url}/messages", headers=guest_headers)).json() == []
    new_message = await client.post(f"{url}/messages", headers=owner_headers,
                                    json={"content": "After rejoining"})
    assert new_message.status_code == 201, new_message.text
    assert [message["id"] for message in (await client.get(f"{url}/messages", headers=guest_headers)).json()] == [
        new_message.json()["id"],
    ]


@pytest.mark.asyncio
async def test_pending_manor_account_must_verify_before_group_join(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "pending_group_owner")
    first_headers, first = await _register(client, "pending_group_first")
    second_headers, second = await _register(client, "pending_group_second")
    for person in (first, second):
        saved = (await db_session.execute(select(User).where(User.id == person["id"]))).scalar_one()
        saved.status = "pending"
    await db_session.commit()

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Pending accounts", "owner_scope": "entity",
        "recipient_email": first["email"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["recipient_outcome"] == "invited"
    assert created.json()["invite_next_step"] == "verify_email"
    assert created.json()["member_count"] == 1
    url = f"/api/v1/chat/rooms/{created.json()['id']}"
    second_invite = await client.post(f"{url}/invites", headers=owner_headers, json={"email": second["email"]})
    assert second_invite.status_code == 201, second_invite.text
    assert second_invite.json()["outcome"] == "invited"
    assert second_invite.json()["invite_next_step"] == "verify_email"
    assert (await db_session.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == created.json()["id"],
        ChatRoomMember.user_id.in_([first["id"], second["id"]]),
    ))).scalars().all() == []

    for person in (first, second):
        saved = (await db_session.execute(select(User).where(User.id == person["id"]))).scalar_one()
        saved.status = "active"
        saved.verified_email = person["email"]
        saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    for headers, token in ((first_headers, created.json()["invite_token"]),
                           (second_headers, second_invite.json()["invite_token"])):
        accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=headers,
                                     json={"token": token})
        assert accepted.status_code == 200, accepted.text
    assert (await client.get(url, headers=owner_headers)).json()["member_count"] == 3
    assert (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id.in_([first["id"], second["id"]]),
        UserMembership.entity_id == owner["entity_id"],
    ))).scalars().all() == []


@pytest.mark.asyncio
async def test_group_rejects_inactive_or_deleted_existing_accounts(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "unavailable_group_owner")
    _, inactive = await _register(client, "unavailable_group_inactive")
    _, deleted = await _register(client, "unavailable_group_deleted")
    inactive_user = (await db_session.execute(select(User).where(User.id == inactive["id"]))).scalar_one()
    inactive_user.status = "disabled"
    deleted_user = (await db_session.execute(select(User).where(User.id == deleted["id"]))).scalar_one()
    deleted_user.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    rejected_create = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Unavailable", "owner_scope": "personal",
        "recipient_email": inactive["email"],
    })
    assert rejected_create.status_code == 409
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Available", "owner_scope": "personal",
        "recipient_email": "unknown-person@example.com",
    })
    assert created.status_code == 201, created.text
    url = f"/api/v1/chat/rooms/{created.json()['id']}"
    for person in (inactive, deleted):
        rejected = await client.post(f"{url}/invites", headers=owner_headers, json={"email": person["email"]})
        assert rejected.status_code == 409
    assert (await client.get(f"{url}/invites", headers=owner_headers)).json()[0]["email"] == "unknown-person@example.com"


@pytest.mark.asyncio
async def test_only_group_owner_sets_admin_and_removed_admin_rejoins_as_member(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "role_owner")
    admin_headers, admin = await _register(client, "role_admin")
    member_headers, member = await _register(client, "role_member")
    for account in (admin, member):
        saved = (await db_session.execute(select(User).where(User.id == account["id"]))).scalar_one()
        saved.verified_email = account["email"]
        saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Role test", "owner_scope": "personal",
        "recipient_email": admin["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    role_url = f"/api/v1/chat/rooms/{room_id}/members/{admin['id']}"
    assert created.json()["recipient_outcome"] == "invited"
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=admin_headers)).status_code == 404
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=admin_headers,
                              json={"token": created.json()["invite_token"]})).status_code == 200
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=admin_headers)).status_code == 200
    assert (await client.patch(role_url, headers=admin_headers, json={"role": "admin"})).status_code == 403
    assert (await client.patch(role_url, headers=owner_headers, json={"role": "owner"})).status_code == 422
    assert (await client.patch(f"/api/v1/chat/rooms/{room_id}/members/{owner['id']}",
                               headers=owner_headers, json={"role": "member"})).status_code == 403

    promoted = await client.patch(role_url, headers=owner_headers, json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    assert next(person for person in promoted.json()["members"] if person["user_id"] == admin["id"])["role"] == "admin"
    invitation = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=admin_headers,
                                   json={"email": member["email"]})
    assert invitation.status_code == 201, invitation.text
    assert invitation.json()["outcome"] == "invited"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=member_headers,
                              json={"token": invitation.json()["invite_token"]})).status_code == 200
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=member_headers)).status_code == 200
    member_url = f"/api/v1/chat/rooms/{room_id}/members/{member['id']}"
    assert (await client.patch(member_url, headers=admin_headers, json={"role": "admin"})).status_code == 403
    assert (await client.delete(role_url, headers=admin_headers)).status_code == 403
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}/members/{owner['id']}",
                                headers=admin_headers)).status_code == 403
    assert (await client.delete(member_url, headers=admin_headers)).status_code == 204
    assert (await client.patch(role_url, headers=member_headers, json={"role": "member"})).status_code == 404

    demoted = await client.patch(role_url, headers=owner_headers, json={"role": "member"})
    assert demoted.status_code == 200, demoted.text
    assert next(person for person in demoted.json()["members"] if person["user_id"] == admin["id"])["role"] == "member"
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=admin_headers,
                              json={"email": "another@example.com"})).status_code == 201

    assert (await client.patch(role_url, headers=owner_headers, json={"role": "admin"})).status_code == 200
    assert (await client.delete(role_url, headers=owner_headers)).status_code == 204
    reinvite = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers,
                                 json={"email": admin["email"]})
    assert reinvite.status_code == 201, reinvite.text
    assert reinvite.json()["outcome"] == "invited"
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=admin_headers)).status_code == 404
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=admin_headers,
                              json={"token": reinvite.json()["invite_token"]})).status_code == 200
    rejoined = await client.get(f"/api/v1/chat/rooms/{room_id}", headers=admin_headers)
    assert rejoined.status_code == 200, rejoined.text
    assert next(person for person in rejoined.json()["members"] if person["user_id"] == admin["id"])["role"] == "member"


@pytest.mark.asyncio
async def test_group_owner_and_admin_can_rename_but_members_cannot(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "rename_owner")
    admin_headers, admin = await _register(client, "rename_admin")
    member_headers, member = await _register(client, "rename_member")
    outsider_headers, _ = await _register(client, "rename_outsider")
    for account in (admin, member):
        saved = (await db_session.execute(select(User).where(User.id == account["id"]))).scalar_one()
        saved.verified_email = account["email"]
        saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Original", "owner_scope": "entity",
        "recipient_email": admin["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    assert created.json()["recipient_outcome"] == "invited"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=admin_headers,
                              json={"token": created.json()["invite_token"]})).status_code == 200
    assert (await client.patch(f"/api/v1/chat/rooms/{room_id}/members/{admin['id']}",
                               headers=owner_headers, json={"role": "admin"})).status_code == 200
    invite = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=admin_headers,
                               json={"email": member["email"]})
    assert invite.status_code == 201, invite.text
    assert invite.json()["outcome"] == "invited"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=member_headers,
                              json={"token": invite.json()["invite_token"]})).status_code == 200

    url = f"/api/v1/chat/rooms/{room_id}"
    renamed = await client.patch(url, headers=admin_headers, json={"title": "  Customer team  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Customer team"
    assert (await client.get(url, headers=member_headers)).json()["title"] == "Customer team"
    assert next(room for room in (await client.get("/api/v1/chat/rooms", headers=owner_headers)).json()
                if room["id"] == room_id)["title"] == "Customer team"
    assert (await client.patch(url, headers=member_headers, json={"title": "No"})).status_code == 403
    assert (await client.patch(url, headers=outsider_headers, json={"title": "No"})).status_code == 404
    assert (await client.patch(url, headers=owner_headers, json={"title": "  "})).status_code == 422
    assert (await client.patch(url, headers=owner_headers, json={"title": "x" * 256})).status_code == 422
    assert (await client.patch(url, headers=owner_headers, json={"title": "Owner title"})).json()["title"] == "Owner title"

    direct = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "dm", "recipient_email": admin["email"],
    })
    assert direct.status_code == 201, direct.text
    assert (await client.patch(f"/api/v1/chat/rooms/{direct.json()['id']}", headers=owner_headers,
                               json={"title": "Not a group"})).status_code == 403


@pytest.mark.asyncio
async def test_only_owner_can_close_group_and_closed_group_keeps_audit_rows(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "close_owner")
    admin_headers, admin = await _register(client, "close_admin")
    member_headers, member = await _register(client, "close_member")
    pending_headers, pending = await _register(client, "close_pending")
    for account in (admin, member):
        saved = (await db_session.execute(select(User).where(User.id == account["id"]))).scalar_one()
        saved.verified_email = account["email"]
        saved.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Close test", "owner_scope": "entity",
        "recipient_email": admin["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    url = f"/api/v1/chat/rooms/{room_id}"
    assert created.json()["recipient_outcome"] == "invited"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=admin_headers,
                              json={"token": created.json()["invite_token"]})).status_code == 200
    assert (await client.patch(f"{url}/members/{admin['id']}", headers=owner_headers,
                               json={"role": "admin"})).status_code == 200
    member_invite = await client.post(f"{url}/invites", headers=admin_headers,
                                      json={"email": member["email"]})
    assert member_invite.status_code == 201, member_invite.text
    assert member_invite.json()["outcome"] == "invited"
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=member_headers,
                              json={"token": member_invite.json()["invite_token"]})).status_code == 200
    pending_invite = await client.post(f"{url}/invites", headers=admin_headers,
                                       json={"email": pending["email"]})
    assert pending_invite.status_code == 201, pending_invite.text
    posted = await client.post(f"{url}/messages", headers=owner_headers,
                               json={"content": "Retained for audit"})
    assert posted.status_code == 201, posted.text

    assert (await client.delete(url, headers=admin_headers)).status_code == 403
    assert (await client.delete(url, headers=member_headers)).status_code == 403
    assert (await client.delete(url, headers=pending_headers)).status_code == 404
    closed = await client.delete(url, headers=owner_headers)
    assert closed.status_code == 204, closed.text
    assert (await client.delete(url, headers=owner_headers)).status_code == 404
    for headers in (owner_headers, admin_headers, member_headers):
        assert (await client.get(url, headers=headers)).status_code == 404
        assert all(room["id"] != room_id for room in (
            await client.get("/api/v1/chat/rooms", headers=headers)
        ).json())
    assert (await client.get(f"{url}/messages", headers=member_headers)).status_code == 404
    assert (await client.post(f"{url}/messages", headers=admin_headers,
                              json={"content": "After close"})).status_code == 404
    assert (await client.get(f"{url}/invites", headers=owner_headers)).status_code == 404
    assert (await client.post(f"{url}/invites", headers=owner_headers,
                              json={"email": "another@example.com"})).status_code == 404
    assert (await client.get(f"{url}/presence", headers=owner_headers)).status_code == 404
    assert (await client.get(f"{url}/agent-candidates", headers=owner_headers)).status_code == 404
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=pending_headers,
                              json={"token": pending_invite.json()["invite_token"]})).status_code == 404

    saved_room = (await db_session.execute(select(ChatRoom).where(ChatRoom.id == room_id))).scalar_one()
    assert saved_room.status == "closed"
    assert (await db_session.execute(select(ChatRoomMessage).where(
        ChatRoomMessage.id == posted.json()["id"], ChatRoomMessage.room_id == room_id,
    ))).scalar_one().content == "Retained for audit"
    assert (await db_session.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.room_id == room_id, ChatRoomInvite.email == pending["email"],
    ))).scalar_one().status == "pending"
    assert (await db_session.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.room_id == room_id,
    ))).scalars().first() is not None

    direct = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "dm", "recipient_email": pending["email"],
    })
    assert direct.status_code == 201, direct.text
    assert (await client.delete(f"/api/v1/chat/rooms/{direct.json()['id']}",
                                headers=owner_headers)).status_code == 403


@pytest.mark.asyncio
async def test_group_invitation_status_expires_and_refreshes_without_exposing_token(client: AsyncClient, db_session):
    owner_headers, _ = await _register(client, "room_invite_status")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Status room", "owner_scope": "personal",
        "recipient_email": "external-invite@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    invitation = (await db_session.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.room_id == room_id,
    ))).scalar_one()
    invitation.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    expired = (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)).json()
    assert len(expired) == 1
    assert expired[0]["email"] == "external-invite@example.com"
    assert expired[0]["status"] == "expired"
    assert datetime.fromisoformat(expired[0]["expires_at"].replace("Z", "+00:00")) == invitation.expires_at
    assert "token_hash" not in expired[0]

    refreshed = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers,
                                  json={"email": "external-invite@example.com"})
    assert refreshed.status_code == 201, refreshed.text
    assert refreshed.json()["outcome"] == "invited"
    assert refreshed.json()["invite_next_step"] == "sign_up"
    assert refreshed.json()["invite_token"]
    latest = (await client.get(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers)).json()
    assert len(latest) == 1
    assert latest[0]["status"] == "pending"
    assert latest[0]["email"] == "external-invite@example.com"


@pytest.mark.asyncio
async def test_private_chat_requires_cross_company_acceptance_and_cannot_gain_a_third_member(client: AsyncClient, db_session):
    first_headers, first = await _register(client, "dm_first")
    second_headers, second = await _register(client, "dm_second")
    first_create = await client.post("/api/v1/chat/rooms", headers=first_headers, json={
        "kind": "dm", "recipient_email": second["email"],
    })
    assert first_create.status_code == 201, first_create.text
    room_id = first_create.json()["id"]
    assert first_create.json()["member_count"] == 1
    assert first_create.json()["agents"] == []
    assert first_create.json()["invite_token"]
    wrong_recipient = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=first_headers,
                                        json={"email": "third@example.com"})
    assert wrong_recipient.status_code == 403
    reissued = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=first_headers,
                                 json={"email": second["email"]})
    assert reissued.status_code == 201, reissued.text
    assert reissued.json()["invite_token"] != first_create.json()["invite_token"]
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=first_headers,
                              json={"content": "Too soon"})).status_code == 409
    saved_second = (await db_session.execute(select(User).where(User.id == second["id"]))).scalar_one()
    saved_second.verified_email = second["email"]
    saved_second.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert (await client.post("/api/v1/chat/rooms/invites/accept", headers=second_headers,
                              json={"token": first_create.json()["invite_token"]})).status_code == 404
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=second_headers,
                                 json={"token": reissued.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    second_create = await client.post("/api/v1/chat/rooms", headers=second_headers, json={
        "kind": "dm", "recipient_email": first["email"],
    })
    assert second_create.status_code == 201, second_create.text
    assert second_create.json()["id"] == room_id
    assert second_create.json()["member_count"] == 2

    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=first_headers,
                             json={"content": "Hello"})
    assert sent.status_code == 201, sent.text
    assert [item["content"] for item in (await client.get(
        f"/api/v1/chat/rooms/{room_id}/messages", headers=second_headers,
    )).json()] == ["Hello"]
    cannot_invite = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=first_headers,
                                      json={"email": "third@example.com"})
    assert cannot_invite.status_code == 403


@pytest.mark.asyncio
async def test_owner_lifecycle_blocks_company_room(client: AsyncClient, db_session):
    headers, user = await _register(client, "room_lifecycle")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Company room", "owner_scope": "entity",
        "recipient_email": "guest-not-yet-registered@example.com",
    })
    assert created.status_code == 201, created.text
    owner = (await db_session.execute(select(Entity).where(Entity.id == user["entity_id"]))).scalar_one()
    owner.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert (await client.get(f"/api/v1/chat/rooms/{created.json()['id']}", headers=headers)).status_code in {401, 403, 404}


@pytest.mark.asyncio
async def test_chat_invite_allows_customer_signup_without_company_membership(client: AsyncClient, db_session, monkeypatch):
    owner_headers, owner = await _register(client, "signup_owner")
    customer_email = "new-chat-customer@example.com"
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Customer support", "owner_scope": "entity",
        "recipient_email": customer_email,
    })
    assert created.status_code == 201, created.text
    assert created.json()["recipient_outcome"] == "invited"
    assert created.json()["invite_next_step"] == "sign_up"
    assert created.json()["invite_token"]

    async def invite_required(_db):
        return True

    monkeypatch.setattr("packages.core.services.invitation_codes.is_required", invite_required)
    no_invite = await client.post("/api/v1/auth/register", json={
        "email": "other-customer@example.com", "password": "test-password-123",
    })
    assert no_invite.status_code == 403
    wrong_email = await client.post("/api/v1/auth/register", json={
        "email": "other-customer@example.com", "password": "test-password-123",
        "chat_invite_token": created.json()["invite_token"],
    })
    assert wrong_email.status_code == 403
    registered = await client.post("/api/v1/auth/register", json={
        "email": customer_email, "password": "test-password-123",
        "chat_invite_token": created.json()["invite_token"],
    })
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["entity_id"] != owner["entity_id"]
    assert (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id == me["id"], UserMembership.entity_id == owner["entity_id"],
    ))).scalars().all() == []
    assert (await client.get(f"/api/v1/chat/rooms/{created.json()['id']}", headers=headers)).status_code == 404

    customer = (await db_session.execute(select(User).where(User.id == me["id"]))).scalar_one()
    customer.verified_email = customer_email
    customer.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_group_binds_exact_agent_deployment_without_granting_customer_workspace_access(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "agent_room_owner")
    guest_headers, guest = await _register(client, "agent_room_guest")
    room_response = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Support", "owner_scope": "entity", "recipient_email": guest["email"],
    })
    assert room_response.status_code == 201, room_response.text
    room_id = room_response.json()["id"]

    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Support AI", status="active")
    first_ws = Workspace(entity_id=owner["entity_id"], name="Alpha", status="active")
    second_ws = Workspace(entity_id=owner["entity_id"], name="Beta", status="active")
    db_session.add_all([agent, first_ws, second_ws])
    await db_session.flush()
    first = AgentSubscription(entity_id=owner["entity_id"], agent_id=agent.id,
                              workspace_id=first_ws.id, status="active")
    second = AgentSubscription(entity_id=owner["entity_id"], agent_id=agent.id,
                               workspace_id=second_ws.id, status="active")
    db_session.add_all([first, second])
    await db_session.commit()
    await _allow_workspace_room_reply(db_session, owner, first_ws.id, room_id)
    await _allow_workspace_room_reply(db_session, owner, second_ws.id, room_id)

    candidates = await client.get(f"/api/v1/chat/rooms/{room_id}/agent-candidates", headers=owner_headers)
    assert candidates.status_code == 200, candidates.text
    assert {candidate["subscription_id"] for candidate in candidates.json()} >= {first.id, second.id}
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/agent-candidates", headers=guest_headers)).status_code == 404
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=guest_headers,
                              json={"subscription_id": first.id})).status_code == 404

    added = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": second.id})
    assert added.status_code == 201, added.text
    assert added.json()["subscription_id"] == second.id
    assert added.json()["workspace_name"] == "Beta"
    binding_id = added.json()["id"]
    duplicate = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                                  json={"subscription_id": second.id})
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == binding_id
    owner_view = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)).json()
    selected = next(item for item in owner_view["agents"] if item["id"] == binding_id)
    assert selected["subscription_id"] == second.id
    assert selected["workspace_name"] == "Beta"
    assert selected["context_scope"] == "agent_and_group_audience"

    guest_user = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    guest_user.verified_email = guest["email"]
    guest_user.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": room_response.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    guest_view = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=guest_headers)).json()
    assert len(guest_view["agents"]) == 2
    assert next(item for item in guest_view["agents"] if item["id"] == binding_id) == {
        "id": binding_id, "name": "Support AI", "available": True,
        "is_default": False, "context_scope": "agent_and_group_audience",
        "avatar_url": None,
    }
    assert next(item for item in guest_view["agents"] if item["is_default"])["context_scope"] == "room_only"
    assert guest_view["can_manage_agents"] is False
    assert (await client.delete(
        f"/api/v1/chat/rooms/{room_id}/agents/{binding_id}", headers=guest_headers,
    )).status_code == 403
    default_binding_id = next(item["id"] for item in owner_view["agents"] if item["is_default"])
    assert (await client.delete(
        f"/api/v1/chat/rooms/{room_id}/agents/{default_binding_id}", headers=owner_headers,
    )).status_code == 403
    second_ws.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert next(item for item in (await client.get(
        f"/api/v1/chat/rooms/{room_id}", headers=guest_headers,
    )).json()["agents"] if item["id"] == binding_id)["available"] is False
    unavailable_mention = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers,
                                            json={"content": "@Support AI unavailable",
                                                  "mentioned_agent_binding_ids": [binding_id]})
    assert unavailable_mention.status_code == 422, unavailable_mention.text
    assert (await db_session.execute(select(ChatRoomAgentRun))).scalars().all() == []
    second_ws.deleted_at = None
    await db_session.commit()
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/agent-candidates", headers=guest_headers)).status_code == 403
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=guest_headers,
                              json={"subscription_id": first.id})).status_code == 403
    assert (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id == guest["id"], UserMembership.entity_id == owner["entity_id"],
    ))).scalars().all() == []

    ordinary = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers,
                                 json={"content": "@Support AI Please help"})
    assert ordinary.status_code == 201, ordinary.text
    assert len((await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers)).json()) == 1

    removed = await client.delete(f"/api/v1/chat/rooms/{room_id}/agents/{binding_id}", headers=owner_headers)
    assert removed.status_code == 204
    assert (await db_session.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.id == binding_id,
    ))).scalar_one_or_none() is None
    for headers in (owner_headers, guest_headers):
        remaining = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=headers)).json()["agents"]
        assert len(remaining) == 1 and remaining[0]["is_default"] is True
    assert (await client.delete(
        f"/api/v1/chat/rooms/{room_id}/agents/{binding_id}", headers=owner_headers,
    )).status_code == 404
    assert second.id in {candidate["subscription_id"] for candidate in (
        await client.get(f"/api/v1/chat/rooms/{room_id}/agent-candidates", headers=owner_headers)
    ).json()}

    readded = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                                json={"subscription_id": second.id})
    assert readded.status_code == 201, readded.text
    assert readded.json()["id"] != binding_id
    assert readded.json()["subscription_id"] == second.id

    # Rows disabled by the previous remove behavior must not remain in the list.
    old_binding_id = generate_ulid()
    db_session.add(ChatRoomAgentBinding(
        id=old_binding_id, room_id=room_id, subscription_id=first.id,
        added_by_user_id=owner["id"], status="disabled",
    ))
    await db_session.commit()
    for headers in (owner_headers, guest_headers):
        visible = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=headers)).json()["agents"]
        assert {item["id"] for item in visible} == {default_binding_id, readded.json()["id"]}


@pytest.mark.asyncio
async def test_workspace_agent_requiring_group_reply_approval_is_addable(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    headers, owner = await _register(client, "agent_reply_policy_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Policy gate", "owner_scope": "entity",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Needs approval", status="active")
    workspace = Workspace(entity_id=owner["entity_id"], name="Approval Workspace", status="active")
    db_session.add_all([agent, workspace])
    await db_session.flush()
    subscription = AgentSubscription(
        entity_id=owner["entity_id"], agent_id=agent.id,
        workspace_id=workspace.id, status="active",
    )
    db_session.add(subscription)
    await db_session.commit()
    await _require_workspace_reply_review(db_session, owner, workspace)
    candidates_url = f"/api/v1/chat/rooms/{room_id}/agent-candidates"
    binding_url = f"/api/v1/chat/rooms/{room_id}/agents"
    assert subscription.id in {item["subscription_id"] for item in (
        await client.get(candidates_url, headers=headers)
    ).json()}
    allowed = await client.post(binding_url, headers=headers, json={"subscription_id": subscription.id})
    assert allowed.status_code == 201, allowed.text

    binding_id = allowed.json()["id"]
    room = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=headers)).json()
    assert next(item for item in room["agents"] if item["id"] == binding_id)["available"] is True
    mention = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=headers, json={
        "content": "@Needs approval help", "mentioned_agent_binding_ids": [binding_id],
    })
    assert mention.status_code == 201

    await _allow_workspace_room_reply(db_session, owner, workspace.id, room_id)
    assert subscription.id in {item["subscription_id"] for item in (
        await client.get(candidates_url, headers=headers)
    ).json()}
    assert (await client.post(binding_url, headers=headers, json={"subscription_id": subscription.id})).status_code == 201

    await _set_workspace_room_reply(db_session, owner, workspace.id, room_id, allowed=False)
    room = (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=headers)).json()
    assert next(item for item in room["agents"] if item["id"] == binding_id)["available"] is True


@pytest.mark.asyncio
async def test_group_agent_binding_rejects_foreign_and_inactive_deployments(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "agent_scope_owner")
    other_headers, other = await _register(client, "agent_scope_other")
    room = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "External", "owner_scope": "entity", "recipient_email": other["email"],
    })
    room_id = room.json()["id"]
    foreign_agent = Agent(entity_id=other["entity_id"], owner_user_id=other["id"], name="Foreign", status="active")
    foreign_ws = Workspace(entity_id=other["entity_id"], name="Private", status="active")
    db_session.add_all([foreign_agent, foreign_ws])
    await db_session.flush()
    foreign_sub = AgentSubscription(entity_id=other["entity_id"], agent_id=foreign_agent.id,
                                    workspace_id=foreign_ws.id, status="active")
    db_session.add(foreign_sub)
    await db_session.commit()
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": foreign_sub.id})).status_code == 404
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=other_headers,
                              json={"subscription_id": foreign_sub.id})).status_code == 404

    own_agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Inactive", status="active")
    own_ws = Workspace(entity_id=owner["entity_id"], name="Removed", status="active")
    db_session.add_all([own_agent, own_ws])
    await db_session.flush()
    own_sub = AgentSubscription(entity_id=owner["entity_id"], agent_id=own_agent.id,
                                workspace_id=own_ws.id, status="disabled")
    db_session.add(own_sub)
    await db_session.commit()
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": own_sub.id})).status_code == 404
    own_sub.status = "active"
    own_ws.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert (await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": own_sub.id})).status_code == 404


@pytest.mark.asyncio
async def test_personal_group_has_room_only_manor_and_can_add_personal_agent(client: AsyncClient, db_session):
    headers, owner = await _register(client, "personal_agent_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Friends", "owner_scope": "personal",
        "recipient_email": "friend@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    default = created.json()["agents"]
    assert len(default) == 1
    assert default[0]["is_default"] is True
    assert default[0]["context_scope"] == "room_only"
    master = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.id == default[0]["subscription_id"],
    ))).scalar_one()
    assert master.agent_id == MANOR_AGENT_ID
    assert master.owner_user_id == owner["id"]
    assert master.workspace_id is None

    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"],
                  name="Research Helper", status="active")
    db_session.add(agent)
    await db_session.flush()
    personal = AgentSubscription(entity_id=owner["entity_id"], agent_id=agent.id,
                                 owner_user_id=owner["id"], name="Personal", status="active")
    db_session.add(personal)
    await db_session.commit()
    candidates = (await client.get(
        f"/api/v1/chat/rooms/{room_id}/agent-candidates", headers=headers,
    )).json()
    assert {candidate["subscription_id"]: candidate["name"] for candidate in candidates}[personal.id] == "Research Helper"
    added = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=headers,
                              json={"subscription_id": personal.id})
    assert added.status_code == 201, added.text
    assert added.json()["name"] == "Research Helper"
    assert added.json()["context_scope"] == "agent_and_group_audience"


@pytest.mark.asyncio
async def test_group_agent_mention_persists_one_run_only_for_selected_binding(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, owner = await _register(client, "mention_admission_owner")
    outsider_headers, _ = await _register(client, "mention_admission_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Mention admission", "owner_scope": "personal",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    messages_url = f"/api/v1/chat/rooms/{room_id}/messages"

    ordinary = await client.post(messages_url, headers=owner_headers, json={"content": "@Manor AI ordinary text"})
    assert ordinary.status_code == 201, ordinary.text
    assert (await db_session.execute(select(ChatRoomAgentRun))).scalars().all() == []

    unknown = await client.post(messages_url, headers=owner_headers, json={
        "content": "@Manor AI invalid selection", "mentioned_agent_binding_ids": [generate_ulid()],
    })
    assert 400 <= unknown.status_code < 500, unknown.text
    assert (await db_session.execute(select(ChatRoomAgentRun))).scalars().all() == []
    assert (await client.post(messages_url, headers=outsider_headers, json={
        "content": "@Manor AI unauthorized", "mentioned_agent_binding_ids": [binding_id],
    })).status_code == 404

    selected = await client.post(messages_url, headers=owner_headers, json={
        "content": "@Manor AI help with this group", "mentioned_agent_binding_ids": [binding_id, binding_id],
    })
    assert selected.status_code == 201, selected.text
    assert selected.json()["sender_user_id"] == owner["id"]
    runs = (await db_session.execute(select(ChatRoomAgentRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].room_id == room_id
    assert runs[0].source_message_id == selected.json()["id"]
    assert runs[0].binding_id == binding_id
    assert runs[0].status == "pending"
    assert len(runs[0].audience_fingerprint) == 64
    visible = (await client.get(messages_url, headers=owner_headers)).json()
    run_view = visible[-1]["agent_runs"][0]
    assert run_view["assistant_blocks"] == [{
        "id": f"room-process-{runs[0].id}", "type": "process", "lifecycle_status": "waiting",
        "default_collapsed": False, "steps": [],
    }]
    assert [run_view] == [{
        "id": runs[0].id, "binding_id": binding_id,
        "agent_name": "Manor AI", "status": "pending",
        "assistant_blocks": [{
            "id": f"room-process-{runs[0].id}", "type": "process",
            "lifecycle_status": "waiting", "default_collapsed": False, "steps": [],
        }],
    }]


@pytest.mark.asyncio
async def test_group_agent_reply_has_durable_identity_and_worker_is_idempotent(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, _ = await _register(client, "mention_reply_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Agent reply", "owner_scope": "personal",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Manor AI summarize this conversation",
        "mentioned_agent_binding_ids": [binding_id],
    })
    assert sent.status_code == 201, sent.text
    run = (await db_session.execute(select(ChatRoomAgentRun))).scalar_one()
    run_id = run.id
    calls = []

    async def fake_completion(*args, **kwargs):
        calls.append((args, kwargs))
        return "A group-only response."

    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", fake_completion)
    await room_agent_runtime.execute_room_agent_run(run_id)
    await room_agent_runtime.execute_room_agent_run(run_id)
    db_session.expire_all()
    completed = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert completed.status == "succeeded"
    assert completed.response_message_id
    assert len(calls) == 1
    visible = await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)
    assert visible.status_code == 200, visible.text
    assert [message["content"] for message in visible.json()] == [
        "@Manor AI summarize this conversation", "A group-only response.",
    ]
    assert visible.json()[0]["agent_runs"][0]["status"] == "succeeded"
    reply = visible.json()[1]
    assert reply["id"] == completed.response_message_id
    assert reply["sender_user_id"] is None
    assert reply["sender_name"] == "Manor AI"
    process, = reply["assistant_blocks"]
    assert process["lifecycle_status"] == "completed"
    assert process["steps"] == []
    assert process["started_at_ms"] <= process["ended_at_ms"]
    assert process["duration_ms"] == process["ended_at_ms"] - process["started_at_ms"]


@pytest.mark.asyncio
async def test_group_agent_turn_distinguishes_same_named_speakers(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, owner = await _register(client, "same_name_owner")
    guest_headers, guest = await _register(client, "same_name_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Same names", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    guest_user = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    guest_user.verified_email = guest["email"]
    guest_user.email_verified_at = datetime.now(timezone.utc)
    owner_user = (await db_session.execute(select(User).where(User.id == owner["id"]))).scalar_one()
    owner_user.display_name = guest_user.display_name = "Alex"
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text

    messages_url = f"/api/v1/chat/rooms/{room_id}/messages"
    for headers, content in ((owner_headers, "I prefer A"), (guest_headers, "I prefer B")):
        sent = await client.post(messages_url, headers=headers, json={"content": content})
        assert sent.status_code == 201, sent.text
    owner_question = await client.post(messages_url, headers=owner_headers, json={
        "content": "@Manor AI which option did I prefer?",
        "mentioned_agent_binding_ids": [binding_id],
    })
    guest_question = await client.post(messages_url, headers=guest_headers, json={
        "content": "@Manor AI which option did I prefer?",
        "mentioned_agent_binding_ids": [binding_id],
    })
    assert owner_question.status_code == guest_question.status_code == 201
    runs = (await db_session.execute(select(ChatRoomAgentRun))).scalars().all()
    turns = {}
    for run in runs:
        turn, error = await room_agent_runtime._admit_run(db_session, run)
        assert error is None
        assert turn is not None
        turns[run.source_message_id] = turn

    owner_turn = turns[owner_question.json()["id"]]
    guest_turn = turns[guest_question.json()["id"]]
    owner_history = [json.loads(entry["content"]) for entry in owner_turn.history]
    guest_history = [json.loads(entry["content"]) for entry in guest_turn.history]
    assert [entry["message"] for entry in owner_history] == ["I prefer A", "I prefer B"]
    assert owner_history[0]["sender_name"] == owner_history[1]["sender_name"] == "Alex"
    assert owner_history[0]["sender_label"] != owner_history[1]["sender_label"]
    assert owner_turn.requester_label == owner_history[0]["sender_label"]
    assert guest_turn.requester_label == guest_history[1]["sender_label"]
    assert owner_history[0]["sender_label"] == guest_history[0]["sender_label"]
    assert owner_history[1]["sender_label"] == guest_history[1]["sender_label"]
    assert owner_turn.requester_label == guest_history[2]["sender_label"]
    assert owner["id"] not in str(owner_history) + str(guest_history)
    assert guest["id"] not in str(owner_history) + str(guest_history)


@pytest.mark.asyncio
async def test_group_agent_history_budget_keeps_the_newest_messages(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, _owner = await _register(client, "history_budget_owner")
    _guest_headers, guest = await _register(client, "history_budget_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Long room", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    messages_url = f"/api/v1/chat/rooms/{room_id}/messages"
    for index in range(6):
        sent = await client.post(messages_url, headers=owner_headers, json={
            "content": f"message {index} " + "x" * 200,
        })
        assert sent.status_code == 201, sent.text
    question = await client.post(messages_url, headers=owner_headers, json={
        "content": "@Manor AI what did I just say?",
        "mentioned_agent_binding_ids": [binding_id],
    })
    assert question.status_code == 201, question.text
    run = (await db_session.execute(select(ChatRoomAgentRun))).scalar_one()
    # Room for roughly three of the six messages.
    one_message = len((await room_agent_runtime._admit_run(db_session, run))[0].history[0]["content"])
    monkeypatch.setattr(room_agent_runtime, "_MAX_HISTORY_CHARS", one_message * 3 + 10)

    turn, error = await room_agent_runtime._admit_run(db_session, run)

    assert error is None and turn is not None
    kept = [json.loads(entry["content"])["message"].split(" x")[0] for entry in turn.history]
    assert kept == ["message 3", "message 4", "message 5"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        ("llm_configuration_missing", "llm_configuration_missing"),
        ("byok_provider_error", "agent_completion_failed"),
    ],
)
async def test_group_agent_provider_failure_does_not_publish_a_successful_reply(
    client: AsyncClient, db_session, monkeypatch, provider_error: str, expected_code: str,
):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, _ = await _register(client, f"mention_provider_{provider_error}")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Provider failure", "owner_scope": "personal",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Manor AI reply", "mentioned_agent_binding_ids": [created.json()["agents"][0]["id"]],
    })
    assert sent.status_code == 201, sent.text
    run_id = (await db_session.execute(select(ChatRoomAgentRun.id))).scalar_one()
    calls = []

    original_resolve = room_agent_runtime.RuntimeResolver.resolve_trace_envelope

    def resolve(resolver, request, **kwargs):
        return original_resolve(resolver, request, **kwargs)

    async def check_credit(_entity_id, **_kwargs):
        pass

    @asynccontextmanager
    async def bill(_entity_id, **_kwargs):
        yield

    async def failed_completion(**_kwargs):
        calls.append(True)
        return SimpleNamespace(
            content="Provider failure details must not become a chat message.",
            stop_reason="error", error=provider_error,
        )

    monkeypatch.setattr(room_agent_runtime.RuntimeResolver, "resolve_trace_envelope", resolve)
    monkeypatch.setattr(room_agent_runtime, "runtime_assert_credit_available", check_credit)
    monkeypatch.setattr(room_agent_runtime, "runtime_llm_billing_context", bill)
    monkeypatch.setattr(room_agent_runtime, "runtime_execute_chat_agent_loop", failed_completion)
    await room_agent_runtime.execute_room_agent_run(run_id)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert calls == [True]

    db_session.expire_all()
    run = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert run.error_code == expected_code
    assert run.response_message_id is None
    visible = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert [message["id"] for message in visible] == [sent.json()["id"]]
    assert visible[0]["agent_runs"][0]["status"] == "failed"
    assert visible[0]["agent_runs"][0].get("error_code") == (
        "llm_configuration_missing" if provider_error == "llm_configuration_missing" else None
    )


@pytest.mark.asyncio
async def test_removed_group_agent_binding_cannot_start_pending_run(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, owner = await _register(client, "mention_removed_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Revoked Agent", "owner_scope": "personal",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Helper", status="active")
    db_session.add(agent)
    await db_session.flush()
    subscription = AgentSubscription(
        entity_id=owner["entity_id"], agent_id=agent.id,
        owner_user_id=owner["id"], name="Helper", status="active",
    )
    db_session.add(subscription)
    await db_session.commit()
    bound = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": subscription.id})
    assert bound.status_code == 201, bound.text
    binding_id = bound.json()["id"]
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Helper please answer", "mentioned_agent_binding_ids": [binding_id],
    })
    assert sent.status_code == 201, sent.text
    run = (await db_session.execute(select(ChatRoomAgentRun))).scalar_one()
    run_id = run.id
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}/agents/{binding_id}",
                                headers=owner_headers)).status_code == 204
    calls = []

    async def fake_completion(*args, **kwargs):
        calls.append((args, kwargs))
        return "must not appear"

    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", fake_completion)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert calls == []
    visible = (await client.get(
        f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers,
    )).json()
    assert [message["id"] for message in visible] == [sent.json()["id"]]
    assert visible[0]["agent_runs"][0]["status"] == "interrupted"
    db_session.expire_all()
    terminal = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert terminal.status in {"interrupted", "failed"}
    assert terminal.response_message_id is None


@pytest.mark.asyncio
async def test_group_agent_result_is_not_published_after_audience_changes(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, _ = await _register(client, "mention_audience_owner")
    _, new_member = await _register(client, "mention_audience_new_member")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Audience fence", "owner_scope": "personal",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Manor AI explain this", "mentioned_agent_binding_ids": [binding_id],
    })
    assert sent.status_code == 201, sent.text
    run = (await db_session.execute(select(ChatRoomAgentRun))).scalar_one()
    run_id = run.id
    calls = []

    async def change_audience(*args, **kwargs):
        calls.append((args, kwargs))
        db_session.add(ChatRoomMember(
            id=generate_ulid(), room_id=room_id, user_id=new_member["id"],
            role="member", status="active", visible_from=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        return "a stale answer"

    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", change_audience)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert len(calls) == 1
    assert [message["id"] for message in (await client.get(
        f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers,
    )).json()] == [sent.json()["id"]]
    db_session.expire_all()
    terminal = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert terminal.status in {"interrupted", "failed"}
    assert terminal.response_message_id is None


@pytest.mark.asyncio
async def test_workspace_group_agent_governance_blocks_before_model(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms
    from packages.core import governance
    from packages.core.constants.channels import ExternalMessageActionKey
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, owner = await _register(client, "mention_policy_denied")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Policy gate", "owner_scope": "entity",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Policy Agent", status="active")
    workspace = Workspace(entity_id=owner["entity_id"], name="Policy Workspace", status="active")
    db_session.add_all([agent, workspace])
    await db_session.flush()
    subscription = AgentSubscription(
        entity_id=owner["entity_id"], agent_id=agent.id,
        workspace_id=workspace.id, status="active",
    )
    db_session.add(subscription)
    await db_session.commit()
    await _allow_workspace_room_reply(db_session, owner, workspace.id, room_id)
    bound = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": subscription.id})
    assert bound.status_code == 201, bound.text
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Policy Agent answer", "mentioned_agent_binding_ids": [bound.json()["id"]],
    })
    assert sent.status_code == 201, sent.text
    run_id = (await db_session.execute(select(ChatRoomAgentRun.id))).scalar_one()
    checked = []
    completions = []

    async def deny_policy(_db, **kwargs):
        checked.append(kwargs)
        return governance.PolicyDecision(allowed=False, pause_for_hitl=False)

    async def fake_completion(*args, **kwargs):
        completions.append((args, kwargs))
        return "must not appear"

    monkeypatch.setattr(governance, "check_step_policy", deny_policy)
    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", fake_completion)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert checked == [{
        "workspace_id": workspace.id, "kind": "action",
        "action_key": ExternalMessageActionKey.SEND.value,
        "resource_id": room_id, "risk_level": "high",
    }]
    assert completions == []
    visible = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert [message["id"] for message in visible] == [sent.json()["id"]]
    assert visible[0]["agent_runs"][0]["status"] == "interrupted"
    db_session.expire_all()
    run = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert run.status == "interrupted"
    assert run.error_code == "governance_blocked"
    assert run.response_message_id is None


@pytest.mark.asyncio
async def test_workspace_group_reply_requires_exact_dual_authority_approval(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)
    queued = []

    async def hold_run(run_id: str) -> None:
        queued.append(run_id)

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    calls = []

    async def fake_completion(*args, **kwargs):
        calls.append((args, kwargs))
        return "A private draft for the group"

    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", fake_completion)
    owner_headers, owner = await _register(client, "reply_approval_owner")
    admin_headers, admin = await _register(client, "reply_approval_foreign_admin")
    outsider_headers, _ = await _register(client, "reply_approval_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Review group", "owner_scope": "entity",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Review Agent", status="active")
    workspace = Workspace(entity_id=owner["entity_id"], name="Review Workspace", status="active")
    db_session.add_all([agent, workspace])
    await db_session.flush()
    subscription = AgentSubscription(
        entity_id=owner["entity_id"], agent_id=agent.id,
        workspace_id=workspace.id, status="active",
    )
    db_session.add(subscription)
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=admin["id"],
        role="admin", status="active",
        visible_from=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    await db_session.commit()
    await _require_workspace_reply_review(db_session, owner, workspace)
    bound = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": subscription.id})
    assert bound.status_code == 201, bound.text
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Review Agent answer", "mentioned_agent_binding_ids": [bound.json()["id"]],
    })
    assert sent.status_code == 201, sent.text
    run_id = queued[-1]
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert len(calls) == 1
    messages = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert len(messages) == 1
    assert messages[0]["agent_runs"][0]["status"] == "waiting_approval"
    assert messages[0]["agent_runs"][0]["approval_kind"] == "reply"
    url = f"/api/v1/chat/rooms/{room_id}/ai-reply-approvals"
    approvals = (await client.get(url, headers=owner_headers)).json()
    assert len(approvals) == 1
    assert approvals[0]["content"] == "A private draft for the group"
    from packages.core.services.room_tool_approval_service import invalidate_stale_room_tool_approvals

    await invalidate_stale_room_tool_approvals(db_session, room_id=room_id)
    assert len((await client.get(url, headers=owner_headers)).json()) == 1
    assert (await client.get(url, headers=admin_headers)).json() == []
    assert (await client.get(url, headers=outsider_headers)).status_code == 404
    decision_url = f"{url}/{run_id}/decision"
    wrong = await client.post(decision_url, headers=owner_headers, json={
        "approve": True, "expected_digest": "0" * 64,
    })
    assert wrong.status_code == 409
    denied = await client.post(decision_url, headers=admin_headers, json={
        "approve": True, "expected_digest": approvals[0]["digest"],
    })
    assert denied.status_code == 403
    approved = await client.post(decision_url, headers=owner_headers, json={
        "approve": True, "expected_digest": approvals[0]["digest"],
    })
    assert approved.status_code == 200, approved.text
    await room_agent_runtime.execute_room_agent_run(run_id)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert len(calls) == 1  # Resume publishes the frozen draft; no second model call.
    messages = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert len(messages) == 2
    assert sum(message["content"] == "A private draft for the group" for message in messages) == 1
    assert (await client.get(url, headers=owner_headers)).json() == []
    assert (await client.post(decision_url, headers=owner_headers, json={
        "approve": True, "expected_digest": approvals[0]["digest"],
    })).status_code == 409

    second = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Review Agent another answer", "mentioned_agent_binding_ids": [bound.json()["id"]],
    })
    assert second.status_code == 201, second.text
    second_run_id = queued[-1]
    await room_agent_runtime.execute_room_agent_run(second_run_id)
    second_draft = (await client.get(url, headers=owner_headers)).json()
    assert len(second_draft) == 1 and second_draft[0]["run_id"] == second_run_id
    rejected = await client.post(f"{url}/{second_run_id}/decision", headers=owner_headers, json={
        "approve": False, "expected_digest": second_draft[0]["digest"],
    })
    assert rejected.status_code == 200, rejected.text
    await room_agent_runtime.execute_room_agent_run(second_run_id)
    messages = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert len(messages) == 3  # Two user messages, only the approved AI reply.
    assert next(message for message in messages if message["id"] == second.json()["id"])["agent_runs"][0]["status"] == "interrupted"

    third = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Review Agent one more", "mentioned_agent_binding_ids": [bound.json()["id"]],
    })
    assert third.status_code == 201, third.text
    third_run_id = queued[-1]
    await room_agent_runtime.execute_room_agent_run(third_run_id)
    third_draft = (await client.get(url, headers=owner_headers)).json()[0]
    assert (await client.post(f"{url}/{third_run_id}/decision", headers=owner_headers, json={
        "approve": True, "expected_digest": third_draft["digest"],
    })).status_code == 200
    from packages.core import humans

    async def authority_revoked(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(humans, "participant_can", authority_revoked)
    await room_agent_runtime.execute_room_agent_run(third_run_id)
    messages = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert len(messages) == 4
    assert next(message for message in messages if message["id"] == third.json()["id"])["agent_runs"][0]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_group_agent_credit_preflight_failure_never_calls_model_or_publishes(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms
    from packages.core.ai.llm_client import CreditExhaustedError
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, owner = await _register(client, "mention_no_credits_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Credit gate", "owner_scope": "personal",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "@Manor AI answer", "mentioned_agent_binding_ids": [created.json()["agents"][0]["id"]],
    })
    assert sent.status_code == 201, sent.text
    run_id = (await db_session.execute(select(ChatRoomAgentRun.id))).scalar_one()
    checked = []
    completions = []

    original_resolve = room_agent_runtime.RuntimeResolver.resolve_trace_envelope

    def resolve(resolver, request, **kwargs):
        return original_resolve(resolver, request, **kwargs)

    async def no_credit(entity_id, **kwargs):
        checked.append((entity_id, kwargs))
        raise CreditExhaustedError("No credits")

    async def fake_completion(**kwargs):
        completions.append(kwargs)
        return SimpleNamespace(content="must not appear")

    monkeypatch.setattr(room_agent_runtime.RuntimeResolver, "resolve_trace_envelope", resolve)
    monkeypatch.setattr(room_agent_runtime, "runtime_assert_credit_available", no_credit)
    monkeypatch.setattr(room_agent_runtime, "runtime_execute_chat_agent_loop", fake_completion)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert checked and checked[0][0] == owner["entity_id"]
    assert completions == []
    visible = (await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers)).json()
    assert [message["id"] for message in visible] == [sent.json()["id"]]
    assert visible[0]["agent_runs"][0]["status"] == "failed"
    db_session.expire_all()
    run = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert run.error_code == "CreditExhaustedError"
    assert run.response_message_id is None


@pytest.mark.asyncio
async def test_group_agent_completion_uses_customer_surface_and_owner_billing(client: AsyncClient, monkeypatch):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import room_agent_runtime

    owner_headers, owner = await _register(client, "mention_surface_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Tool-free boundary", "owner_scope": "entity",
        "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    enabled = await client.patch(
        f"/api/v1/chat/rooms/{created.json()['id']}/ai-execution",
        headers=owner_headers, json={"enabled": True, "expected_revision": 0},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["ai_tool_execution_enabled"] is True

    observed = {}

    original_resolve = room_agent_runtime.RuntimeResolver.resolve_trace_envelope

    def resolve(resolver, request, **kwargs):
        observed["request"] = request
        observed["resolution"] = kwargs
        return original_resolve(resolver, request, **kwargs)

    async def check_credit(entity_id, **kwargs):
        observed["credit"] = (entity_id, kwargs)

    @asynccontextmanager
    async def bill(entity_id, **kwargs):
        observed["billing"] = (entity_id, kwargs)
        yield

    async def complete(**kwargs):
        observed["completion"] = kwargs
        return SimpleNamespace(content="  Safe reply.  ")

    monkeypatch.setattr(room_agent_runtime.RuntimeResolver, "resolve_trace_envelope", resolve)
    monkeypatch.setattr(room_agent_runtime, "runtime_assert_credit_available", check_credit)
    monkeypatch.setattr(room_agent_runtime, "runtime_llm_billing_context", bill)
    monkeypatch.setattr(room_agent_runtime, "runtime_execute_chat_agent_loop", complete)
    turn = room_agent_runtime.RoomAgentTurn(
        run_id=generate_ulid(), room_id=created.json()["id"], source_message_id=generate_ulid(),
        binding_id=generate_ulid(), entity_id=owner["entity_id"], agent_id=generate_ulid(),
        workspace_id=generate_ulid(), agent_name="Customer Helper", agent_avatar_url=None,
        model=None, temperature=None, max_tokens=None,
        requester_label="P2", requester_name="Alex",
        current_message="What is in this group?", history=[{"role": "user", "content": "A visible room message."}],
    )
    assert await room_agent_runtime.run_room_agent_completion(turn) == "Safe reply."
    assert observed["request"].surface is ChatSurface.PUBLIC_CUSTOMER_CHAT
    assert observed["request"].entity_id == turn.entity_id
    assert observed["request"].user_id is None
    assert json.loads(observed["completion"]["user_message"]) == {
        "sender_kind": "person", "sender_label": "P2", "sender_name": "Alex",
        "message": "What is in this group?",
    }
    assert observed["request"].input_preview == observed["completion"]["user_message"]
    assert observed["request"].workspace_id == turn.workspace_id
    assert observed["resolution"]["tool_schemas"] == []
    assert observed["resolution"]["allowed_tool_names"] == set()
    assert observed["credit"][0] == turn.entity_id
    assert observed["billing"][0] == turn.entity_id
    assert observed["billing"][1]["user_id"] is None
    assert observed["completion"]["tools"] == []
    assert observed["completion"]["allowed_tool_names"] == set()
    assert observed["completion"]["conversation_id"] is None
    assert observed["completion"]["initial_messages"] == turn.history


@pytest.mark.asyncio
async def test_external_guest_can_address_exact_workspace_agent_without_membership(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    owner_headers, owner = await _register(client, "mention_external_owner")
    guest_headers, guest = await _register(client, "mention_external_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Customer support", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    agent = Agent(entity_id=owner["entity_id"], owner_user_id=owner["id"], name="Support Agent", status="active")
    workspace = Workspace(entity_id=owner["entity_id"], name="Support Workspace", status="active")
    db_session.add_all([agent, workspace])
    await db_session.flush()
    subscription = AgentSubscription(
        entity_id=owner["entity_id"], agent_id=agent.id,
        workspace_id=workspace.id, status="active",
    )
    db_session.add(subscription)
    await db_session.commit()
    await _allow_workspace_room_reply(db_session, owner, workspace.id, room_id)
    bound = await client.post(f"/api/v1/chat/rooms/{room_id}/agents", headers=owner_headers,
                              json={"subscription_id": subscription.id})
    assert bound.status_code == 201, bound.text
    guest_user = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    guest_user.verified_email = guest["email"]
    guest_user.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    assert (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id == guest["id"], UserMembership.entity_id == owner["entity_id"],
    ))).scalars().all() == []

    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers, json={
        "content": "@Support Agent answer from this group only",
        "mentioned_agent_binding_ids": [bound.json()["id"]],
    })
    assert sent.status_code == 201, sent.text
    run_id = (await db_session.execute(select(ChatRoomAgentRun.id))).scalar_one()
    turns = []

    async def fake_completion(turn):
        turns.append(turn)
        return "A customer-safe answer."

    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", fake_completion)
    await room_agent_runtime.execute_room_agent_run(run_id)
    assert len(turns) == 1
    assert turns[0].entity_id == owner["entity_id"]
    assert turns[0].agent_id == agent.id
    assert turns[0].workspace_id == workspace.id
    assert turns[0].room_id == room_id
    visible = await client.get(f"/api/v1/chat/rooms/{room_id}/messages", headers=guest_headers)
    assert visible.status_code == 200, visible.text
    assert visible.json()[-1]["content"] == "A customer-safe answer."
    assert visible.json()[-1]["sender_agent_name"] == "Support Agent"


@pytest.mark.asyncio
async def test_room_presence_uses_live_sessions_and_only_exposes_current_members(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "presence_owner")
    guest_headers, guest = await _register(client, "presence_guest")
    outsider_headers, outsider = await _register(client, "presence_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "External team", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    url = f"/api/v1/chat/rooms/{room_id}/presence"
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    assert (await client.get(url, headers=outsider_headers)).status_code == 404

    now = datetime.now(timezone.utc)
    sessions = [
        UserSessionLog(entity_id=person["entity_id"], user_id=person["id"],
                       status="active", last_seen_at=now)
        for person in (owner, guest, outsider)
    ]
    db_session.add_all(sessions)
    await db_session.flush()
    leases = [
        UserSessionLease(id=f"presence-{index}", entity_id=person["entity_id"],
                         user_id=person["id"], session_id=session.id,
                         expires_at=now + timedelta(seconds=60))
        for index, (person, session) in enumerate(zip((owner, guest, outsider), sessions))
    ]
    db_session.add_all(leases)
    await db_session.commit()
    before_join = await client.get(url, headers=owner_headers)
    assert before_join.status_code == 200
    assert before_join.headers["cache-control"] == "private, no-store"
    assert before_join.json() == {"online_user_ids": [owner["id"]]}
    owner_list = await client.get("/api/v1/chat/rooms", headers=owner_headers)
    assert owner_list.headers["cache-control"] == "private, no-store"
    owner_rooms = owner_list.json()
    assert next(room for room in owner_rooms if room["id"] == room_id)["online_user_ids"] == [owner["id"]]

    saved_guest = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    saved_guest.verified_email = guest["email"]
    saved_guest.email_verified_at = now
    await db_session.commit()
    joined = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                               json={"token": created.json()["invite_token"]})
    assert joined.status_code == 200, joined.text
    assert (await client.get(url, headers=guest_headers)).json() == {
        "online_user_ids": sorted([owner["id"], guest["id"]]),
    }
    guest_rooms = (await client.get("/api/v1/chat/rooms", headers=guest_headers)).json()
    assert next(room for room in guest_rooms if room["id"] == room_id)["online_user_ids"] == sorted([
        owner["id"], guest["id"],
    ])
    assert (await client.get("/api/v1/chat/rooms", headers=outsider_headers)).json() == []

    leases[1].expires_at = now - timedelta(seconds=1)
    await db_session.commit()
    assert (await client.get(url, headers=owner_headers)).json() == {
        "online_user_ids": [owner["id"]],
    }
    removed = await client.delete(f"/api/v1/chat/rooms/{room_id}/members/{guest['id']}",
                                  headers=owner_headers)
    assert removed.status_code == 204
    assert (await client.get(url, headers=guest_headers)).status_code == 404
    sessions[0].status = "closed"
    sessions[0].ended_at = now
    await db_session.commit()
    assert (await client.get(url, headers=owner_headers)).json() == {"online_user_ids": []}


@pytest.mark.asyncio
async def test_three_group_members_chat_and_presence_without_exposing_outsider(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "three_presence_owner")
    guest_headers, guest = await _register(client, "three_presence_guest")
    third_headers, third = await _register(client, "three_presence_third")
    outsider_headers, outsider = await _register(client, "three_presence_outsider")
    assert guest["entity_id"] != owner["entity_id"]

    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Three-person room", "owner_scope": "entity",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    invited_third = await client.post(f"/api/v1/chat/rooms/{room_id}/invites", headers=owner_headers,
                                      json={"email": third["email"]})
    assert invited_third.status_code == 201, invited_third.text

    now = datetime.now(timezone.utc)
    for person in (guest, third):
        saved = (await db_session.execute(select(User).where(User.id == person["id"]))).scalar_one()
        saved.verified_email = person["email"]
        saved.email_verified_at = now
    await db_session.commit()
    for headers, token in ((guest_headers, created.json()["invite_token"]),
                           (third_headers, invited_third.json()["invite_token"])):
        joined = await client.post("/api/v1/chat/rooms/invites/accept", headers=headers,
                                   json={"token": token})
        assert joined.status_code == 200, joined.text

    people = (owner, guest, third, outsider)
    sessions = [UserSessionLog(entity_id=person["entity_id"], user_id=person["id"],
                               status="active", last_seen_at=now) for person in people]
    db_session.add_all(sessions)
    await db_session.flush()
    leases = [UserSessionLease(id=f"three-presence-{index}", entity_id=person["entity_id"],
                               user_id=person["id"], session_id=session.id,
                               expires_at=now + timedelta(seconds=60))
              for index, (person, session) in enumerate(zip(people, sessions))]
    db_session.add_all(leases)
    await db_session.commit()

    expected_online = sorted([owner["id"], guest["id"], third["id"]])
    presence_url = f"/api/v1/chat/rooms/{room_id}/presence"
    messages_url = f"/api/v1/chat/rooms/{room_id}/messages"
    for headers in (owner_headers, guest_headers, third_headers):
        presence = await client.get(presence_url, headers=headers)
        assert presence.status_code == 200, presence.text
        assert presence.json() == {"online_user_ids": expected_online}
        rooms = (await client.get("/api/v1/chat/rooms", headers=headers)).json()
        listed_room = next(room for room in rooms if room["id"] == room_id)
        assert listed_room["member_count"] == 3
        assert listed_room["online_user_ids"] == expected_online
    assert (await client.get(presence_url, headers=outsider_headers)).status_code == 404
    assert (await client.get(messages_url, headers=outsider_headers)).status_code == 404
    assert (await client.get("/api/v1/chat/rooms", headers=outsider_headers)).json() == []

    for headers, content in ((owner_headers, "From owner"), (guest_headers, "From guest"),
                             (third_headers, "From third")):
        sent = await client.post(messages_url, headers=headers, json={"content": content})
        assert sent.status_code == 201, sent.text
    for headers in (owner_headers, guest_headers, third_headers):
        visible = await client.get(messages_url, headers=headers)
        assert visible.status_code == 200, visible.text
        assert sorted(message["content"] for message in visible.json()) == [
            "From guest", "From owner", "From third",
        ]

    leases[2].expires_at = now - timedelta(seconds=1)
    await db_session.commit()
    expected_online = sorted([owner["id"], guest["id"]])
    assert (await client.get(presence_url, headers=owner_headers)).json() == {
        "online_user_ids": expected_online,
    }
    rooms = (await client.get("/api/v1/chat/rooms", headers=third_headers)).json()
    assert next(room for room in rooms if room["id"] == room_id)["online_user_ids"] == expected_online


@pytest.mark.asyncio
async def test_group_agent_delivery_recovers_pending_but_never_replays_stale_running(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms
    from packages.core.tasks import room_agent_tasks

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", lambda _run_id: True)
    monkeypatch.setattr(
        room_agent_tasks, "async_session",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    delivered: list[str] = []
    monkeypatch.setattr(room_agent_tasks, "enqueue_room_agent_run", delivered.append)

    headers, _ = await _register(client, "room_delivery_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Delivery", "owner_scope": "personal",
        "recipient_email": "delivery-guest@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=headers, json={
        "content": "@Manor AI hello", "mentioned_agent_binding_ids": [binding_id],
    })
    assert sent.status_code == 201, sent.text
    run_id = sent.json()["agent_runs"][0]["id"]
    run = (await db_session.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
    ))).scalar_one()
    now = datetime.now(timezone.utc)
    run.created_at = now - timedelta(minutes=3)
    run.last_enqueued_at = now - timedelta(minutes=2)
    await db_session.commit()

    assert await room_agent_tasks.recover_room_agent_runs(now=now) == (1, 0)
    assert delivered == [run_id]
    await db_session.refresh(run)
    assert run.last_enqueued_at == now
    assert await room_agent_tasks.recover_room_agent_runs(now=now) == (0, 0)

    run.status = "running"
    run.started_at = now - timedelta(minutes=13)
    await db_session.commit()
    assert await room_agent_tasks.recover_room_agent_runs(now=now) == (0, 1)
    await db_session.refresh(run)
    assert run.status == "interrupted"
    assert run.error_code == "worker_lost"
    assert delivered == [run_id]


@pytest.mark.asyncio
async def test_concurrent_group_mentions_keep_distinct_sender_and_source(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", lambda _run_id: True)
    headers, owner = await _register(client, "room_concurrent_owner")
    guest_headers, guest = await _register(client, "room_concurrent_guest")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Concurrent turns", "owner_scope": "personal",
        "recipient_email": "concurrent-guest@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    responses = await asyncio.gather(*(
        client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=(headers if number % 2 == 0 else guest_headers), json={
            "content": f"@Manor AI request {number}",
            "mentioned_agent_binding_ids": [binding_id],
        })
        for number in range(12)
    ))
    assert all(response.status_code == 201 for response in responses), [
        response.text for response in responses if response.status_code != 201
    ]
    message_ids = {response.json()["id"] for response in responses}
    run_ids = {response.json()["agent_runs"][0]["id"] for response in responses}
    assert len(message_ids) == len(run_ids) == 12
    runs = (await db_session.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.room_id == room_id,
    ))).scalars().all()
    assert {run.source_message_id for run in runs} == message_ids
    assert {run.id for run in runs} == run_ids
    messages = (await db_session.execute(select(ChatRoomMessage).where(
        ChatRoomMessage.id.in_(message_ids),
    ))).scalars().all()
    assert {message.sender_user_id for message in messages} == {owner["id"], guest["id"]}
    for message in messages:
        number = int(message.content.rsplit(" ", 1)[1])
        assert message.sender_user_id == (owner["id"] if number % 2 == 0 else guest["id"])


@pytest.mark.asyncio
async def test_group_message_history_search_and_concurrent_senders(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "room_history_owner")
    guest_headers, guest = await _register(client, "room_history_guest")
    outsider_headers, _ = await _register(client, "room_history_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "History", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    base = f"/api/v1/chat/rooms/{room_id}/messages"
    before_join = await client.post(base, headers=owner_headers, json={"content": "private-before-join"})
    assert before_join.status_code == 201
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    responses = await asyncio.wait_for(asyncio.gather(*(
        client.post(base, headers=owner_headers if number % 2 == 0 else guest_headers,
                    json={"content": f"searchable message {number:03d}"})
        for number in range(60)
    )), timeout=30)
    assert all(response.status_code == 201 for response in responses), [
        response.text for response in responses if response.status_code != 201
    ]
    assert len({response.json()["id"] for response in responses}) == 60
    newest = await client.get(base, headers=guest_headers)
    assert newest.status_code == 200
    assert len(newest.json()) == 50
    older = await client.get(base, headers=guest_headers, params={"before": newest.json()[0]["id"]})
    assert older.status_code == 200
    assert len(older.json()) == 10
    assert len({message["id"] for message in newest.json() + older.json()}) == 60
    assert all(message["content"] != "private-before-join" for message in newest.json() + older.json())
    cursor = older.json()[0]["id"]
    caught_up: list[dict] = []
    while True:
        page = await client.get(base, headers=guest_headers, params={"after": cursor, "limit": 17})
        assert page.status_code == 200, page.text
        if not page.json():
            break
        caught_up.extend(page.json())
        cursor = page.json()[-1]["id"]
    assert len(caught_up) == 59
    assert len({message["id"] for message in caught_up}) == 59
    assert (await client.get(base, headers=guest_headers, params={"after": before_join.json()["id"]})).status_code == 404
    assert (await client.get(base, headers=guest_headers, params={"after": cursor, "before": cursor})).status_code == 422

    found = await client.get(base, headers=guest_headers, params={"q": "message 02"})
    assert found.status_code == 200
    assert {item["content"] for item in found.json()} == {
        f"searchable message {number:03d}" for number in range(20, 30)
    }
    assert (await client.get(base, headers=guest_headers, params={"q": "private-before-join"})).json() == []
    assert (await client.get(base, headers=guest_headers, params={"q": "%"})).json() == []
    assert (await client.get(base, headers=outsider_headers, params={"q": "searchable"})).status_code == 404
    room_list = (await client.get("/api/v1/chat/rooms", headers=owner_headers)).json()
    assert next(item for item in room_list if item["id"] == room_id)["last_message_at"] is not None
    assert owner["id"] != guest["id"]


@pytest.mark.asyncio
async def test_hundred_member_group_accepts_twenty_parallel_messages(client: AsyncClient, db_session):
    owner_headers, owner = await _register(client, "room_load_owner")
    guest_headers, guest = await _register(client, "room_load_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Load boundary", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    now = datetime.now(timezone.utc)
    members = [ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=now,
    )]
    for number in range(98):
        extra_id = generate_ulid()
        db_session.add(User(
            id=extra_id, entity_id=owner["entity_id"],
            email=f"room-load-{room_id}-{number}@example.com", password_hash="unused",
            display_name=f"Load member {number}", role="member", status="active",
        ))
        members.append(ChatRoomMember(
            id=generate_ulid(), room_id=room_id, user_id=extra_id,
            role="member", status="active", visible_from=now,
        ))
    await db_session.flush()  # Persist synthetic users before their member FKs.
    db_session.add_all(members)
    await db_session.commit()
    base = f"/api/v1/chat/rooms/{room_id}/messages"
    started = perf_counter()
    responses = await asyncio.wait_for(asyncio.gather(*(
        client.post(base, headers=owner_headers if number % 2 == 0 else guest_headers,
                    json={"content": f"burst {number:02d}"})
        for number in range(20)
    )), timeout=30)
    elapsed = perf_counter() - started
    print(f"20 parallel sends in a 100-member group: {elapsed:.2f}s ({20 / elapsed:.1f} messages/s)")
    assert all(response.status_code == 201 for response in responses), [
        response.text for response in responses if response.status_code != 201
    ]
    assert len({response.json()["id"] for response in responses}) == 20
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)).json()["member_count"] == 100
    history = (await client.get(base, headers=guest_headers)).json()
    assert {item["content"] for item in history} == {f"burst {number:02d}" for number in range(20)}


ROOM_CALL = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_search", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"RV park\"}"}},
]}
ROOM_OUTPUT = {"role": "tool", "tool_call_id": "call_search", "content": "Listing 0: $800/month, pets allowed"}


def _room_agent_loop(db_session, monkeypatch) -> list[dict]:
    """A room Agent whose every turn runs one tool, recording what it was given."""
    from packages.core.services import room_agent_runtime

    _isolate_room_agent_worker(db_session, monkeypatch)
    seen: list[dict] = []

    async def check_credit(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def bill(*_args, **_kwargs):
        yield

    async def complete(**kwargs):
        seen.append(kwargs)
        reply = f"Answer {len(seen)}"
        return SimpleNamespace(content=reply, stop_reason="end_turn", messages=[
            {"role": "system", "content": kwargs["system_prompt"]},
            *(kwargs["initial_messages"] or []),
            {"role": "user", "content": kwargs["user_message"]},
            ROOM_CALL, ROOM_OUTPUT, {"role": "assistant", "content": reply},
        ])

    monkeypatch.setattr(room_agent_runtime, "runtime_assert_credit_available", check_credit)
    monkeypatch.setattr(room_agent_runtime, "runtime_llm_billing_context", bill)
    monkeypatch.setattr(room_agent_runtime, "runtime_execute_chat_agent_loop", complete)
    return seen


async def _mention_and_run(client, db_session, headers, room_id, binding_id, content):
    from packages.core.services import room_agent_runtime

    sent = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=headers, json={
        "content": f"@Manor AI {content}", "mentioned_agent_binding_ids": [binding_id],
    })
    assert sent.status_code == 201, sent.text
    run_id = (await db_session.execute(select(ChatRoomAgentRun.id).where(
        ChatRoomAgentRun.source_message_id == sent.json()["id"],
    ))).scalar_one()
    await room_agent_runtime.execute_room_agent_run(run_id)
    db_session.expire_all()
    run = (await db_session.execute(select(ChatRoomAgentRun).where(ChatRoomAgentRun.id == run_id))).scalar_one()
    assert run.status == "succeeded", run.error_code
    return sent.json()["id"]


async def _room_transcript_rows(db_session, room_id) -> int:
    from sqlalchemy import func

    from packages.core.models.conversation_transcript import ConversationTranscriptItem

    return (await db_session.execute(select(func.count()).select_from(ConversationTranscriptItem).where(
        ConversationTranscriptItem.room_id == room_id,
    ))).scalar_one()


@pytest.mark.asyncio
async def test_group_agent_reads_its_previous_tool_output_and_forgets_retracted_messages(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    seen = _room_agent_loop(db_session, monkeypatch)
    owner_headers, _owner = await _register(client, "room_transcript_owner")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Trip", "owner_scope": "personal", "recipient_email": "unjoined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id, binding_id = created.json()["id"], created.json()["agents"][0]["id"]

    first = await _mention_and_run(client, db_session, owner_headers, room_id, binding_id, "find an RV park")
    await _mention_and_run(client, db_session, owner_headers, room_id, binding_id, "does it allow pets?")

    # The follow-up sees the earlier tool output, not just the published reply.
    assert ROOM_OUTPUT in seen[1]["initial_messages"] and ROOM_CALL in seen[1]["initial_messages"]
    assert "find an RV park" in json.dumps(seen[1]["initial_messages"], ensure_ascii=False)

    retracted = await client.delete(f"/api/v1/chat/rooms/{room_id}/messages/{first}", headers=owner_headers)
    assert retracted.status_code == 204, retracted.text
    assert await _room_transcript_rows(db_session, room_id) == 0

    await _mention_and_run(client, db_session, owner_headers, room_id, binding_id, "anything cheaper?")
    rebuilt = json.dumps(seen[2]["initial_messages"], ensure_ascii=False)
    assert "find an RV park" not in rebuilt and ROOM_OUTPUT["content"] not in rebuilt
    assert "does it allow pets?" in rebuilt


@pytest.mark.asyncio
async def test_group_agent_context_restarts_for_a_new_audience_and_ends_with_the_group(
    client: AsyncClient, db_session, monkeypatch,
):
    from apps.api.routers import chat_rooms

    async def hold_run(_run_id: str) -> None:
        pass

    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", hold_run)
    seen = _room_agent_loop(db_session, monkeypatch)
    owner_headers, _owner = await _register(client, "room_audience_owner")
    guest_headers, guest = await _register(client, "room_audience_guest")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Before you joined", "owner_scope": "personal", "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id, binding_id = created.json()["id"], created.json()["agents"][0]["id"]

    await _mention_and_run(client, db_session, owner_headers, room_id, binding_id, "private budget is 900")
    assert await _room_transcript_rows(db_session, room_id) > 0

    guest_user = (await db_session.execute(select(User).where(User.id == guest["id"]))).scalar_one()
    guest_user.verified_email = guest["email"]
    guest_user.email_verified_at = datetime.now(timezone.utc)
    await db_session.commit()
    accepted = await client.post("/api/v1/chat/rooms/invites/accept", headers=guest_headers,
                                 json={"token": created.json()["invite_token"]})
    assert accepted.status_code == 200, accepted.text
    assert await _room_transcript_rows(db_session, room_id) == 0

    await _mention_and_run(client, db_session, owner_headers, room_id, binding_id, "what should we book?")
    new_audience = json.dumps(seen[1]["initial_messages"], ensure_ascii=False)
    assert "private budget is 900" not in new_audience and ROOM_OUTPUT["content"] not in new_audience
    assert await _room_transcript_rows(db_session, room_id) > 0

    closed = await client.delete(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)
    assert closed.status_code == 204, closed.text
    assert await _room_transcript_rows(db_session, room_id) == 0


@pytest.mark.asyncio
async def test_room_list_reads_rooms_together_and_shows_each_room_as_its_page_does(
    client: AsyncClient, db_session,
):
    owner_headers, _ = await _register(client, "room_list_owner")
    guests = [(await _register(client, f"room_list_guest_{index}"))[1] for index in range(3)]

    async def create(guest: dict) -> str:
        created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
            "kind": "group", "title": f"Room {guest['email']}", "owner_scope": "entity",
            "recipient_email": guest["email"],
        })
        assert created.status_code == 201, created.text
        db_session.add(ChatRoomMember(
            id=generate_ulid(), room_id=created.json()["id"], user_id=guest["id"],
            role="member", status="active", visible_from=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        return created.json()["id"]

    async def listed() -> tuple[list[dict], int]:
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args):
            statements.append(statement)

        from packages.core import database

        engine = database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await client.get("/api/v1/chat/rooms", headers=owner_headers)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200, response.text
        return response.json(), len(statements)

    first = await create(guests[0])
    _, one_room_statements = await listed()
    room_ids = [first, *[await create(guest) for guest in guests[1:]]]
    posted = await client.post(f"/api/v1/chat/rooms/{room_ids[1]}/messages", headers=owner_headers,
                               json={"content": "Latest"})
    assert posted.status_code == 201, posted.text

    rooms, three_room_statements = await listed()

    assert three_room_statements == one_room_statements > 0
    assert sorted(room["id"] for room in rooms) == sorted(room_ids)
    for room in rooms:
        page = (await client.get(f"/api/v1/chat/rooms/{room['id']}", headers=owner_headers)).json()
        assert {key: room[key] for key in page} == page
        assert len(room["members"]) == 2 and room["agents"]
        assert (room["last_message_at"] is not None) == (room["id"] == room_ids[1])
