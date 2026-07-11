"""E2E tests: Kanban board view — grouped tasks, move between columns."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "boarduser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Board Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_task_board_grouped(client: AsyncClient):
    """Create tasks with different statuses, fetch board, verify grouping."""
    headers = await _auth(client)

    # Create three tasks
    t1 = await client.post("/api/v1/tasks", headers=headers, json={"title": "Task A"})
    t2 = await client.post("/api/v1/tasks", headers=headers, json={"title": "Task B"})
    t3 = await client.post("/api/v1/tasks", headers=headers, json={"title": "Task C"})
    assert t1.status_code == 201
    assert t2.status_code == 201
    assert t3.status_code == 201

    # Move t2 to in_progress, t3 to completed
    await client.put(
        f"/api/v1/tasks/{t2.json()['id']}",
        headers=headers,
        json={"status": "in_progress"},
    )
    await client.put(
        f"/api/v1/tasks/{t3.json()['id']}",
        headers=headers,
        json={"status": "completed"},
    )

    # Fetch board
    resp = await client.get("/api/v1/tasks/board", headers=headers)
    assert resp.status_code == 200
    board = resp.json()

    assert len(board.get("pending", [])) == 1
    assert board["pending"][0]["title"] == "Task A"
    assert len(board.get("in_progress", [])) == 1
    assert board["in_progress"][0]["title"] == "Task B"
    assert len(board.get("completed", [])) == 1
    assert board["completed"][0]["title"] == "Task C"


@pytest.mark.asyncio
async def test_move_task(client: AsyncClient):
    """Move a task from pending to in_progress via the move endpoint, verify started_at is set."""
    headers = await _auth(client, "boardmove")

    resp = await client.post("/api/v1/tasks", headers=headers, json={"title": "Movable"})
    assert resp.status_code == 201
    task_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"
    assert resp.json()["started_at"] is None

    # Move to in_progress
    move_resp = await client.post(
        f"/api/v1/tasks/{task_id}/move",
        headers=headers,
        json={"status": "in_progress"},
    )
    assert move_resp.status_code == 200
    data = move_resp.json()
    assert data["status"] == "in_progress"
    assert data["started_at"] is not None


@pytest.mark.asyncio
async def test_move_to_completed(client: AsyncClient):
    """Move a task to completed, verify completed_at is set."""
    headers = await _auth(client, "boarddone")

    resp = await client.post("/api/v1/tasks", headers=headers, json={"title": "Finish me"})
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Move directly to completed
    move_resp = await client.post(
        f"/api/v1/tasks/{task_id}/move",
        headers=headers,
        json={"status": "completed"},
    )
    assert move_resp.status_code == 200
    data = move_resp.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None
