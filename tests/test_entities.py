"""E2E tests: entity management."""

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, username: str = "testuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Test Corp",
        },
    )
    return resp.json()


@pytest.mark.asyncio
async def test_get_entity(client: AsyncClient):
    reg = await _register(client)
    resp = await client.get(
        "/api/v1/entities/me",
        headers={
            "Authorization": f"Bearer {reg['access_token']}",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == reg["entity_id"]
    assert data["name"] == "Test Corp"


@pytest.mark.asyncio
async def test_update_entity(client: AsyncClient):
    reg = await _register(client)
    headers = {"Authorization": f"Bearer {reg['access_token']}"}

    resp = await client.put(
        "/api/v1/entities/me",
        headers=headers,
        json={
            "name": "Updated Corp",
            "address": "123 Main St",
            "llm_model": "anthropic/claude-opus-4",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Corp"
    assert data["address"] == "123 Main St"
    assert data["llm_model"] == "anthropic/claude-opus-4"

    # Verify persisted
    resp2 = await client.get("/api/v1/entities/me", headers=headers)
    assert resp2.json()["name"] == "Updated Corp"


@pytest.mark.asyncio
async def test_get_entity_no_auth(client: AsyncClient):
    resp = await client.get("/api/v1/entities/me")
    assert resp.status_code == 401
