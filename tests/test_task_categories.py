"""E2E tests: task categories CRUD."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "catuser") -> dict:
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
async def test_create_category(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/tasks/categories",
        headers=headers,
        json={
            "name": "Maintenance",
            "icon": "wrench",
            "color": "#FF5733",
            "sort_order": 1,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Maintenance"
    assert data["icon"] == "wrench"
    assert data["color"] == "#FF5733"
    assert data["sort_order"] == 1


@pytest.mark.asyncio
async def test_list_categories(client: AsyncClient):
    headers = await _auth(client)
    await client.post("/api/v1/tasks/categories", headers=headers, json={"name": "Cat A", "sort_order": 2})
    await client.post("/api/v1/tasks/categories", headers=headers, json={"name": "Cat B", "sort_order": 1})

    resp = await client.get("/api/v1/tasks/categories", headers=headers)
    assert resp.status_code == 200
    cats = resp.json()
    assert len(cats) == 2
    # Should be ordered by sort_order
    assert cats[0]["name"] == "Cat B"
    assert cats[1]["name"] == "Cat A"


@pytest.mark.asyncio
async def test_update_category(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post("/api/v1/tasks/categories", headers=headers, json={"name": "Old Name"})
    cat_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/tasks/categories/{cat_id}",
        headers=headers,
        json={
            "name": "New Name",
            "color": "#00FF00",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["color"] == "#00FF00"


@pytest.mark.asyncio
async def test_delete_category(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post("/api/v1/tasks/categories", headers=headers, json={"name": "To Delete"})
    cat_id = create.json()["id"]

    resp = await client.delete(f"/api/v1/tasks/categories/{cat_id}", headers=headers)
    assert resp.status_code == 204

    # Verify it's gone
    resp2 = await client.get("/api/v1/tasks/categories", headers=headers)
    assert all(c["id"] != cat_id for c in resp2.json())
