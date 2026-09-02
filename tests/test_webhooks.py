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
@pytest.mark.parametrize(
    "url",
    [
        "example.com/webhook",
        "ftp://example.com/webhook",
        "https:///missing-host",
        "http://",
        "   ",
    ],
)
async def test_create_rejects_non_http_webhook_urls(client: AsyncClient, url: str):
    """Reject malformed/non-HTTP endpoint URLs before persisting an endpoint."""
    headers = await _auth(client, f"hookuser_invalid_{abs(hash(url))}")

    resp = await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={"url": url},
    )

    assert 400 <= resp.status_code < 500


@pytest.mark.asyncio
async def test_update_rejects_invalid_url_without_changing_endpoint(client: AsyncClient):
    headers = await _auth(client, "hookuser_invalid_update")

    create_resp = await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={"url": "https://example.com/original"},
    )
    endpoint_id = create_resp.json()["id"]

    update_resp = await client.put(
        f"/api/v1/webhooks/{endpoint_id}",
        headers=headers,
        json={"url": "not-a-url"},
    )
    assert 400 <= update_resp.status_code < 500

    get_resp = await client.get(f"/api/v1/webhooks/{endpoint_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["url"] == "https://example.com/original"


@pytest.mark.asyncio
async def test_webhook_secret_is_only_returned_on_create(client: AsyncClient):
    headers = await _auth(client, "hookuser_secret_visibility")

    create_resp = await client.post(
        "/api/v1/webhooks",
        headers=headers,
        json={"url": "https://example.com/secret"},
    )
    assert create_resp.status_code == 201
    endpoint_id = create_resp.json()["id"]
    assert create_resp.json().get("secret")

    list_resp = await client.get("/api/v1/webhooks", headers=headers)
    get_resp = await client.get(f"/api/v1/webhooks/{endpoint_id}", headers=headers)
    update_resp = await client.put(
        f"/api/v1/webhooks/{endpoint_id}",
        headers=headers,
        json={"description": "updated"},
    )

    for resp in (list_resp, get_resp, update_resp):
        assert resp.status_code == 200
        payload = resp.json()
        entries = payload if isinstance(payload, list) else [payload]
        assert all("secret" not in entry for entry in entries)


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
