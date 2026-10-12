"""E2E tests: Kanban board view — grouped tasks, move between columns."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.task_service import get_tasks_by_status


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
async def test_task_board_pages_all_rows_without_leaking_filtered_tasks(client: AsyncClient, db_session):
    from tests.test_document_permissions import _create_entity_user

    headers = await _auth(client, "boardpagination")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]
    reader = await _create_entity_user(entity_id, "boardpagereader", role="member")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    visible = Workspace(id=generate_ulid(), entity_id=entity_id, name="Readable", settings={"access_mode": "members_only"})
    private = Workspace(id=generate_ulid(), entity_id=entity_id, name="Private", settings={"access_mode": "members_only"})
    trashed = Workspace(id=generate_ulid(), entity_id=entity_id, name="Trashed", deleted_at=now)
    db_session.add_all([visible, private, trashed])
    db_session.add(WorkspaceStaff(workspace_id=visible.id, user_id=reader["id"], role="viewer", status="active"))

    failed = [
        Task(
            id=generate_ulid(), entity_id=entity_id, workspace_id=visible.id,
            title=f"Failed task {i}", status="failed", priority=i % 3,
            created_at=now + timedelta(seconds=i // 3), details={},
        )
        for i in range(105)
    ]
    pending = Task(id=generate_ulid(), entity_id=entity_id, title="Entity task", status="pending", details={})
    hidden = [
        Task(entity_id=entity_id, workspace_id=private.id, title="Private task", status="failed", priority=99, details={}),
        Task(entity_id=entity_id, workspace_id=trashed.id, title="Trashed task", status="failed", priority=99, details={}),
        Task(entity_id=entity_id, workspace_id=visible.id, title="Automation task", status="failed", priority=99, details={"scheduled_job_id": "job-a"}),
        Task(entity_id=generate_ulid(), title="Other entity task", status="failed", priority=99, details={}),
    ]
    db_session.add_all([*failed, pending, *hidden])
    await db_session.commit()
    expected = [task.id for task in sorted(failed, key=lambda task: (task.priority, task.created_at, task.id), reverse=True)]

    pages = []
    for offset, page_size in [(0, 50), (50, 50), (100, 5), (150, 0)]:
        response = await client.get(
            "/api/v1/tasks/board",
            headers=reader["headers"],
            params={"limit_per_status": 50, "offset_per_status": offset},
        )
        assert response.status_code == 200, response.text
        board = response.json()
        ids = [task["id"] for task in board["failed"]]
        assert len(ids) == page_size
        assert ids == expected[offset:offset + 50]
        assert board["_counts"] == {"failed": 105, "pending": 1}
        assert [task["id"] for task in board["pending"]] == ([pending.id] if offset == 0 else [])
        pages.extend(ids)
    assert pages == expected
    assert len(set(pages)) == 105

    scoped = await client.get(
        "/api/v1/tasks/board",
        headers=reader["headers"],
        params={"workspaceId": visible.id, "limit_per_status": 50, "offset_per_status": 100},
    )
    assert scoped.status_code == 200, scoped.text
    assert [task["id"] for task in scoped.json()["failed"]] == expected[100:]
    assert scoped.json()["_counts"] == {"failed": 105}
    for offset in (0, 50, 100):
        denied = await client.get(
            "/api/v1/tasks/board",
            headers=reader["headers"],
            params={"workspace_id": private.id, "offset_per_status": offset},
        )
        assert denied.status_code == 404


@pytest.mark.asyncio
async def test_board_service_pages_ties_and_automation_opt_in(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [
        Task(
            id=f"board-tie-{i:016d}", entity_id=entity_id, workspace_id=workspace_id,
            title=f"Task {i}", status="failed", priority=3, created_at=now,
            details={"scheduled_job_id": "job-a"} if i == 2 else {},
        )
        for i in range(3)
    ]
    db_session.add_all(tasks)
    await db_session.flush()

    for include_automations, expected in [(False, [tasks[1].id, tasks[0].id]), (True, [tasks[2].id, tasks[1].id, tasks[0].id])]:
        actual = []
        for offset in range(len(expected) + 1):
            board = await get_tasks_by_status(
                db_session, entity_id, workspace_id=workspace_id,
                readable_workspace_ids={workspace_id},
                include_automations=include_automations,
                limit_per_status=1, offset_per_status=offset,
            )
            assert board["_counts"] == {"failed": len(expected)}
            actual.extend(task.id for task in board["failed"])
        assert actual == expected

    inaccessible = await get_tasks_by_status(
        db_session, entity_id, readable_workspace_ids=set(),
        include_automations=True, offset_per_status=1,
    )
    assert inaccessible == {"_counts": {}}


@pytest.mark.asyncio
async def test_task_board_preferences_are_user_scoped(client: AsyncClient):
    first_headers = await _auth(client, "boardprefsfirst")
    second_headers = await _auth(client, "boardprefssecond")

    default_response = await client.get(
        "/api/v1/tasks/board-preferences",
        headers=first_headers,
    )
    assert default_response.status_code == 200
    assert default_response.json() == {
        "mode": "kanban",
        "visible_columns": ["todo", "scheduled", "in_progress", "review", "done"],
        "column_order": ["todo", "scheduled", "in_progress", "review", "done"],
        "configured": False,
    }

    update_response = await client.put(
        "/api/v1/tasks/board-preferences",
        headers=first_headers,
        json={
            "mode": "list",
            "visible_columns": ["done", "todo", "done", "unknown"],
            "column_order": ["done", "todo"],
        },
    )
    assert update_response.status_code == 200
    assert update_response.json() == {
        "mode": "list",
        "visible_columns": ["done", "todo"],
        "column_order": ["done", "todo", "scheduled", "in_progress", "review"],
        "configured": True,
    }

    first_read = await client.get(
        "/api/v1/tasks/board-preferences",
        headers=first_headers,
    )
    assert first_read.json() == update_response.json()

    second_read = await client.get(
        "/api/v1/tasks/board-preferences",
        headers=second_headers,
    )
    assert second_read.status_code == 200
    assert second_read.json()["configured"] is False
    assert second_read.json()["mode"] == "kanban"


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
