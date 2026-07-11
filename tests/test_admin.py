"""E2E tests: audit logs, entity settings, user preferences."""

import pytest
from httpx import AsyncClient

from packages.core.constants.plans import DEFAULT_PLAN_ID


async def _auth(client: AsyncClient, username: str = "adminuser") -> dict:
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


# ── Audit logs ──


@pytest.mark.asyncio
async def test_audit_log_created(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/admin/audit-logs",
        headers=headers,
        json={
            "action": "task.create",
            "resource_type": "task",
            "resource_id": "01ABC",
            "details": {"name": "My Task"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["action"] == "task.create"
    assert data["resource_type"] == "task"
    assert data["resource_id"] == "01ABC"
    assert data["details"] == {"name": "My Task"}
    assert data["id"]  # ULID present


@pytest.mark.asyncio
async def test_list_audit_logs_filtered(client: AsyncClient):
    headers = await _auth(client)
    # Create several entries
    await client.post(
        "/api/v1/admin/audit-logs",
        headers=headers,
        json={
            "action": "task.create",
            "resource_type": "task",
        },
    )
    await client.post(
        "/api/v1/admin/audit-logs",
        headers=headers,
        json={
            "action": "agent.update",
            "resource_type": "agent",
        },
    )
    await client.post(
        "/api/v1/admin/audit-logs",
        headers=headers,
        json={
            "action": "task.delete",
            "resource_type": "task",
        },
    )

    # No filter — all 3
    resp = await client.get("/api/v1/admin/audit-logs", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 3

    # Filter by resource_type
    resp = await client.get(
        "/api/v1/admin/audit-logs",
        headers=headers,
        params={
            "resource_type": "task",
        },
    )
    assert resp.json()["total"] == 2

    # Filter by action
    resp = await client.get(
        "/api/v1/admin/audit-logs",
        headers=headers,
        params={
            "action": "agent.update",
        },
    )
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["action"] == "agent.update"


# ── Entity settings ──


@pytest.mark.asyncio
async def test_entity_settings_crud(client: AsyncClient):
    headers = await _auth(client)

    # New entities carry the default OSS/free plan setting.
    resp = await client.get("/api/v1/admin/settings", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["settings"] == {"plan": DEFAULT_PLAN_ID}

    # Set some values
    resp = await client.put(
        "/api/v1/admin/settings",
        headers=headers,
        json={
            "theme": "dark",
            "language": "en",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["settings"] == {"plan": DEFAULT_PLAN_ID, "theme": "dark", "language": "en"}

    # Read back
    resp = await client.get("/api/v1/admin/settings", headers=headers)
    assert resp.json()["settings"]["theme"] == "dark"


@pytest.mark.asyncio
async def test_settings_merge(client: AsyncClient):
    """Update preserves existing keys (partial merge)."""
    headers = await _auth(client)

    # First update
    await client.put(
        "/api/v1/admin/settings",
        headers=headers,
        json={
            "theme": "dark",
            "language": "en",
        },
    )

    # Second update — adds new key, keeps existing
    resp = await client.put(
        "/api/v1/admin/settings",
        headers=headers,
        json={
            "notifications": True,
            "theme": "light",
        },
    )
    settings = resp.json()["settings"]
    assert settings["language"] == "en"  # preserved
    assert settings["notifications"] is True  # added
    assert settings["theme"] == "light"  # overwritten


# ── User preferences ──


@pytest.mark.asyncio
async def test_user_preferences_crud(client: AsyncClient):
    headers = await _auth(client)

    # Initially empty
    resp = await client.get("/api/v1/admin/preferences", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["preferences"] == {}

    # Set preferences
    resp = await client.put(
        "/api/v1/admin/preferences",
        headers=headers,
        json={
            "sidebar_collapsed": True,
            "font_size": 14,
        },
    )
    assert resp.status_code == 200
    prefs = resp.json()["preferences"]
    assert prefs["sidebar_collapsed"] is True
    assert prefs["font_size"] == 14

    # Merge — preserves existing
    resp = await client.put(
        "/api/v1/admin/preferences",
        headers=headers,
        json={
            "font_size": 16,
            "color": "blue",
        },
    )
    prefs = resp.json()["preferences"]
    assert prefs["sidebar_collapsed"] is True  # preserved
    assert prefs["font_size"] == 16  # overwritten
    assert prefs["color"] == "blue"  # added


# ── Isolation ──


@pytest.mark.asyncio
async def test_admin_isolation(client: AsyncClient):
    """Audit logs are scoped to entity — user B cannot see user A's logs."""
    headers_a = await _auth(client, "user_a")
    headers_b = await _auth(client, "user_b")

    # User A creates an audit entry
    await client.post(
        "/api/v1/admin/audit-logs",
        headers=headers_a,
        json={
            "action": "secret.action",
            "resource_type": "secret",
        },
    )

    # User A sees it
    resp = await client.get("/api/v1/admin/audit-logs", headers=headers_a)
    assert resp.json()["total"] == 1

    # User B sees nothing (different entity)
    resp = await client.get("/api/v1/admin/audit-logs", headers=headers_b)
    assert resp.json()["total"] == 0
