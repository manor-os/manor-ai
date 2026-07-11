"""E2E tests: report generation — task, usage, activity reports + HTML endpoint."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "reportuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Report Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _task_payload(**overrides) -> dict:
    base = {"title": "Report task", "status": "pending", "priority": 3}
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
async def test_task_report(client: AsyncClient):
    """Create tasks, generate report, verify HTML contains stats."""
    headers = await _auth(client, "rpt_task")

    # Create tasks in different statuses
    r1 = await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/tasks", headers=headers, json=_task_payload())
    assert r2.status_code == 201
    t2_id = r2.json()["id"]
    await client.put(f"/api/v1/tasks/{t2_id}", headers=headers, json={"status": "completed"})

    resp = await client.get("/api/v1/reports/tasks?days=30", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "html" in body
    assert "Task Report" in body["title"]
    assert body["text_summary"]  # non-empty
    assert "generated_at" in body
    # HTML should contain stats
    assert "Total Tasks" in body["html"]
    assert "Completed" in body["html"]


@pytest.mark.asyncio
async def test_usage_report(client: AsyncClient):
    """Log usage, generate report, verify data."""
    headers = await _auth(client, "rpt_usage")

    await client.post("/api/v1/usage", headers=headers, json=_usage_payload(total_tokens=500, cost_usd=0.01))
    await client.post("/api/v1/usage", headers=headers, json=_usage_payload(total_tokens=300, cost_usd=0.005))

    resp = await client.get("/api/v1/reports/usage?days=30", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "Usage Report" in body["title"]
    assert "html" in body
    assert "Total Tokens" in body["html"]
    assert body["text_summary"]
    assert body["generated_at"]


@pytest.mark.asyncio
async def test_activity_report(client: AsyncClient):
    """Create activity, generate digest."""
    headers = await _auth(client, "rpt_activity")

    # Create tasks to generate activity
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload(title="Alpha"))
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload(title="Beta"))

    resp = await client.get("/api/v1/reports/activity?days=7", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "Activity Digest" in body["title"]
    assert "html" in body
    assert body["generated_at"]


@pytest.mark.asyncio
async def test_report_html_endpoint(client: AsyncClient):
    """Verify /tasks/html returns content-type text/html."""
    headers = await _auth(client, "rpt_html")

    # Create a task so the report has data
    await client.post("/api/v1/tasks", headers=headers, json=_task_payload())

    resp = await client.get("/api/v1/reports/tasks/html?days=30", headers=headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<!DOCTYPE html>" in resp.text
    assert "Manor AI" in resp.text
    assert "Task Report" in resp.text
