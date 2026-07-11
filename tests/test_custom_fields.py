"""E2E tests: custom field definitions and workspace dashboards."""

import pytest
from httpx import AsyncClient

from packages.core.services.custom_field_service import validate_custom_fields


async def _auth(client: AsyncClient, username: str = "fielduser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Fields Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


# ── 1. Create a custom field definition ──


@pytest.mark.asyncio
async def test_create_field_definition(client: AsyncClient):
    headers = await _auth(client, "fieldcreate")

    resp = await client.post(
        "/api/v1/custom-fields",
        headers=headers,
        json={
            "name": "property_address",
            "display_name": "Property Address",
            "field_type": "text",
            "target": "task",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "property_address"
    assert body["display_name"] == "Property Address"
    assert body["field_type"] == "text"
    assert body["target"] == "task"
    assert body["required"] is False
    assert body["status"] == "active"
    assert "id" in body


# ── 2. List custom field definitions ──


@pytest.mark.asyncio
async def test_list_field_definitions(client: AsyncClient):
    headers = await _auth(client, "fieldlist")

    # Create two fields with different targets
    await client.post(
        "/api/v1/custom-fields",
        headers=headers,
        json={
            "name": "priority_level",
            "display_name": "Priority Level",
            "field_type": "select",
            "target": "task",
            "options": ["Low", "Medium", "High"],
        },
    )
    await client.post(
        "/api/v1/custom-fields",
        headers=headers,
        json={
            "name": "company_size",
            "display_name": "Company Size",
            "field_type": "number",
            "target": "client",
        },
    )

    # List all
    resp = await client.get("/api/v1/custom-fields", headers=headers)
    assert resp.status_code == 200
    all_fields = resp.json()
    assert len(all_fields) == 2

    # Filter by target=task
    resp = await client.get("/api/v1/custom-fields?target=task", headers=headers)
    assert resp.status_code == 200
    task_fields = resp.json()
    assert len(task_fields) == 1
    assert task_fields[0]["name"] == "priority_level"


# ── 3. Validate custom fields (unit test — no DB) ──


@pytest.mark.asyncio
async def test_validate_custom_fields():
    field_defs = [
        {"name": "address", "field_type": "text", "required": True, "options": []},
        {"name": "price", "field_type": "number", "required": False, "options": []},
        {"name": "status", "field_type": "select", "required": False, "options": ["Active", "Sold"]},
        {"name": "tags", "field_type": "multiselect", "required": False, "options": ["Luxury", "Budget", "New"]},
        {"name": "featured", "field_type": "boolean", "required": False, "options": []},
    ]

    # Valid values
    ok, errors = validate_custom_fields(
        field_defs,
        {
            "address": "123 Main St",
            "price": 250000,
            "status": "Active",
            "tags": ["Luxury", "New"],
            "featured": True,
        },
    )
    assert ok is True
    assert errors == []

    # Missing required field
    ok, errors = validate_custom_fields(field_defs, {"price": 100})
    assert ok is False
    assert any("address" in e and "required" in e for e in errors)

    # Invalid number
    ok, errors = validate_custom_fields(field_defs, {"address": "x", "price": "not-a-number"})
    assert ok is False
    assert any("price" in e and "number" in e for e in errors)

    # Invalid select option
    ok, errors = validate_custom_fields(field_defs, {"address": "x", "status": "Unknown"})
    assert ok is False
    assert any("status" in e for e in errors)

    # Invalid multiselect option
    ok, errors = validate_custom_fields(field_defs, {"address": "x", "tags": ["Luxury", "Invalid"]})
    assert ok is False
    assert any("tags" in e and "invalid" in e.lower() for e in errors)

    # Invalid boolean
    ok, errors = validate_custom_fields(field_defs, {"address": "x", "featured": "yes"})
    assert ok is False
    assert any("featured" in e and "boolean" in e for e in errors)


# ── 4. Workspace dashboard ──


@pytest.mark.asyncio
async def test_workspace_dashboard(client: AsyncClient):
    headers = await _auth(client, "fielddash")

    # Create a workspace
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Test Operation",
            "description": "Dashboard test workspace",
        },
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Create tasks in this workspace
    for i, status in enumerate(["pending", "pending", "in_progress", "completed"]):
        t = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "title": f"Task {i + 1}",
                "status": "pending",
                "priority": 3,
                "workspace_id": ws_id,
            },
        )
        assert t.status_code == 201
        if status != "pending":
            task_id = t.json()["id"]
            await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": status})

    # Get workspace dashboard
    resp = await client.get(f"/api/v1/workspaces/{ws_id}/dashboard", headers=headers)
    assert resp.status_code == 200
    dash = resp.json()

    assert dash["workspace_id"] == ws_id
    assert dash["tasks"]["total"] == 4
    assert dash["tasks"]["by_status"]["pending"] == 2
    assert dash["tasks"]["by_status"]["in_progress"] == 1
    assert dash["tasks"]["by_status"]["completed"] == 1
    assert dash["documents"]["total"] == 0
    assert dash["agents"]["total"] == 0
    assert len(dash["recent_tasks"]) == 4
    assert "custom_field_summary" in dash
