"""E2E tests: notifications CRUD, read/unread, isolation."""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services import realtime
from packages.core.services.notification_service import create_notification


async def _auth(client: AsyncClient, username: str = "notifuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Notif Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_create_and_list_notifications(client: AsyncClient):
    headers = await _auth(client)

    # Create two notifications
    r1 = await client.post(
        "/api/v1/notifications",
        headers=headers,
        json={
            "type": "task_assigned",
            "title": "You have a new task",
            "body": "Fix the AC in unit 101",
        },
    )
    assert r1.status_code == 201
    assert r1.json()["type"] == "task_assigned"
    assert r1.json()["title"] == "You have a new task"
    assert r1.json()["is_read"] is False

    r2 = await client.post(
        "/api/v1/notifications",
        headers=headers,
        json={
            "type": "system",
            "title": "Welcome aboard!",
        },
    )
    assert r2.status_code == 201

    # List all
    resp = await client.get("/api/v1/notifications", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["unread_count"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_mark_read(client: AsyncClient):
    headers = await _auth(client)

    # Create a notification
    r = await client.post(
        "/api/v1/notifications",
        headers=headers,
        json={
            "type": "message",
            "title": "New message from agent",
        },
    )
    nid = r.json()["id"]

    # Mark it as read
    resp = await client.post(f"/api/v1/notifications/{nid}/read", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["is_read"] is True
    assert resp.json()["read_at"] is not None

    # Unread count should be 0
    list_resp = await client.get("/api/v1/notifications", headers=headers)
    assert list_resp.json()["unread_count"] == 0


@pytest.mark.asyncio
async def test_mark_all_read(client: AsyncClient):
    headers = await _auth(client)

    # Create 3 notifications
    for i in range(3):
        await client.post(
            "/api/v1/notifications",
            headers=headers,
            json={
                "type": "system",
                "title": f"Notification {i}",
            },
        )

    # Verify 3 unread
    resp = await client.get("/api/v1/notifications", headers=headers)
    assert resp.json()["unread_count"] == 3

    # Mark all read
    mark_resp = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert mark_resp.status_code == 200
    assert mark_resp.json()["count"] == 3

    # Verify 0 unread
    resp2 = await client.get("/api/v1/notifications", headers=headers)
    assert resp2.json()["unread_count"] == 0


@pytest.mark.asyncio
async def test_notifications_include_active_company_memberships(
    client: AsyncClient,
    db_session,
):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "notif_multi",
            "email": "notif_multi@test.com",
            "password": "pass123",
            "entity_name": "Notif Primary",
        },
    )
    assert register.status_code == 200, register.text
    data = register.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}

    user = (await db_session.execute(select(User).where(User.id == data["user_id"]))).scalar_one()
    company = Entity(id=generate_ulid(), name="Notif Secondary", settings={})
    db_session.add(company)
    membership = UserMembership(
        id=generate_ulid(),
        user_id=user.id,
        entity_id=company.id,
        role="member",
        status="active",
    )
    notification = Notification(
        id=generate_ulid(),
        entity_id=company.id,
        user_id=user.id,
        type="system",
        title="Company-side notice",
        content="This notification belongs to another active company.",
    )
    db_session.add_all([membership, notification])
    await db_session.commit()

    resp = await client.get("/api/v1/notifications", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unread_count"] == 1
    assert [n["title"] for n in body["items"]] == ["Company-side notice"]

    mark = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert mark.status_code == 200, mark.text
    assert mark.json()["count"] == 1

    after = await client.get("/api/v1/notifications", headers=headers)
    assert after.json()["unread_count"] == 0

    membership.status = "inactive"
    await db_session.commit()

    mark_after_revocation = await client.post(
        f"/api/v1/notifications/{notification.id}/read",
        headers=headers,
    )
    assert mark_after_revocation.status_code == 404
    delete_after_revocation = await client.delete(
        f"/api/v1/notifications/{notification.id}",
        headers=headers,
    )
    assert delete_after_revocation.status_code == 404


@pytest.mark.asyncio
async def test_unread_count(client: AsyncClient):
    headers = await _auth(client)

    # Create 2 notifications
    r1 = await client.post(
        "/api/v1/notifications",
        headers=headers,
        json={
            "type": "task_completed",
            "title": "Task done",
        },
    )
    await client.post(
        "/api/v1/notifications",
        headers=headers,
        json={
            "type": "system",
            "title": "System update",
        },
    )

    # Unread count is 2
    resp = await client.get("/api/v1/notifications", headers=headers)
    assert resp.json()["unread_count"] == 2

    # Mark one as read
    await client.post(f"/api/v1/notifications/{r1.json()['id']}/read", headers=headers)

    # Unread count is 1
    resp2 = await client.get("/api/v1/notifications", headers=headers)
    assert resp2.json()["unread_count"] == 1

    # Filter unread_only
    resp3 = await client.get("/api/v1/notifications?unread_only=true", headers=headers)
    assert resp3.json()["total"] == 1
    assert resp3.json()["items"][0]["title"] == "System update"


@pytest.mark.asyncio
async def test_delete_notification(client: AsyncClient):
    headers = await _auth(client)

    r = await client.post(
        "/api/v1/notifications",
        headers=headers,
        json={
            "type": "system",
            "title": "To be deleted",
        },
    )
    nid = r.json()["id"]

    # Delete it
    resp = await client.delete(f"/api/v1/notifications/{nid}", headers=headers)
    assert resp.status_code == 204

    # Should be gone
    list_resp = await client.get("/api/v1/notifications", headers=headers)
    assert list_resp.json()["total"] == 0

    # Deleting again returns 404
    resp2 = await client.delete(f"/api/v1/notifications/{nid}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_notification_isolation(client: AsyncClient):
    """User A cannot see or modify User B's notifications."""
    headers_a = await _auth(client, "notif_a")
    headers_b = await _auth(client, "notif_b")

    # A creates a notification
    r = await client.post(
        "/api/v1/notifications",
        headers=headers_a,
        json={
            "type": "system",
            "title": "A's secret notification",
        },
    )
    nid = r.json()["id"]

    # B cannot see it in their list
    resp = await client.get("/api/v1/notifications", headers=headers_b)
    assert resp.json()["total"] == 0

    # B cannot mark it as read
    resp2 = await client.post(f"/api/v1/notifications/{nid}/read", headers=headers_b)
    assert resp2.status_code == 404

    # B cannot delete it
    resp3 = await client.delete(f"/api/v1/notifications/{nid}", headers=headers_b)
    assert resp3.status_code == 404


@pytest.mark.asyncio
async def test_realtime_push_happens_only_after_commit(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "notif_after_commit",
            "email": "notif_after_commit@test.com",
            "password": "pass123",
            "entity_name": "After Commit Corp",
        },
    )
    data = register.json()
    pushed: list[dict] = []

    async def fake_push(user_id: str, notification: dict, *, entity_id: str):
        pushed.append({"user_id": user_id, "entity_id": entity_id, **notification})

    monkeypatch.setattr(realtime, "push_notification", fake_push)
    await create_notification(
        db_session,
        data["entity_id"],
        data["user_id"],
        "system",
        "Committed notification",
    )
    assert pushed == []

    await db_session.commit()
    await asyncio.sleep(0)
    assert [item["title"] for item in pushed] == ["Committed notification"]


@pytest.mark.asyncio
async def test_realtime_push_is_discarded_on_rollback(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "notif_rollback",
            "email": "notif_rollback@test.com",
            "password": "pass123",
            "entity_name": "Rollback Corp",
        },
    )
    data = register.json()
    pushed: list[dict] = []

    async def fake_push(user_id: str, notification: dict, *, entity_id: str):
        pushed.append(notification)

    monkeypatch.setattr(realtime, "push_notification", fake_push)
    await create_notification(
        db_session,
        data["entity_id"],
        data["user_id"],
        "system",
        "Rolled back notification",
    )
    await db_session.rollback()
    await asyncio.sleep(0)
    assert pushed == []


@pytest.mark.asyncio
async def test_notification_rejects_conflicting_workspace_scopes(
    client: AsyncClient,
    db_session,
):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "notif_scope_conflict",
            "email": "notif_scope_conflict@test.com",
            "password": "pass123",
            "entity_name": "Scope Conflict Corp",
        },
    )
    data = register.json()

    with pytest.raises(ValueError, match="workspace scope"):
        await create_notification(
            db_session,
            data["entity_id"],
            data["user_id"],
            "system",
            "Conflicting scope",
            workspace_id="workspace-a",
            meta={"workspace_id": "workspace-b"},
        )

    assert (await db_session.execute(select(Notification))).scalars().all() == []
