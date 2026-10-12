"""Group file mutations require one durable, exact owner/admin approval."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tests.auth_helpers import register_user_and_get_token
from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import (
    ChatRoom, ChatRoomAgentRun, ChatRoomMember, ChatRoomMessage,
)
from packages.core.services.room_tool_approval_service import (
    RoomToolApprovalError, claim_approved_room_tool_call, claim_direct_room_tool_call,
    finish_room_tool_call, park_room_tool_approval, request_room_tool_approval,
)


async def _register(client: AsyncClient, name: str) -> tuple[dict[str, str], dict]:
    response = await register_user_and_get_token(client, json={
        "email": f"{name}@example.com", "password": "test-password-123",
        "entity_name": f"{name} Co",
    })
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()


async def _room_mode(client, headers, room_id, mode):
    url = f"/api/v1/approval-settings/rooms/{room_id}"
    before = await client.get(url, headers=headers)
    assert before.status_code == 200, before.text
    response = await client.put(url, headers=headers, json={
        "expected_version": before.json()["version"], "draft": {"mode": mode, "user_rules": []},
    })
    assert response.status_code == 200, response.text
    return response.json()


async def _approval_setup(client, db_session, monkeypatch):
    from apps.api.routers import chat_rooms
    from packages.core.services import room_agent_runtime

    async def admitted(_db, _run):
        return object(), None

    async def no_worker(_run_id):
        return None

    monkeypatch.setattr(room_agent_runtime, "_admit_run", admitted)
    monkeypatch.setattr(chat_rooms, "enqueue_room_agent_run", no_worker)
    owner_headers, owner = await _register(client, "tool_approval_owner")
    admin_headers, admin = await _register(client, "tool_approval_admin")
    member_headers, member = await _register(client, "tool_approval_member")
    outsider_headers, _ = await _register(client, "tool_approval_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "Tool approvals", "owner_scope": "personal",
        "recipient_email": admin["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    now = datetime.now(timezone.utc)
    for actor, role in ((admin, "admin"), (member, "member")):
        db_session.add(ChatRoomMember(
            id=generate_ulid(), room_id=room_id, user_id=actor["id"],
            role=role, status="active", visible_from=now,
        ))
    await db_session.commit()
    enabled = await client.patch(f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=owner_headers, json={
        "enabled": True, "expected_revision": 0,
    })
    assert enabled.status_code == 200, enabled.text
    await _room_mode(client, owner_headers, room_id, "confirm_changes")
    return room_id, (owner_headers, owner), (admin_headers, admin), (member_headers, member), outsider_headers


async def _waiting_run(db_session, room_id: str, owner_id: str, *, revision=1):
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner_id,
        content="Create a group file", mentioned_user_ids=[],
    )
    db_session.add(source)
    await db_session.flush()
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=generate_ulid(), agent_name="Manor AI", status="running",
        audience_fingerprint="a" * 64, ai_tool_execution_revision=revision,
    )
    db_session.add(run)
    await db_session.commit()
    arguments = {"name": "report.txt", "content": "room-safe content"}
    approval = await request_room_tool_approval(
        db_session, run_id=run.id, tool_call_id="call-1", tool_name="room_create_file",
        arguments=arguments, display_summary="Create report.txt in this group",
    )
    await db_session.commit()
    await park_room_tool_approval(db_session, run_id=run.id, approval_id=approval.id, checkpoint={
        "schema_version": 4,
        "pending_tool_call": {"id": "call-1", "name": "room_create_file", "arguments": arguments},
    })
    await db_session.commit()
    return run, approval, arguments


async def _running_run(db_session, room_id: str, sender_id: str, *, revision: int):
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=sender_id,
        content="Create a group file", mentioned_user_ids=[],
    )
    db_session.add(source)
    await db_session.flush()
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=generate_ulid(), agent_name="Manor AI", status="running",
        audience_fingerprint="a" * 64, ai_tool_execution_revision=revision,
    )
    db_session.add(run)
    await db_session.commit()
    return run


@pytest.mark.asyncio
async def test_direct_room_file_claim_is_durable_exact_and_single_execution(
    client, db_session, monkeypatch,
):
    room_id, (owner_headers, owner), _, (_, member), _ = (
        await _approval_setup(client, db_session, monkeypatch)
    )
    legacy_run = await _running_run(db_session, room_id, owner["id"], revision=1)
    arguments = {"name": "report.txt", "content": "group-only"}
    with pytest.raises(RoomToolApprovalError, match="approval_required"):
        await claim_direct_room_tool_call(
            db_session, run_id=legacy_run.id, tool_call_id="legacy-call",
            tool_name="room_create_file", arguments=arguments,
            display_summary="Create report.txt",
        )
    await db_session.commit()
    await _room_mode(client, owner_headers, room_id, "automatic")
    room = await db_session.get(ChatRoom, room_id, populate_existing=True)
    assert room.ai_tool_execution_revision == 1
    run = await _running_run(db_session, room_id, owner["id"], revision=1)
    receipt = await claim_direct_room_tool_call(
        db_session, run_id=run.id, tool_call_id="direct-call",
        tool_name="room_create_file", arguments=arguments,
        display_summary="Create report.txt",
    )
    assert receipt.status == "claimed"
    assert receipt.decided_by_user_id == owner["id"]
    assert receipt.decided_at and receipt.claimed_at
    with pytest.raises(RoomToolApprovalError, match="tool_call_already_decided"):
        await claim_direct_room_tool_call(
            db_session, run_id=run.id, tool_call_id="direct-call",
            tool_name="room_create_file", arguments=arguments,
            display_summary="Create report.txt",
        )
    await db_session.commit()
    with pytest.raises(RoomToolApprovalError, match="tool_call_already_decided"):
        await claim_direct_room_tool_call(
            db_session, run_id=run.id, tool_call_id="alternate-id",
            tool_name="room_create_file", arguments=arguments,
            display_summary="Create report.txt",
        )
    await db_session.commit()
    await finish_room_tool_call(
        db_session, approval_id=receipt.id, succeeded=True,
        result_document_id=generate_ulid(),
    )
    with pytest.raises(RoomToolApprovalError, match="tool_call_already_decided"):
        await claim_direct_room_tool_call(
            db_session, run_id=run.id, tool_call_id="alternate-id",
            tool_name="room_create_file", arguments=arguments,
            display_summary="Create report.txt",
        )
    await db_session.commit()
    member_run = await _running_run(db_session, room_id, member["id"], revision=1)
    member_receipt = await claim_direct_room_tool_call(
        db_session, run_id=member_run.id, tool_call_id="member-call",
        tool_name="room_create_file", arguments=arguments,
        display_summary="Create report.txt",
    )
    assert member_receipt.status == "claimed"
    assert member_receipt.decided_by_user_id == member["id"]


@pytest.mark.asyncio
async def test_automatic_mode_preserves_existing_pending_room_approval(
    client, db_session, monkeypatch,
):
    room_id, (owner_headers, owner), (admin_headers, _), _, _ = (
        await _approval_setup(client, db_session, monkeypatch)
    )
    run, approval, arguments = await _waiting_run(db_session, room_id, owner["id"])
    await _room_mode(client, owner_headers, room_id, "automatic")
    await db_session.refresh(run)
    await db_session.refresh(approval)
    assert run.status == "waiting_approval"
    assert run.checkpoint["pending_tool_call"]["id"] == approval.tool_call_id
    assert approval.status == "pending"
    with pytest.raises(RoomToolApprovalError, match="run_not_running"):
        await claim_direct_room_tool_call(db_session, run_id=run.id, tool_call_id="call-1",
            tool_name="room_create_file", arguments=arguments, display_summary="Create report.txt")
    await db_session.commit()
    response = await client.post(
        f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals/{approval.id}/decision",
        headers=admin_headers, json={"approve": True},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(approval)
    assert approval.status == "approved"


@pytest.mark.asyncio
async def test_room_tool_approval_requires_admin_and_claims_exact_call_once(client, db_session, monkeypatch):
    room_id, (owner_headers, owner), (admin_headers, admin), (member_headers, _), outsider_headers = (
        await _approval_setup(client, db_session, monkeypatch)
    )
    run, approval, arguments = await _waiting_run(db_session, room_id, owner["id"])
    url = f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals/{approval.id}/decision"
    listing = await client.get(f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals", headers=admin_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()[0]["summary"] == "Create report.txt in this group"
    assert "arguments" not in listing.text and "room-safe content" not in listing.text
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals",
                             headers=member_headers)).status_code == 403
    assert (await client.get(f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals",
                             headers=outsider_headers)).status_code == 404
    assert (await client.post(url, headers=member_headers, json={"approve": True})).status_code == 403
    assert (await client.post(url, headers=outsider_headers, json={"approve": True})).status_code == 404
    first, duplicate = await asyncio.gather(
        client.post(url, headers=admin_headers, json={"approve": True}),
        client.post(url, headers=admin_headers, json={"approve": True}),
    )
    assert first.status_code == duplicate.status_code == 200, (first.text, duplicate.text)
    assert first.json()["status"] == duplicate.json()["status"] == "approved"
    audit = (await db_session.execute(select(AuditLog).where(
        AuditLog.resource_type == "chat_room_tool_approval", AuditLog.resource_id == approval.id,
    ))).scalars().all()
    assert len(audit) == 1
    assert audit[0].user_id == admin["id"]
    await db_session.refresh(run)
    await db_session.refresh(approval)
    assert run.status == "pending"
    run.status = "running"
    await db_session.commit()
    with pytest.raises(RoomToolApprovalError, match="tool_call_changed"):
        await claim_approved_room_tool_call(
            db_session, run_id=run.id, tool_call_id="call-1", tool_name="room_create_file",
            arguments={"name": "other.txt", "content": "room-safe content"},
        )
    claimed = await claim_approved_room_tool_call(
        db_session, run_id=run.id, tool_call_id="call-1", tool_name="room_create_file",
        arguments=arguments,
    )
    assert claimed.status == "claimed"
    with pytest.raises(RoomToolApprovalError, match="tool_call_not_approved"):
        await claim_approved_room_tool_call(
            db_session, run_id=run.id, tool_call_id="call-1", tool_name="room_create_file",
            arguments=arguments,
        )
    receipt = await finish_room_tool_call(
        db_session, approval_id=approval.id, succeeded=True,
        result_document_id=generate_ulid(),
    )
    assert receipt.status == "succeeded" and receipt.result_document_id
    assert (await client.post(url, headers=owner_headers, json={"approve": True})).status_code == 409


@pytest.mark.asyncio
async def test_room_tool_approval_rejects_stale_revision_role_and_binding(client, db_session, monkeypatch):
    room_id, (owner_headers, owner), (admin_headers, admin), _, _ = (
        await _approval_setup(client, db_session, monkeypatch)
    )
    run, approval, _ = await _waiting_run(db_session, room_id, owner["id"])
    url = f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals/{approval.id}/decision"
    admin_member = (await db_session.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room_id,
        ChatRoomMember.user_id == admin["id"],
    ))).scalar_one()
    admin_member.role = "member"
    await db_session.commit()
    assert (await client.post(url, headers=admin_headers, json={"approve": True})).status_code == 403
    admin_member.role = "admin"
    await db_session.commit()
    run.binding_id = generate_ulid()
    await db_session.commit()
    assert (await client.post(url, headers=admin_headers, json={"approve": True})).status_code == 409
    await db_session.refresh(approval)
    assert approval.status == "pending"
    # A fresh run may not cross an owner disable/re-enable revision fence.
    run.binding_id = approval.binding_id
    await db_session.commit()
    disabled = await client.patch(f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=owner_headers, json={
        "enabled": False, "expected_revision": 1,
    })
    assert disabled.status_code == 200, disabled.text
    enabled = await client.patch(f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=owner_headers, json={
        "enabled": True, "expected_revision": 2,
    })
    assert enabled.status_code == 200, enabled.text
    assert (await client.post(url, headers=admin_headers, json={"approve": True})).status_code == 409
    await db_session.refresh(run)
    await db_session.refresh(approval)
    assert run.status == "interrupted"
    assert run.error_code == "execution_setting_changed"
    assert approval.status == "invalidated"
    assert (await client.delete(f"/api/v1/chat/rooms/{room_id}", headers=owner_headers)).status_code == 204
    assert (await client.post(url, headers=admin_headers, json={"approve": True})).status_code == 404


@pytest.mark.asyncio
async def test_unparked_or_preenable_room_run_cannot_be_approved(client, db_session, monkeypatch):
    room_id, (_, owner), (admin_headers, _), _, _ = await _approval_setup(client, db_session, monkeypatch)
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="Old message", mentioned_user_ids=[],
    )
    db_session.add(source)
    await db_session.flush()
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=generate_ulid(), agent_name="Manor AI", status="running",
        audience_fingerprint="a" * 64, ai_tool_execution_revision=None,
    )
    db_session.add(run)
    await db_session.commit()
    with pytest.raises(RoomToolApprovalError, match="execution_setting_changed"):
        await request_room_tool_approval(
            db_session, run_id=run.id, tool_call_id="call-old", tool_name="room_create_file",
            arguments={"name": "old.txt"}, display_summary="Create old.txt",
        )
    await db_session.rollback()
    run.ai_tool_execution_revision = 1
    await db_session.commit()
    approval = await request_room_tool_approval(
        db_session, run_id=run.id, tool_call_id="call-new", tool_name="room_create_file",
        arguments={"name": "new.txt"}, display_summary="Create new.txt",
    )
    await db_session.commit()
    url = f"/api/v1/chat/rooms/{room_id}/ai-tool-approvals/{approval.id}/decision"
    assert (await client.post(url, headers=admin_headers, json={"approve": True})).status_code == 409
    await db_session.refresh(approval)
    assert approval.status == "pending"
