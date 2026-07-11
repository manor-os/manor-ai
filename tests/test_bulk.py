"""E2E tests: bulk operations, CSV export/import."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "bulkuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Bulk Corp",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_bulk_update_task_status(client: AsyncClient):
    headers = await _auth(client)
    # Create 3 tasks
    ids = []
    for i in range(3):
        resp = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "title": f"Bulk task {i}",
            },
        )
        assert resp.status_code == 201
        ids.append(resp.json()["id"])

    # Bulk update status
    resp = await client.post(
        "/api/v1/bulk/tasks/status",
        headers=headers,
        json={
            "task_ids": ids,
            "status": "in_progress",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 3

    # Verify all changed
    for tid in ids:
        resp = await client.get(f"/api/v1/tasks/{tid}", headers=headers)
        assert resp.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_bulk_delete_documents(client: AsyncClient):
    headers = await _auth(client)
    # Upload 3 documents
    doc_ids = []
    for i in range(3):
        resp = await client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": (f"doc{i}.txt", f"content {i}".encode(), "text/plain")},
        )
        assert resp.status_code == 201
        doc_ids.append(resp.json()["id"])

    # Bulk delete 2
    resp = await client.post(
        "/api/v1/bulk/documents/delete",
        headers=headers,
        json={
            "document_ids": doc_ids[:2],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 2

    # Verify 1 remains
    resp = await client.get("/api/v1/documents", headers=headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == doc_ids[2]


@pytest.mark.asyncio
async def test_export_tasks_csv(client: AsyncClient):
    headers = await _auth(client)
    # Create tasks
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Export task A",
            "priority": 1,
        },
    )
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Export task B",
            "priority": 5,
        },
    )

    resp = await client.get("/api/v1/bulk/export/tasks", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    lines = resp.text.strip().split("\n")
    assert len(lines) == 3  # header + 2 rows
    header = lines[0]
    assert "id" in header
    assert "title" in header
    assert "status" in header
    assert "priority" in header
    # Verify task data present
    assert "Export task A" in resp.text
    assert "Export task B" in resp.text


@pytest.mark.asyncio
async def test_export_clients_csv(client: AsyncClient):
    headers = await _auth(client)
    # Create clients
    await client.post(
        "/api/v1/clients",
        headers=headers,
        json={
            "name": "Alice Smith",
            "email": "alice@example.com",
            "phone": "555-0001",
        },
    )
    await client.post(
        "/api/v1/clients",
        headers=headers,
        json={
            "name": "Bob Jones",
            "email": "bob@example.com",
        },
    )

    resp = await client.get("/api/v1/bulk/export/clients", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    lines = resp.text.strip().split("\n")
    assert len(lines) == 3  # header + 2 rows
    assert "Alice Smith" in resp.text
    assert "Bob Jones" in resp.text


@pytest.mark.asyncio
async def test_import_tasks_csv(client: AsyncClient):
    headers = await _auth(client)
    csv_content = (
        "title,description,priority,status,deadline\n"
        "Imported task 1,First imported,2,pending,\n"
        "Imported task 2,Second imported,4,in_progress,\n"
        "Imported task 3,,1,pending,\n"
    )
    resp = await client.post(
        "/api/v1/bulk/import/tasks",
        headers=headers,
        files={"file": ("tasks.csv", csv_content.encode(), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 3

    # Verify tasks were created
    resp = await client.get("/api/v1/tasks", headers=headers)
    assert resp.json()["total"] == 3
    titles = {t["title"] for t in resp.json()["items"]}
    assert "Imported task 1" in titles
    assert "Imported task 2" in titles
    assert "Imported task 3" in titles
