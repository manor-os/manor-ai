"""Public links admit authenticated users without granting any other scope.

Database/API tests only: no live model or email calls.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from packages.core.models.chat_room import ChatRoom, ChatRoomMember
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services.chat_room_invites import valid_chat_registration_invite
from test_chat_rooms import _register

ROOT = "/api/v1/chat/rooms"


async def group(client, scope="personal"):
    headers, owner = await _register(client, "public_owner")
    created = await client.post(ROOT, headers=headers, json={
        "kind": "group", "title": "Open study group", "owner_scope": scope,
        "recipient_email": "email-invite@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    enabled = await client.put(f"{ROOT}/{room_id}/public-invite", headers=headers, json={})
    assert enabled.status_code == 200, enabled.text
    return headers, owner, room_id, enabled.json()["token"], created.json()["invite_token"]


async def join(client, headers, token):
    return await client.post(f"{ROOT}/public-invites/join", headers=headers, json={"token": token})


@pytest.mark.asyncio
async def test_public_link_reusable_private_history_and_idempotent_join(client, db_session):
    owner_headers, owner, room_id, token, email_token = await group(client, "entity")
    first_headers, first = await _register(client, "public_first")
    second_headers, second = await _register(client, "public_second")
    assert (await join(client, {}, token)).status_code == 401
    assert (await client.post(f"{ROOT}/{room_id}/messages", headers=owner_headers, json={"content": "Before joining"})).status_code == 201
    joined = await join(client, first_headers, token)
    assert joined.status_code == 200, joined.text
    assert token not in json.dumps(joined.json())
    assert "public_invite_token" not in joined.json()
    assert (await client.get(f"{ROOT}/{room_id}/messages", headers=first_headers)).json() == []
    member = await db_session.scalar(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room_id, ChatRoomMember.user_id == first["id"],
    ))
    visible_from = member.visible_from
    message = await client.post(f"{ROOT}/{room_id}/messages", headers=owner_headers, json={"content": "After joining"})
    assert message.status_code == 201, message.text
    assert (await join(client, first_headers, token)).status_code == 200
    await db_session.refresh(member)
    assert member.visible_from == visible_from
    assert [m["content"] for m in (await client.get(f"{ROOT}/{room_id}/messages", headers=first_headers)).json()] == ["After joining"]
    assert (await join(client, second_headers, token)).status_code == 200
    assert (await client.get(f"{ROOT}/{room_id}/messages", headers=second_headers)).json() == []
    for guest in (first, second):
        assert guest["entity_id"] != owner["entity_id"]
        assert await db_session.scalar(select(UserMembership.id).where(
            UserMembership.user_id == guest["id"], UserMembership.entity_id == owner["entity_id"],
        )) is None
    assert (await client.post(f"{ROOT}/invites/accept", headers=second_headers, json={"token": email_token})).status_code == 403
    assert (await join(client, first_headers, email_token)).status_code == 404


@pytest.mark.asyncio
async def test_public_link_only_owner_admin_can_read_or_manage(client):
    owner_headers, _, room_id, token, _ = await group(client)
    member_headers, member = await _register(client, "public_member")
    outsider_headers, _ = await _register(client, "public_outsider")
    assert (await join(client, member_headers, token)).status_code == 200
    path = f"{ROOT}/{room_id}/public-invite"
    for method in ("GET", "PUT", "DELETE"):
        for headers, status in ((member_headers, 403), (outsider_headers, 404)):
            result = await client.request(method, path, headers=headers, **({"json": {}} if method == "PUT" else {}))
            assert result.status_code == status, result.text
    promoted = await client.patch(f"{ROOT}/{room_id}/members/{member['id']}", headers=owner_headers, json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    fetched = await client.get(path, headers=member_headers)
    assert fetched.json()["token"] == token
    assert fetched.headers["cache-control"] == "private, no-store"
    assert (await client.put(path, headers=member_headers, json={})).json()["token"] == token
    changed = (await client.put(path, headers=member_headers, json={"reset": True})).json()["token"]
    assert changed != token
    assert (await join(client, outsider_headers, token)).status_code == 404
    assert (await join(client, outsider_headers, changed)).status_code == 200
    assert (await client.delete(path, headers=member_headers)).status_code == 204
    assert (await client.get(path, headers=member_headers)).json() == {"token": None}
    assert (await join(client, outsider_headers, changed)).status_code == 404
    renewed = (await client.put(path, headers=owner_headers, json={})).json()["token"]
    assert renewed not in {token, changed}


@pytest.mark.asyncio
async def test_public_link_admits_any_signup_email_but_revoked_link_does_not(client, db_session, monkeypatch):
    owner_headers, owner, room_id, token, _ = await group(client)

    async def invite_required(_db):
        return True

    monkeypatch.setattr("packages.core.services.invitation_codes.is_required", invite_required)
    for name in ("one", "two"):
        result = await client.post("/api/v1/auth/register", json={
            "email": f"new-public-{name}@example.com", "password": "test-password-123", "chat_invite_token": token,
        })
        assert result.status_code == 200, result.text
        headers = {"Authorization": f"Bearer {result.json()['access_token']}"}
        assert (await join(client, headers, token)).status_code == 200
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        assert me["entity_id"] != owner["entity_id"]
    assert await valid_chat_registration_invite(db_session, token, "any@example.com")
    await client.delete(f"{ROOT}/{room_id}/public-invite", headers=owner_headers)
    assert not await valid_chat_registration_invite(db_session, token, "any@example.com")
    blocked = await client.post("/api/v1/auth/register", json={
        "email": "revoked-public@example.com", "password": "test-password-123", "chat_invite_token": token,
    })
    assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_public_rejoin_resets_role_and_visibility_and_concurrent_joins_are_unique(client, db_session):
    owner_headers, _, room_id, token, _ = await group(client)
    guest_headers, guest = await _register(client, "public_rejoin")
    responses = await asyncio.gather(*(join(client, guest_headers, token) for _ in range(3)))
    assert [r.status_code for r in responses] == [200, 200, 200]
    members = (await db_session.scalars(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room_id, ChatRoomMember.user_id == guest["id"],
    ))).all()
    assert len(members) == 1
    visible_from = members[0].visible_from
    await client.patch(f"{ROOT}/{room_id}/members/{guest['id']}", headers=owner_headers, json={"role": "admin"})
    assert (await client.delete(f"{ROOT}/{room_id}/membership", headers=guest_headers)).status_code == 204
    await client.post(f"{ROOT}/{room_id}/messages", headers=owner_headers, json={"content": "While away"})
    assert (await join(client, guest_headers, token)).status_code == 200
    await db_session.refresh(members[0])
    assert members[0].role == "member"
    assert members[0].visible_from > visible_from
    assert (await client.get(f"{ROOT}/{room_id}/messages", headers=guest_headers)).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["closed", "deleted_personal_owner", "inactive_personal_owner", "deleted_entity"])
async def test_unavailable_room_or_owner_blocks_join_and_signup(client, db_session, unavailable):
    scope = "entity" if unavailable == "deleted_entity" else "personal"
    _, owner, room_id, token, _ = await group(client, scope)
    guest_headers, _ = await _register(client, "public_unavailable")
    if unavailable == "closed":
        (await db_session.get(ChatRoom, room_id)).status = "closed"
    elif unavailable == "deleted_entity":
        (await db_session.get(Entity, owner["entity_id"])).deleted_at = datetime.now(timezone.utc)
    else:
        user = await db_session.get(User, owner["id"])
        if unavailable == "deleted_personal_owner":
            user.deleted_at = datetime.now(timezone.utc)
        else:
            user.status = "suspended"
    await db_session.commit()
    assert (await join(client, guest_headers, token)).status_code == 404
    assert not await valid_chat_registration_invite(db_session, token, "any@example.com")


@pytest.mark.asyncio
async def test_dm_cannot_enable_public_invites(client):
    headers, _ = await _register(client, "public_dm")
    room = await client.post(ROOT, headers=headers, json={"kind": "dm", "recipient_email": "private@example.com"})
    assert room.status_code == 201, room.text
    for method in ("GET", "PUT", "DELETE"):
        result = await client.request(method, f"{ROOT}/{room.json()['id']}/public-invite", headers=headers, **({"json": {}} if method == "PUT" else {}))
        assert result.status_code == 403, result.text
