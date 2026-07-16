"""E2E tests: webhook endpoint CRUD, delivery, HMAC signatures."""

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.oss_regression


async def _auth(client: AsyncClient, username: str = "hookuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Hook Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_create_webhook_endpoint(client: AsyncClient):
    """Create an endpoint and verify a secret is auto-generated."""
    headers = await _auth(client)

    resp = await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={
            "url": "https://example.com/webhook",
            "events": ["task.created", "document.uploaded"],
            "description": "Test endpoint",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"] == "https://example.com/webhook"
    assert data["events"] == ["task.created", "document.uploaded"]
    assert data["enabled"] is True
    assert data["description"] == "Test endpoint"
    # Secret should be auto-generated (64 hex chars = 32 bytes)
    assert data["secret"] is not None
    assert len(data["secret"]) == 64
    assert data["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_list_endpoints(client: AsyncClient):
    """Create two endpoints and list them."""
    headers = await _auth(client, "hookuser_list")

    # Create two endpoints
    await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={
            "url": "https://example.com/hook1",
            "events": ["task.created"],
        },
    )
    await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={
            "url": "https://example.com/hook2",
            "events": [],
        },
    )

    resp = await client.get("/api/v1/webhooks", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    urls = {ep["url"] for ep in data}
    assert "https://example.com/hook1" in urls
    assert "https://example.com/hook2" in urls


@pytest.mark.asyncio
async def test_update_endpoint(client: AsyncClient):
    """Create an endpoint, update its URL and events."""
    headers = await _auth(client, "hookuser_update")

    resp = await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={
            "url": "https://example.com/old",
            "events": ["task.created"],
        },
    )
    eid = resp.json()["id"]

    update_resp = await client.put(
        f"/api/v1/webhooks/{eid}",
        headers=headers,
        json={
            "url": "https://example.com/new",
            "events": ["task.created", "task.completed"],
            "enabled": False,
        },
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["url"] == "https://example.com/new"
    assert data["events"] == ["task.created", "task.completed"]
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_delete_endpoint(client: AsyncClient):
    """Create an endpoint, delete it, verify 404 on re-fetch."""
    headers = await _auth(client, "hookuser_delete")

    resp = await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={
            "url": "https://example.com/todelete",
            "events": [],
        },
    )
    eid = resp.json()["id"]

    # Delete
    del_resp = await client.delete(f"/api/v1/webhooks/{eid}", headers=headers)
    assert del_resp.status_code == 204

    # Should be gone
    get_resp = await client.get(f"/api/v1/webhooks/{eid}", headers=headers)
    assert get_resp.status_code == 404

    # Double delete returns 404
    del_resp2 = await client.delete(f"/api/v1/webhooks/{eid}", headers=headers)
    assert del_resp2.status_code == 404


@pytest.mark.asyncio
async def test_webhook_signature(client: AsyncClient):
    """Verify HMAC-SHA256 signature computation matches expected output."""
    from packages.core.services.webhook_service import _sign_payload

    secret = "my-test-secret"
    payload = json.dumps({"event": "task.created", "data": {"id": "123"}}).encode()

    signature = _sign_payload(payload, secret)

    # Compute expected signature independently
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    assert signature == expected
    # Sanity: it should be a 64-char hex string
    assert len(signature) == 64
    assert all(c in "0123456789abcdef" for c in signature)
