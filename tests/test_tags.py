"""E2E tests: universal tagging system."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "taguser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Tag Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_create_and_list_tags(client: AsyncClient):
    headers = await _auth(client)

    # Create tags
    r1 = await client.post(
        "/api/v1/tags",
        headers=headers,
        json={
            "name": "urgent",
            "color": "#ff0000",
        },
    )
    assert r1.status_code == 201
    assert r1.json()["name"] == "urgent"
    assert r1.json()["color"] == "#ff0000"

    r2 = await client.post(
        "/api/v1/tags",
        headers=headers,
        json={
            "name": "q4-2026",
            "description": "Q4 2026 items",
        },
    )
    assert r2.status_code == 201

    # Duplicate returns existing
    r3 = await client.post("/api/v1/tags", headers=headers, json={"name": "urgent"})
    assert r3.status_code == 201
    assert r3.json()["id"] == r1.json()["id"]

    # List all
    resp = await client.get("/api/v1/tags", headers=headers)
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "urgent" in names
    assert "q4-2026" in names

    # Search prefix
    resp2 = await client.get("/api/v1/tags?search=urg", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1
    assert resp2.json()[0]["name"] == "urgent"


@pytest.mark.asyncio
async def test_tag_resource(client: AsyncClient):
    headers = await _auth(client)

    # Create a task to tag
    task_resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Tagged task",
        },
    )
    assert task_resp.status_code == 201
    task_id = task_resp.json()["id"]

    # Apply tag (auto-creates "priority" tag)
    resp = await client.post(
        "/api/v1/tags/apply",
        headers=headers,
        json={
            "tag_name": "priority",
            "resource_type": "task",
            "resource_id": task_id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    # Apply same tag again — not newly added
    resp2 = await client.post(
        "/api/v1/tags/apply",
        headers=headers,
        json={
            "tag_name": "priority",
            "resource_type": "task",
            "resource_id": task_id,
        },
    )
    assert resp2.json()["applied"] is False

    # Verify tags on the resource
    resp3 = await client.get(
        f"/api/v1/tags/resource?resource_type=task&resource_id={task_id}",
        headers=headers,
    )
    assert resp3.status_code == 200
    assert len(resp3.json()) == 1
    assert resp3.json()[0]["name"] == "priority"


@pytest.mark.asyncio
async def test_untag_resource(client: AsyncClient):
    headers = await _auth(client)

    task_resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Untag test task",
        },
    )
    task_id = task_resp.json()["id"]

    # Tag it
    await client.post(
        "/api/v1/tags/apply",
        headers=headers,
        json={
            "tag_name": "temp-tag",
            "resource_type": "task",
            "resource_id": task_id,
        },
    )

    # Verify tagged
    resp = await client.get(
        f"/api/v1/tags/resource?resource_type=task&resource_id={task_id}",
        headers=headers,
    )
    assert len(resp.json()) == 1

    # Untag
    resp2 = await client.post(
        "/api/v1/tags/remove",
        headers=headers,
        json={
            "tag_name": "temp-tag",
            "resource_type": "task",
            "resource_id": task_id,
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["removed"] is True

    # Verify removed
    resp3 = await client.get(
        f"/api/v1/tags/resource?resource_type=task&resource_id={task_id}",
        headers=headers,
    )
    assert len(resp3.json()) == 0


@pytest.mark.asyncio
async def test_find_by_tag(client: AsyncClient):
    headers = await _auth(client)

    # Create 2 tasks and 1 "document" (use a fake ID since we just need the tag link)
    t1 = await client.post("/api/v1/tasks", headers=headers, json={"title": "Find task 1"})
    t2 = await client.post("/api/v1/tasks", headers=headers, json={"title": "Find task 2"})
    task_id_1 = t1.json()["id"]
    task_id_2 = t2.json()["id"]
    fake_doc_id = "01JTEST00000DOCUMENT00001"

    # Tag all three with "shared-tag"
    for rt, rid in [("task", task_id_1), ("task", task_id_2), ("document", fake_doc_id)]:
        await client.post(
            "/api/v1/tags/apply",
            headers=headers,
            json={
                "tag_name": "shared-tag",
                "resource_type": rt,
                "resource_id": rid,
            },
        )

    # Find all by tag
    resp = await client.get("/api/v1/tags/find/shared-tag", headers=headers)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 3

    # Filter by resource_type
    resp2 = await client.get("/api/v1/tags/find/shared-tag?resource_type=task", headers=headers)
    assert len(resp2.json()) == 2

    resp3 = await client.get("/api/v1/tags/find/shared-tag?resource_type=document", headers=headers)
    assert len(resp3.json()) == 1
    assert resp3.json()[0]["resource_id"] == fake_doc_id


@pytest.mark.asyncio
async def test_popular_tags(client: AsyncClient):
    headers = await _auth(client)

    fake_ids = [f"01JTEST0000POPULAR{i:07d}" for i in range(5)]

    # "hot" tag on 3 resources, "warm" on 2, "cold" on 1
    for rid in fake_ids[:3]:
        await client.post(
            "/api/v1/tags/apply",
            headers=headers,
            json={
                "tag_name": "hot",
                "resource_type": "task",
                "resource_id": rid,
            },
        )
    for rid in fake_ids[:2]:
        await client.post(
            "/api/v1/tags/apply",
            headers=headers,
            json={
                "tag_name": "warm",
                "resource_type": "task",
                "resource_id": rid,
            },
        )
    await client.post(
        "/api/v1/tags/apply",
        headers=headers,
        json={
            "tag_name": "cold",
            "resource_type": "task",
            "resource_id": fake_ids[0],
        },
    )

    resp = await client.get("/api/v1/tags/popular", headers=headers)
    assert resp.status_code == 200
    popular = resp.json()
    assert len(popular) == 3
    # Ordered by count descending
    assert popular[0]["name"] == "hot"
    assert popular[0]["count"] == 3
    assert popular[1]["name"] == "warm"
    assert popular[1]["count"] == 2
    assert popular[2]["name"] == "cold"
    assert popular[2]["count"] == 1
