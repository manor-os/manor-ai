"""E2E tests: entity API key management."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "keyuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── Create ──


@pytest.mark.asyncio
async def test_create_api_key(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "OpenRouter Production",
            "provider": "openrouter",
            "api_key": "sk-or-v1-abc123def456ghi789",
            "default_model": "anthropic/claude-sonnet-4",
            "is_default": True,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "OpenRouter Production"
    assert data["provider"] == "openrouter"
    assert data["key_prefix"].startswith("sk-or-v1")
    assert data["is_default"] is True
    assert data["status"] == "active"
    assert data["usage_count"] == 0
    # Clear key returned on create
    assert data["api_key"] == "sk-or-v1-abc123def456ghi789"
    assert data["id"]  # ULID present


# ── List (no hash) ──


@pytest.mark.asyncio
async def test_list_keys_no_hash(client: AsyncClient):
    headers = await _auth(client)
    await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "Key A",
            "provider": "openai",
            "api_key": "sk-test-key-aaaa1234bbbb",
        },
    )
    resp = await client.get("/api/v1/api-keys", headers=headers)
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) == 1
    assert "key_hash" not in keys[0]
    assert keys[0]["key_prefix"] == "sk-test-key-..."


# ── Default key logic ──


@pytest.mark.asyncio
async def test_set_default_key(client: AsyncClient):
    headers = await _auth(client)
    # Create first key as default
    r1 = await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "Key 1",
            "provider": "openrouter",
            "api_key": "sk-first-key-123456789",
            "is_default": True,
        },
    )
    key1_id = r1.json()["id"]

    # Create second key as default — should unset first
    r2 = await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "Key 2",
            "provider": "openai",
            "api_key": "sk-second-key-abcdef123",
            "is_default": True,
        },
    )
    key2_id = r2.json()["id"]

    # List and verify
    resp = await client.get("/api/v1/api-keys", headers=headers)
    keys = {k["id"]: k for k in resp.json()}
    assert keys[key1_id]["is_default"] is False
    assert keys[key2_id]["is_default"] is True


# ── Revoke ──


@pytest.mark.asyncio
async def test_revoke_key(client: AsyncClient):
    headers = await _auth(client)
    r = await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "Revoke Me",
            "provider": "anthropic",
            "api_key": "sk-ant-revoke-test-1234",
        },
    )
    key_id = r.json()["id"]

    resp = await client.delete(f"/api/v1/api-keys/{key_id}", headers=headers)
    assert resp.status_code == 204

    # Revoked key should not appear in list
    resp = await client.get("/api/v1/api-keys", headers=headers)
    assert len(resp.json()) == 0


# ── Rotate ──


@pytest.mark.asyncio
async def test_rotate_key(client: AsyncClient):
    headers = await _auth(client)
    r = await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "Rotate Me",
            "provider": "openrouter",
            "api_key": "sk-or-v1-old-key-value123",
        },
    )
    key_id = r.json()["id"]

    resp = await client.post(
        f"/api/v1/api-keys/{key_id}/rotate",
        headers=headers,
        json={
            "new_api_key": "sk-or-v1-brand-new-key456",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["key_prefix"].startswith("sk-or-v1")


# ── Resolve config ──


@pytest.mark.asyncio
async def test_resolve_config(client: AsyncClient):
    headers = await _auth(client)
    # Create a default key
    await client.post(
        "/api/v1/api-keys",
        headers=headers,
        json={
            "name": "Default Key",
            "provider": "openrouter",
            "api_key": "sk-or-v1-resolve-test-99",
            "default_model": "anthropic/claude-sonnet-4",
            "is_default": True,
        },
    )

    resp = await client.get("/api/v1/api-keys/resolve", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "entity_api_key"
    assert data["provider"] == "openrouter"
    assert data["model"] == "anthropic/claude-sonnet-4"
    assert data["key_prefix"].startswith("sk-or-v1")
    # Ensure no raw key exposed
    assert "api_key" not in data or data.get("api_key") is None
