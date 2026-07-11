"""E2E tests: workspace CRUD."""

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, username: str = "wsuser") -> tuple[str, dict]:
    """Register and return (token, headers)."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "WS Corp",
        },
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_create_workspace(client: AsyncClient):
    _, headers = await _register(client)
    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "My Project",
            "description": "A test workspace",
            "category": "development",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Project"
    assert data["description"] == "A test workspace"
    assert data["category"] == "development"
    assert data["id"]


@pytest.mark.asyncio
async def test_create_workspace_enforces_plan_limit_without_stale_cache(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Free tenants should not be able to create a second workspace by
    reusing a cached positive plan-gate result from the first request."""
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    plan_gate.invalidate_gate_cache()

    _, headers = await _register(client, "workspace_limit_cache")

    first = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Allowed Workspace"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Blocked Workspace"},
    )
    assert second.status_code == 402, second.text
    assert second.json()["detail"]["kind"] == "workspaces"


@pytest.mark.asyncio
async def test_delete_workspace_immediately_frees_plan_slot(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """A delete response must make the freed workspace slot visible to the
    very next create request; clients should not need to wait for the
    dependency cleanup commit."""
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    plan_gate.invalidate_gate_cache()

    _, headers = await _register(client, "workspace_delete_frees_slot")

    original = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Original"},
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]

    deleted = await client.delete(f"/api/v1/workspaces/{original_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    replacement = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Replacement"},
    )
    assert replacement.status_code == 201, replacement.text


@pytest.mark.asyncio
async def test_restore_workspace_enforces_plan_limit(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Restoring a trashed workspace should not bypass the active
    workspace cap."""
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    plan_gate.invalidate_gate_cache()

    _, headers = await _register(client, "workspace_restore_limit")

    original = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Original"},
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]

    deleted = await client.delete(f"/api/v1/workspaces/{original_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    replacement = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Replacement"},
    )
    assert replacement.status_code == 201, replacement.text

    restored = await client.post(f"/api/v1/workspaces/{original_id}/restore", headers=headers)
    assert restored.status_code == 402, restored.text
    assert restored.json()["detail"]["kind"] == "workspaces"


@pytest.mark.asyncio
async def test_list_workspaces(client: AsyncClient):
    _, headers = await _register(client)
    # Create 3 workspaces
    for name in ["Alpha", "Beta", "Gamma"]:
        await client.post("/api/v1/workspaces", headers=headers, json={"name": name})

    resp = await client.get("/api/v1/workspaces", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    names = {ws["name"] for ws in data}
    assert names == {"Alpha", "Beta", "Gamma"}


@pytest.mark.asyncio
async def test_get_workspace(client: AsyncClient):
    _, headers = await _register(client)
    create_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "GetMe"})
    ws_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "GetMe"


@pytest.mark.asyncio
async def test_update_workspace(client: AsyncClient):
    _, headers = await _register(client)
    create_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Old Name"})
    ws_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=headers,
        json={
            "name": "New Name",
            "address": "456 Oak Ave",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["address"] == "456 Oak Ave"


@pytest.mark.asyncio
async def test_delete_workspace(client: AsyncClient):
    _, headers = await _register(client)
    create_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "ToDelete"})
    ws_id = create_resp.json()["id"]

    # Delete
    resp = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp.status_code == 204

    # Verify gone
    resp2 = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_workspace_isolation(client: AsyncClient):
    """User A can't see User B's workspaces."""
    _, headers_a = await _register(client, "user_a")
    _, headers_b = await _register(client, "user_b")

    # A creates a workspace
    create_resp = await client.post("/api/v1/workspaces", headers=headers_a, json={"name": "A's Project"})
    ws_id = create_resp.json()["id"]

    # B can't see it
    resp = await client.get(f"/api/v1/workspaces/{ws_id}", headers=headers_b)
    assert resp.status_code == 404

    # B's list is empty
    resp2 = await client.get("/api/v1/workspaces", headers=headers_b)
    assert len(resp2.json()) == 0
