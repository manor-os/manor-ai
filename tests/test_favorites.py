"""E2E tests: favorites, pins, bookmarks."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "favuser") -> dict:
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
async def test_toggle_favorite(client: AsyncClient):
    headers = await _auth(client)

    # Toggle ON
    resp = await client.post(
        "/api/v1/favorites/toggle",
        headers=headers,
        json={
            "resource_type": "task",
            "resource_id": "01TASK00000000000000000001",
            "favorite_type": "star",
            "note": "Important task",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_favorited"] is True
    assert data["favorite"]["resource_type"] == "task"
    assert data["favorite"]["note"] == "Important task"

    # Verify via check endpoint
    resp = await client.get(
        "/api/v1/favorites/check",
        headers=headers,
        params={
            "resource_type": "task",
            "resource_id": "01TASK00000000000000000001",
            "favorite_type": "star",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["is_favorited"] is True

    # Toggle OFF
    resp = await client.post(
        "/api/v1/favorites/toggle",
        headers=headers,
        json={
            "resource_type": "task",
            "resource_id": "01TASK00000000000000000001",
            "favorite_type": "star",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_favorited"] is False
    assert data["favorite"] is None

    # Verify removed
    resp = await client.get(
        "/api/v1/favorites/check",
        headers=headers,
        params={
            "resource_type": "task",
            "resource_id": "01TASK00000000000000000001",
            "favorite_type": "star",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["is_favorited"] is False


@pytest.mark.asyncio
async def test_list_favorites(client: AsyncClient):
    headers = await _auth(client)

    # Favorite multiple items
    for i, rtype in enumerate(["task", "document", "agent"]):
        await client.post(
            "/api/v1/favorites/toggle",
            headers=headers,
            json={
                "resource_type": rtype,
                "resource_id": f"01RES0000000000000000000{i}",
            },
        )

    # List all
    resp = await client.get("/api/v1/favorites", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3

    # Filter by resource_type
    resp = await client.get(
        "/api/v1/favorites",
        headers=headers,
        params={
            "resource_type": "task",
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["resource_type"] == "task"


@pytest.mark.asyncio
async def test_pinned_items(client: AsyncClient):
    headers = await _auth(client)

    # Pin a task and a conversation
    await client.post(
        "/api/v1/favorites/toggle",
        headers=headers,
        json={
            "resource_type": "task",
            "resource_id": "01TASK00000000000000000001",
            "favorite_type": "pin",
            "note": "Sprint goal",
        },
    )
    await client.post(
        "/api/v1/favorites/toggle",
        headers=headers,
        json={
            "resource_type": "conversation",
            "resource_id": "01CONV00000000000000000001",
            "favorite_type": "pin",
            "note": "Design discussion",
        },
    )

    resp = await client.get("/api/v1/favorites/pinned", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    types = {item["resource_type"] for item in data}
    assert types == {"task", "conversation"}


@pytest.mark.asyncio
async def test_favorite_counts(client: AsyncClient):
    headers = await _auth(client, username="favcount1")
    resource_type = "task"
    resource_id = "01TASK00000000000000000099"

    # Star and pin the same resource
    await client.post(
        "/api/v1/favorites/toggle",
        headers=headers,
        json={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "favorite_type": "star",
        },
    )
    await client.post(
        "/api/v1/favorites/toggle",
        headers=headers,
        json={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "favorite_type": "pin",
        },
    )

    resp = await client.get(
        "/api/v1/favorites/counts",
        headers=headers,
        params={
            "resource_type": resource_type,
            "resource_id": resource_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["star"] == 1
    assert data["pin"] == 1
    assert data["bookmark"] == 0
