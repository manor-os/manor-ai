"""E2E tests: global search endpoint."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "searchuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_global_search(client: AsyncClient):
    headers = await _auth(client)

    # Create some tasks
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Fix plumbing issue",
            "description": "Kitchen sink leaking",
        },
    )
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Unrelated task",
        },
    )

    # Upload a document
    await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("plumbing_report.txt", b"report content", "text/plain")},
    )

    # Search for "plumbing"
    resp = await client.get("/api/v1/search?q=plumbing", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    # Should find the task and the document
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["name"] == "Fix plumbing issue"
    assert len(data["documents"]) == 1
    assert data["documents"][0]["name"] == "plumbing_report.txt"
    # No matching agents or conversations
    assert len(data["agents"]) == 0
    assert len(data["conversations"]) == 0


@pytest.mark.asyncio
async def test_search_empty_query(client: AsyncClient):
    headers = await _auth(client)

    resp = await client.get("/api/v1/search?q=", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []
    assert data["documents"] == []
    assert data["agents"] == []
    assert data["conversations"] == []
