"""E2E tests: activity feed and event logging."""

import asyncio

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "actuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Activity Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_log_and_list_events(client: AsyncClient):
    """Log events via POST, list them back with filters."""
    headers = await _auth(client)

    # Log two events of different types
    r1 = await client.post(
        "/api/v1/activity/events",
        headers=headers,
        json={
            "event_type": "task.created",
            "source": "test",
            "payload": {"task_id": "t1", "title": "Fix roof"},
        },
    )
    assert r1.status_code == 201
    assert r1.json()["event_type"] == "task.created"

    r2 = await client.post(
        "/api/v1/activity/events",
        headers=headers,
        json={
            "event_type": "document.uploaded",
            "source": "test",
            "payload": {"document_id": "d1", "name": "lease.pdf"},
        },
    )
    assert r2.status_code == 201

    # List all
    resp = await client.get("/api/v1/activity/events", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # Filter by event_type
    resp2 = await client.get("/api/v1/activity/events?event_type=task.created", headers=headers)
    assert resp2.json()["total"] == 1
    assert resp2.json()["items"][0]["event_type"] == "task.created"

    # Filter by source
    resp3 = await client.get("/api/v1/activity/events?source=test", headers=headers)
    assert resp3.json()["total"] == 2


@pytest.mark.asyncio
async def test_activity_feed_format(client: AsyncClient):
    """Log events and verify the feed returns human-readable descriptions."""
    headers = await _auth(client)

    await client.post(
        "/api/v1/activity/events",
        headers=headers,
        json={
            "event_type": "task.created",
            "payload": {"task_id": "t1", "title": "Paint walls"},
        },
    )
    await client.post(
        "/api/v1/activity/events",
        headers=headers,
        json={
            "event_type": "user.login",
            "payload": {"username": "alice"},
        },
    )
    await client.post(
        "/api/v1/activity/events",
        headers=headers,
        json={
            "event_type": "goal.completed",
            "payload": {"goal": "Quarterly review"},
        },
    )

    resp = await client.get("/api/v1/activity/feed", headers=headers)
    assert resp.status_code == 200
    feed = resp.json()
    assert len(feed) == 3

    # Check each feed item has the right shape
    for item in feed:
        assert "id" in item
        assert "event_type" in item
        assert "description" in item
        assert "timestamp" in item
        assert "icon" in item

    descriptions = {item["event_type"]: item["description"] for item in feed}
    assert descriptions["task.created"] == "New task: Paint walls"
    assert descriptions["user.login"] == "alice logged in"
    assert descriptions["goal.completed"] == "Goal completed: Quarterly review"

    # Check icons
    icons = {item["event_type"]: item["icon"] for item in feed}
    assert icons["task.created"] == "clipboard"
    assert icons["user.login"] == "user"
    assert icons["goal.completed"] == "target"


@pytest.mark.asyncio
async def test_auto_event_on_task_create(client: AsyncClient):
    """Creating a task via the API should auto-emit a task.created event."""
    headers = await _auth(client)

    # Create a task
    resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Replace light bulbs",
        },
    )
    assert resp.status_code == 201

    # Give the fire-and-forget task a moment to complete
    await asyncio.sleep(0.3)

    # Check event was logged
    resp2 = await client.get("/api/v1/activity/events?event_type=task.created", headers=headers)
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["total"] >= 1
    titles = [e["payload"].get("title") for e in data["items"]]
    assert "Replace light bulbs" in titles


@pytest.mark.asyncio
async def test_auto_event_on_login(client: AsyncClient):
    """Logging in should auto-emit a user.login event."""
    # Register first
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "actuser",
            "email": "actuser@test.com",
            "password": "pass123",
            "entity_name": "Login Corp",
        },
    )
    assert reg.status_code == 200
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    # Login (triggers authenticate_user which emits user.login)
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "actuser",
            "password": "pass123",
        },
    )
    assert login_resp.status_code == 200

    # Give the fire-and-forget task a moment to complete
    await asyncio.sleep(0.3)

    # Check event was logged
    resp = await client.get("/api/v1/activity/events?event_type=user.login", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    usernames = [e["payload"].get("username") for e in data["items"]]
    assert "actuser" in usernames
