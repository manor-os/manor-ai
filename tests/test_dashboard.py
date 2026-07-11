"""E2E tests: dashboard analytics — stats, task trends, usage trends, activity."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Task
from packages.core.models.usage import TokenUsageLog


async def _auth(client: AsyncClient, username: str = "dashuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Dash Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _task_payload(**overrides) -> dict:
    base = {"title": "Test task", "status": "pending", "priority": 3}
    base.update(overrides)
    return base


def _usage_payload(**overrides) -> dict:
    base = {
        "model": "gpt-4o",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": 0.0025,
        "source": "chat",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_dashboard_layout_defaults_and_persists(client: AsyncClient):
    headers = await _auth(client, "dashlayout")

    default_response = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert default_response.status_code == 200
    defaults = default_response.json()
    assert defaults["version"] == 1
    assert [widget["id"] for widget in defaults["widgets"]] == [
        "daily_brief",
        "time_saved",
        "total_tasks",
        "tasks_running",
        "activity",
        "workspaces",
        "task_trend",
    ]
    assert all(widget["visible"] for widget in defaults["widgets"])

    customized = [
        {"id": "daily_brief", "visible": True},
        {"id": "tasks_running", "visible": False},
        {"id": "total_tasks", "visible": True},
        {"id": "time_saved", "visible": True},
        {"id": "task_trend", "visible": False},
        {"id": "workspaces", "visible": True},
        {"id": "activity", "visible": True},
    ]
    update_response = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={"widgets": customized},
    )
    assert update_response.status_code == 200
    assert update_response.json()["widgets"] == customized

    persisted_response = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert persisted_response.status_code == 200
    assert persisted_response.json()["widgets"] == customized

    other_headers = await _auth(client, "dashlayoutother")
    other_response = await client.get(
        "/api/v1/dashboard/layout", headers=other_headers
    )
    assert other_response.status_code == 200
    assert all(widget["visible"] for widget in other_response.json()["widgets"])
    assert other_response.json()["widgets"] != customized


@pytest.mark.asyncio
async def test_dashboard_layout_rejects_unknown_and_duplicate_widgets(client: AsyncClient):
    headers = await _auth(client, "dashlayoutinvalid")

    unknown = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={"widgets": [{"id": "weather", "visible": True}]},
    )
    assert unknown.status_code == 422

    duplicate = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={
            "widgets": [
                {"id": "daily_brief", "visible": True},
                {"id": "daily_brief", "visible": False},
            ]
        },
    )
    assert duplicate.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_stats(client: AsyncClient):
    """Create tasks + docs and verify stats endpoint returns correct counts."""
    headers = await _auth(client, "dashstats")

    # Create tasks and transition them to different statuses
    r1 = await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    assert r2.status_code == 201
    r3 = await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    assert r3.status_code == 201

    # Transition task 2 to in_progress, task 3 to completed
    t2_id = r2.json()["id"]
    t3_id = r3.json()["id"]
    await client.put(f"/api/v1/tasks/{t2_id}", headers=headers, json={"status": "in_progress"})
    await client.put(f"/api/v1/tasks/{t3_id}", headers=headers, json={"status": "completed"})

    # Log some usage
    await client.post("/api/v1/usage", headers=headers, json=_usage_payload(total_tokens=200))
    await client.post("/api/v1/usage", headers=headers, json=_usage_payload(total_tokens=300))

    resp = await client.get("/api/v1/dashboard/stats", headers=headers)
    assert resp.status_code == 200
    stats = resp.json()

    # Task counts
    assert stats["tasks"]["total"] == 3
    assert stats["tasks"]["by_status"]["pending"] == 1
    assert stats["tasks"]["by_status"]["in_progress"] == 1
    assert stats["tasks"]["by_status"]["completed"] == 1

    # Usage totals
    assert stats["usage"]["total_tokens"] == 500
    assert stats["usage"]["today_tokens"] == 500

    # Documents should be zero (none created)
    assert stats["documents"]["total"] == 0


@pytest.mark.asyncio
async def test_dashboard_stats_use_user_timezone_for_today_and_overdue(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    headers = await _auth(client, "dash_timezone")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    tz_update = await client.put(
        "/api/v1/auth/me",
        headers=headers,
        json={"timezone": "America/Los_Angeles"},
    )
    assert tz_update.status_code == 200

    # 2026-05-02 06:30 UTC is still 2026-05-01 in Los Angeles.
    fixed_now = datetime(2026, 5, 2, 6, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "packages.core.services.analytics_service.utc_now",
        lambda: fixed_now,
    )

    entity_id = me["entity_id"]
    user_id = me["id"]
    db_session.add_all(
        [
            Task(
                id=generate_ulid(),
                entity_id=entity_id,
                title="Due on local today",
                status="pending",
                priority=3,
                task_type="general",
                details={},
                deadline=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
            Task(
                id=generate_ulid(),
                entity_id=entity_id,
                title="Due before local today",
                status="pending",
                priority=3,
                task_type="general",
                details={},
                deadline=datetime(2026, 4, 30, tzinfo=timezone.utc),
            ),
            Conversation(
                id=generate_ulid(),
                entity_id=entity_id,
                user_id=user_id,
                title="Local today conversation",
                channel="web",
                status="active",
                scope="channel",
                created_at=datetime(2026, 5, 2, 6, 0, tzinfo=timezone.utc),
            ),
            Conversation(
                id=generate_ulid(),
                entity_id=entity_id,
                user_id=user_id,
                title="Previous local day conversation",
                channel="web",
                status="active",
                scope="channel",
                created_at=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            ),
            TokenUsageLog(
                id=generate_ulid(),
                entity_id=entity_id,
                model="gpt-4o",
                provider="openai",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                source="chat",
                created_at=datetime(2026, 5, 2, 6, 0, tzinfo=timezone.utc),
            ),
            TokenUsageLog(
                id=generate_ulid(),
                entity_id=entity_id,
                model="gpt-4o",
                provider="openai",
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                source="chat",
                created_at=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/v1/dashboard/stats", headers=headers)
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["tasks"]["overdue"] == 1
    assert stats["conversations"]["today"] == 1
    assert stats["usage"]["today_tokens"] == 15


@pytest.mark.asyncio
async def test_task_trends(client: AsyncClient):
    """Create tasks and verify date grouping in task-trends endpoint."""
    headers = await _auth(client, "dashtrends")

    # Create several tasks (all today)
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload())

    resp = await client.get("/api/v1/dashboard/task-trends?days=7", headers=headers)
    assert resp.status_code == 200
    trends = resp.json()

    # Should have at least one date entry for today
    assert len(trends) >= 1
    today_entry = trends[-1]  # sorted by date, today should be last
    assert today_entry["created"] == 3
    assert "date" in today_entry


@pytest.mark.asyncio
async def test_usage_trends(client: AsyncClient):
    """Log usage and verify date grouping in usage-trends endpoint."""
    headers = await _auth(client, "dashusage")

    await client.post("/api/v1/usage", headers=headers, json=_usage_payload(total_tokens=100, cost_usd=0.001))
    await client.post("/api/v1/usage", headers=headers, json=_usage_payload(total_tokens=200, cost_usd=0.002))

    resp = await client.get("/api/v1/dashboard/usage-trends?days=7", headers=headers)
    assert resp.status_code == 200
    trends = resp.json()

    assert len(trends) >= 1
    today_entry = trends[-1]
    assert today_entry["tokens"] == 300
    assert today_entry["cost"] == pytest.approx(0.003, abs=1e-6)
    assert "date" in today_entry


@pytest.mark.asyncio
async def test_recent_activity(client: AsyncClient):
    """Create tasks + docs and verify activity items are returned."""
    headers = await _auth(client, "dashactivity")

    # Create some tasks
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload(title="Task Alpha"))
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload(title="Task Beta"))

    resp = await client.get("/api/v1/dashboard/recent-activity?limit=10", headers=headers)
    assert resp.status_code == 200
    activity = resp.json()

    assert len(activity) >= 2
    # All items should have the required fields
    for item in activity:
        assert "type" in item
        assert "id" in item
        assert "name" in item
        assert "action" in item
        assert "timestamp" in item

    # Should include our tasks
    task_names = [a["name"] for a in activity if a["type"] == "task"]
    actions_by_name = {a["name"]: a["action"] for a in activity if a["type"] == "task"}
    assert "Task Alpha" in task_names
    assert "Task Beta" in task_names
    assert actions_by_name["Task Alpha"] == "created"
    assert actions_by_name["Task Beta"] == "created"


@pytest.mark.asyncio
async def test_recent_activity_filters_workspace_and_automation_tasks(client: AsyncClient):
    """Recent activity should respect workspace filters and hide scheduler internals."""
    headers = await _auth(client, "dashactivityscope")
    ws_a = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Activity A"})
    ws_b = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Activity B"})
    ws_a_id = ws_a.json()["id"]
    ws_b_id = ws_b.json()["id"]

    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json=_task_payload(title="A visible task", workspace_id=ws_a_id),
    )
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json=_task_payload(title="B hidden by workspace filter", workspace_id=ws_b_id),
    )
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json=_task_payload(
            title="Automation hidden",
            workspace_id=ws_a_id,
            details={"scheduled_job_id": "job_123"},
        ),
    )

    resp = await client.get(f"/api/v1/dashboard/recent-activity?limit=10&workspace_id={ws_a_id}", headers=headers)

    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == ["A visible task"]
