"""E2E tests: entity data backup and export."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "backupuser") -> dict:
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


@pytest.mark.asyncio
async def test_export_summary(client: AsyncClient):
    """Create some data and verify the summary returns correct counts."""
    headers = await _auth(client)

    # Create a workspace
    await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Test WS",
        },
    )

    # Create a task
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Backup test task",
        },
    )

    resp = await client.get("/api/v1/backup/summary", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["users"] == 1
    assert data["workspaces"] >= 1
    assert data["tasks"] >= 1


@pytest.mark.asyncio
async def test_full_export(client: AsyncClient):
    """Create user + task + workspace, export, verify structure."""
    headers = await _auth(client)

    # Create a workspace
    await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Export WS",
        },
    )

    # Create a task
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Export test task",
        },
    )

    resp = await client.get("/api/v1/backup/export", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    # Top-level structure
    assert data["version"] == "1.0"
    assert "exported_at" in data
    assert "entity_id" in data
    assert "entity" in data
    assert "users" in data
    assert "workspaces" in data
    assert "tasks" in data
    assert "conversations" in data
    assert "documents" in data
    assert "agents" in data
    assert "clients" in data
    assert "staff_members" in data
    assert "stats" in data

    # Verify data was actually exported
    assert len(data["users"]) == 1
    assert len(data["workspaces"]) >= 1
    assert len(data["tasks"]) >= 1

    # Stats match actual data
    assert data["stats"]["users"] == len(data["users"])
    assert data["stats"]["workspaces"] == len(data["workspaces"])
    assert data["stats"]["tasks"] == len(data["tasks"])


@pytest.mark.asyncio
async def test_export_excludes_passwords(client: AsyncClient):
    """Exported user records must not contain password_hash."""
    headers = await _auth(client)

    resp = await client.get("/api/v1/backup/export", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["users"]) >= 1
    for user in data["users"]:
        assert "password_hash" not in user, "password_hash must not appear in export"
