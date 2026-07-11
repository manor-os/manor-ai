"""E2E tests: agent conversation memory CRUD, context, archive, isolation."""

import pytest
from httpx import AsyncClient

from packages.core.memory.service import load_memory_block


async def _auth(client: AsyncClient, username: str = "memuser") -> dict:
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
async def test_add_and_list_memories(client: AsyncClient):
    headers = await _auth(client)

    # Add two memories
    r1 = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "User prefers weekly email reports",
            "memory_type": "preference",
            "importance": 8,
        },
    )
    assert r1.status_code == 201
    data1 = r1.json()
    assert data1["content"] == "User prefers weekly email reports"
    assert data1["memory_type"] == "preference"
    assert data1["importance"] == 8
    assert data1["status"] == "active"

    r2 = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Company manages 50 rental units",
            "memory_type": "context",
            "importance": 6,
        },
    )
    assert r2.status_code == 201

    # List all
    resp = await client.get("/api/v1/memories", headers=headers)
    assert resp.status_code == 200
    mems = resp.json()
    assert len(mems) == 2
    # Should be sorted by importance DESC
    assert mems[0]["importance"] >= mems[1]["importance"]

    # List filtered by type
    resp2 = await client.get("/api/v1/memories?type=preference", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1
    assert resp2.json()[0]["memory_type"] == "preference"


@pytest.mark.asyncio
async def test_get_context_memories(client: AsyncClient):
    headers = await _auth(client, "memuser_ctx")

    # Add memories with different importance
    await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Low importance fact",
            "memory_type": "fact",
            "importance": 2,
        },
    )
    await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "High importance preference",
            "memory_type": "preference",
            "importance": 9,
        },
    )
    await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Medium importance context",
            "memory_type": "context",
            "importance": 5,
        },
    )

    resp = await client.get("/api/v1/memories/context", headers=headers)
    assert resp.status_code == 200
    ctx = resp.json()["context"]
    assert "## Your Memory" in ctx
    assert "[preference] High importance preference" in ctx
    assert "[fact] Low importance fact" in ctx

    # Verify ordering: high importance line appears before low importance line
    high_pos = ctx.index("High importance preference")
    low_pos = ctx.index("Low importance fact")
    assert high_pos < low_pos


@pytest.mark.asyncio
async def test_context_memories_compact_oversized_entries(client: AsyncClient):
    headers = await _auth(client, "memuser_long_ctx")

    await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Long preference starts. " + ("A" * 12_000) + " Long preference tail.",
            "memory_type": "preference",
            "importance": 10,
        },
    )
    await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Short memory should still be loaded",
            "memory_type": "fact",
            "importance": 5,
        },
    )

    resp = await client.get("/api/v1/memories/context", headers=headers)
    assert resp.status_code == 200
    ctx = resp.json()["context"]
    assert "Long preference starts" in ctx
    assert "prompt budget" in ctx
    assert "Short memory should still be loaded" in ctx
    assert len(ctx) < 2_500


def test_workspace_memory_block_compacts_each_entry():
    block = load_memory_block(
        [
            {
                "title": "Huge learning",
                "scope": "learning",
                "confidence": 0.8,
                "content": "The workspace learned this. " + ("B" * 5_000) + " Keep this tail.",
            },
            {
                "title": "Small preference",
                "scope": "preference",
                "confidence": 0.9,
                "content": "Prefer weekly summaries before taking action.",
            },
        ],
        max_chars=2_200,
    )

    assert "Huge learning" in block
    assert "prompt budget" in block
    assert "Small preference" in block
    assert len(block) <= 2_200


@pytest.mark.asyncio
async def test_archive_memory(client: AsyncClient):
    headers = await _auth(client, "memuser_arc")

    create = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "content": "Will be archived",
            "memory_type": "fact",
        },
    )
    mem_id = create.json()["id"]

    # Archive
    resp = await client.post(f"/api/v1/memories/{mem_id}/archive", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"

    # Archived memory should not appear in list (list filters active only)
    resp2 = await client.get("/api/v1/memories", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()) == 0

    # Archived memory should not appear in context
    resp3 = await client.get("/api/v1/memories/context", headers=headers)
    assert resp3.json()["context"] == ""


@pytest.mark.asyncio
async def test_memory_isolation(client: AsyncClient):
    """User A's memories are not visible to user B."""
    headers_a = await _auth(client, "memuser_a")
    headers_b = await _auth(client, "memuser_b")

    # User A creates a memory
    await client.post(
        "/api/v1/memories",
        headers=headers_a,
        json={
            "content": "Secret A info",
            "memory_type": "fact",
        },
    )

    # User B creates a memory
    await client.post(
        "/api/v1/memories",
        headers=headers_b,
        json={
            "content": "Secret B info",
            "memory_type": "fact",
        },
    )

    # User A should only see their own
    resp_a = await client.get("/api/v1/memories", headers=headers_a)
    assert resp_a.status_code == 200
    mems_a = resp_a.json()
    assert len(mems_a) == 1
    assert mems_a[0]["content"] == "Secret A info"

    # User B should only see their own
    resp_b = await client.get("/api/v1/memories", headers=headers_b)
    assert resp_b.status_code == 200
    mems_b = resp_b.json()
    assert len(mems_b) == 1
    assert mems_b[0]["content"] == "Secret B info"
