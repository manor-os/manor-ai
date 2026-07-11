"""E2E tests: task templates — CRUD, instantiation, recurring setup."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "tmpluser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Template Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


async def _create_template(client: AsyncClient, headers: dict, **overrides) -> dict:
    payload = {
        "name": "Weekly Property Inspection",
        "title_template": "Property inspection - {{date}}",
        "description_template": "Inspect unit {{unit}} at {{address}}",
        "priority": 2,
        "task_type": "inspection",
        "details_template": {"unit": "{{unit}}", "address": "{{address}}"},
        "tags": ["inspection", "weekly"],
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/tasks/templates", headers=headers, json=payload)
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
async def test_create_template(client: AsyncClient):
    headers = await _auth(client)
    data = await _create_template(client, headers)
    assert data["name"] == "Weekly Property Inspection"
    assert data["title_template"] == "Property inspection - {{date}}"
    assert data["priority"] == 2
    assert data["task_type"] == "inspection"
    assert data["tags"] == ["inspection", "weekly"]
    assert data["status"] == "active"
    assert data["is_recurring"] is False


@pytest.mark.asyncio
async def test_instantiate_template(client: AsyncClient):
    headers = await _auth(client)
    tmpl = await _create_template(client, headers)

    resp = await client.post(
        f"/api/v1/tasks/templates/{tmpl['id']}/instantiate",
        headers=headers,
        json={"variables": {"unit": "3B", "address": "123 Main St"}},
    )
    assert resp.status_code == 201
    task = resp.json()
    assert "3B" in task["title"] or "3B" in (task["description"] or "")
    # Title should have date filled in (not the raw placeholder)
    assert "{{date}}" not in task["title"]
    assert task["description"] == "Inspect unit 3B at 123 Main St"
    assert task["details"]["unit"] == "3B"
    assert task["details"]["address"] == "123 Main St"
    assert task["priority"] == 2
    assert task["task_type"] == "inspection"
    assert task["status"] == "pending"


@pytest.mark.asyncio
async def test_list_templates(client: AsyncClient):
    headers = await _auth(client)
    await _create_template(client, headers, name="Template A")
    await _create_template(client, headers, name="Template B")

    resp = await client.get("/api/v1/tasks/templates", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    names = {t["name"] for t in items}
    assert "Template A" in names
    assert "Template B" in names


@pytest.mark.asyncio
async def test_update_template(client: AsyncClient):
    headers = await _auth(client)
    tmpl = await _create_template(client, headers)

    resp = await client.put(
        f"/api/v1/tasks/templates/{tmpl['id']}",
        headers=headers,
        json={"name": "Updated Name", "priority": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["priority"] == 1
    # Unchanged fields should be preserved
    assert data["title_template"] == "Property inspection - {{date}}"


@pytest.mark.asyncio
async def test_recurring_setup(client: AsyncClient):
    headers = await _auth(client)
    tmpl = await _create_template(client, headers)

    resp = await client.post(
        f"/api/v1/tasks/templates/{tmpl['id']}/recurring",
        headers=headers,
        json={"cron_expr": "0 9 * * 1"},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["execution_type"] == "task_template"
    assert job["execution_target"]["template_id"] == tmpl["id"]
    assert job["cron_expr"] == "0 9 * * 1"
    assert "Recurring:" in job["name"]

    # Verify template is now marked as recurring
    resp2 = await client.get(
        f"/api/v1/tasks/templates/{tmpl['id']}",
        headers=headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["is_recurring"] is True
    assert resp2.json()["recurrence_rule"] == "0 9 * * 1"
