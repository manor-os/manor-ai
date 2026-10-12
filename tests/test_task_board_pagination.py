"""Database-free checks for the bounded, scoped task board pagination contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from apps.api.routers import tasks as task_routes
from packages.core.models.task import Task
from packages.core.services import task_service, workspace_access


@pytest.mark.asyncio
async def test_board_service_bounds_rows_in_sql_and_keeps_full_counts():
    counts = MagicMock()
    counts.all.return_value = [("failed", 105), ("pending", 1)]
    page = MagicMock()
    task = Task(id="last-page-task", status="failed")
    page.scalars.return_value.all.return_value = [task]
    db = SimpleNamespace(execute=AsyncMock(side_effect=[counts, page]))

    board = await task_service.get_tasks_by_status(
        db,
        "entity-a",
        workspace_id="workspace-a",
        readable_workspace_ids={"workspace-a"},
        limit_per_status=50,
        offset_per_status=100,
    )

    assert board == {"failed": [task], "pending": [], "_counts": {"failed": 105, "pending": 1}}
    count_query, page_query = [
        str(call.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for call in db.execute.await_args_list
    ]
    assert "GROUP BY" in count_query
    assert "row_number() OVER (PARTITION BY" in page_query
    assert "priority DESC" in page_query
    assert "created_at DESC" in page_query
    assert "id DESC" in page_query
    assert "status_position > 100" in page_query
    assert "status_position <= 150" in page_query
    for query in (count_query, page_query):
        assert "tasks.entity_id = 'entity-a'" in query
        assert "tasks.workspace_id = 'workspace-a'" in query
        assert "tasks.workspace_id IN ('workspace-a')" in query
        assert "workspaces.deleted_at IS NOT NULL" in query
        assert "scheduled_job_id" in query


def _board_app(monkeypatch):
    app = FastAPI()
    user = SimpleNamespace(id="user-a", entity_id="entity-a", role="member")
    db = object()
    app.dependency_overrides[task_routes.get_current_user] = lambda: user
    app.dependency_overrides[task_routes.get_db] = lambda: db
    monkeypatch.setattr(task_routes, "require_workspace_readable", AsyncMock())
    monkeypatch.setattr(workspace_access, "readable_workspace_ids_for_user", AsyncMock(return_value={"workspace-a"}))
    board_service = AsyncMock(return_value={"_counts": {}})
    monkeypatch.setattr(task_routes, "get_tasks_by_status", board_service)
    monkeypatch.setattr(task_routes, "_resolve_lookups", AsyncMock(return_value=({}, {}, {}, {}, {}, {})))
    monkeypatch.setattr(task_routes, "_active_execution_task_ids", AsyncMock(return_value=set()))
    app.get("/board")(task_routes.task_board)
    return app, board_service, db


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["limit_per_status=0", "limit_per_status=201", "offset_per_status=-1"])
async def test_board_rejects_invalid_page_bounds(monkeypatch, query):
    app, board_service, _db = _board_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.get(f"/board?{query}")
    assert response.status_code == 422
    board_service.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("query,limit,offset", [("", 50, 0), ("&limit_per_status=200&offset_per_status=100", 200, 100)])
async def test_board_forwards_page_and_existing_read_scope(monkeypatch, query, limit, offset):
    app, board_service, db = _board_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.get(f"/board?workspaceId=workspace-a{query}")
    assert response.status_code == 200
    board_service.assert_awaited_once_with(
        db,
        "entity-a",
        workspace_id="workspace-a",
        readable_workspace_ids={"workspace-a"},
        limit_per_status=limit,
        offset_per_status=offset,
    )
