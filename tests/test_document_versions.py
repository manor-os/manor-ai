"""E2E tests: document versioning and trash/restore."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "veruser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _upload(client: AsyncClient, headers: dict, name: str = "file.txt") -> dict:
    resp = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (name, b"hello world", "text/plain")},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
async def test_create_version(client: AsyncClient):
    headers = await _auth(client)
    doc = await _upload(client, headers)

    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
        json={"change_summary": "Initial snapshot"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["version_number"] == 1
    assert data["name"] == "file.txt"
    assert data["change_summary"] == "Initial snapshot"
    assert data["created_by"] == "veruser"


@pytest.mark.asyncio
async def test_list_versions(client: AsyncClient):
    headers = await _auth(client, "veruser2")
    doc = await _upload(client, headers)

    await client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
        json={"change_summary": "v1"},
    )
    await client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
        json={"change_summary": "v2"},
    )

    resp = await client.get(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
    )
    assert resp.status_code == 200
    versions = resp.json()
    assert len(versions) == 2
    # Newest first
    assert versions[0]["version_number"] == 2
    assert versions[1]["version_number"] == 1


@pytest.mark.asyncio
async def test_trash_document(client: AsyncClient):
    headers = await _auth(client, "veruser3")
    doc = await _upload(client, headers)

    # Trash
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/trash",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["trashed"] is True

    # Should not appear in normal list
    list_resp = await client.get("/api/v1/documents", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0

    # Should appear in trash list
    trash_resp = await client.get("/api/v1/documents/trash", headers=headers)
    assert trash_resp.status_code == 200
    assert len(trash_resp.json()) == 1
    assert trash_resp.json()[0]["id"] == doc["id"]


@pytest.mark.asyncio
async def test_restore_document(client: AsyncClient):
    headers = await _auth(client, "veruser4")
    doc = await _upload(client, headers)

    # Trash then restore
    await client.post(f"/api/v1/documents/{doc['id']}/trash", headers=headers)
    resp = await client.post(f"/api/v1/documents/{doc['id']}/restore", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["restored"] is True

    # Should be back in normal list
    list_resp = await client.get("/api/v1/documents", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1
    assert list_resp.json()["items"][0]["id"] == doc["id"]

    # Should not be in trash
    trash_resp = await client.get("/api/v1/documents/trash", headers=headers)
    assert len(trash_resp.json()) == 0


@pytest.mark.asyncio
async def test_empty_trash(client: AsyncClient):
    headers = await _auth(client, "veruser5")
    doc1 = await _upload(client, headers, "a.txt")
    doc2 = await _upload(client, headers, "b.txt")

    # Trash both
    await client.post(f"/api/v1/documents/{doc1['id']}/trash", headers=headers)
    await client.post(f"/api/v1/documents/{doc2['id']}/trash", headers=headers)

    # Empty trash
    resp = await client.post("/api/v1/documents/trash/empty", headers=headers)
    assert resp.status_code == 204

    # Trash should be empty
    trash_resp = await client.get("/api/v1/documents/trash", headers=headers)
    assert len(trash_resp.json()) == 0

    # Normal list also empty (docs permanently deleted)
    list_resp = await client.get("/api/v1/documents", headers=headers)
    assert list_resp.json()["total"] == 0
