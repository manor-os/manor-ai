"""E2E tests: entity quota defaults, usage report, quota check, quota update."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "quotauser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Quota Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_get_default_quota(client: AsyncClient):
    """New entity gets free plan defaults."""
    headers = await _auth(client, "quotadefault")

    resp = await client.get("/api/v1/quotas", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "free"
    assert body["tokens"]["limit"] == 1_000_000
    assert body["tokens"]["used"] == 0
    assert body["api_calls"]["limit"] == 10_000
    assert body["api_calls"]["used"] == 0
    assert body["storage"]["limit"] == 1_073_741_824
    assert body["users"]["limit"] == 5
    assert body["agents"]["limit"] == 3
    assert body["documents"]["limit"] == 100


@pytest.mark.asyncio
async def test_usage_report(client: AsyncClient):
    """Verify report structure contains expected keys and types."""
    headers = await _auth(client, "quotareport")

    resp = await client.get("/api/v1/quotas", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    # All expected top-level keys present
    for key in ("plan", "tokens", "api_calls", "storage", "users", "agents", "documents"):
        assert key in body, f"Missing key: {key}"

    # Metered resources have used/limit/pct
    for key in ("tokens", "api_calls", "storage"):
        assert "used" in body[key]
        assert "limit" in body[key]
        assert "pct" in body[key]

    # Non-metered resources have limit
    for key in ("users", "agents", "documents"):
        assert "limit" in body[key]


@pytest.mark.asyncio
async def test_check_quota_within_limit(client: AsyncClient):
    """Fresh entity should be within all quotas."""
    headers = await _auth(client, "quotacheck")

    for resource in ("users", "agents", "documents", "storage", "tokens", "api_calls"):
        resp = await client.get(f"/api/v1/quotas/check/{resource}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is True, f"{resource} should be allowed"
        assert body["reason"] == ""

    # Invalid resource returns 400
    resp = await client.get("/api/v1/quotas/check/invalid", headers=headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_quota(client: AsyncClient):
    """Owner can change limits and they persist."""
    headers = await _auth(client, "quotaupdate")

    # Update several limits
    resp = await client.put(
        "/api/v1/quotas",
        headers=headers,
        json={
            "plan_name": "pro",
            "max_users": 50,
            "max_tokens_monthly": 10_000_000,
            "max_api_calls_daily": 100_000,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_name"] == "pro"
    assert body["max_users"] == 50
    assert body["max_tokens_monthly"] == 10_000_000
    assert body["max_api_calls_daily"] == 100_000
    # Unchanged defaults preserved
    assert body["max_agents"] == 3
    assert body["max_documents"] == 100

    # Verify via GET report
    resp2 = await client.get("/api/v1/quotas", headers=headers)
    assert resp2.status_code == 200
    report = resp2.json()
    assert report["plan"] == "pro"
    assert report["tokens"]["limit"] == 10_000_000
    assert report["users"]["limit"] == 50
