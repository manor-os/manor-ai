from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


async def _auth(client, username: str = "summaryuser") -> tuple[dict, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Summary Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["entity_id"]


@pytest.mark.asyncio
async def test_workspace_daily_summary_uses_real_workspace_data(client):
    import packages.core.database as db_module
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.task import Task
    from packages.core.services.workspace_daily_summary_service import (
        get_workspace_daily_summary,
    )

    headers, entity_id = await _auth(client, "daily_summary_real")
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Launch Room"},
    )
    workspace_id = ws_resp.json()["id"]

    summary_day = datetime(2026, 1, 2, tzinfo=timezone.utc)
    today = datetime(2026, 1, 3, tzinfo=timezone.utc)

    async with db_module.async_session() as db:
        db.add_all(
            [
                Task(
                    id=generate_ulid(),
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    title="Ship launch page",
                    status="completed",
                    priority=4,
                    details={},
                    created_at=summary_day,
                    completed_at=summary_day.replace(hour=16),
                ),
                Task(
                    id=generate_ulid(),
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    title="Review legal copy",
                    status="waiting_on_customer",
                    priority=5,
                    details={},
                    created_at=summary_day.replace(hour=9),
                ),
                Task(
                    id=generate_ulid(),
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    title="Fix failed campaign run",
                    status="failed",
                    priority=4,
                    details={},
                    created_at=summary_day.replace(hour=10),
                    updated_at=summary_day.replace(hour=11),
                ),
                Task(
                    id=generate_ulid(),
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    title="Publish morning post",
                    status="pending",
                    priority=5,
                    details={},
                    created_at=summary_day.replace(hour=12),
                    deadline=today.replace(hour=15),
                ),
            ]
        )

        plan_id = generate_ulid()
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                status="running",
                plan_dag={},
                cost_tracking={},
                dispatcher_state={},
            )
        )
        db.add(
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="approval",
                kind="human",
                params={},
                depends_on=[],
                evidence_refs=[],
                cost={},
                step_status="waiting_human",
            )
        )
        db.add(
            ScheduledJob(
                id=generate_ulid(),
                job_id="summary-broken-job",
                entity_id=entity_id,
                workspace_id=workspace_id,
                name="Broken summary job",
                job_type="cron",
                schedule_kind="cron",
                cron_expr="0 8 * * *",
                execution_type="skill",
                execution_target={},
                enabled=True,
                last_status="error",
                consecutive_errors=3,
            )
        )
        await db.commit()

    async with db_module.async_session() as db:
        summary = await get_workspace_daily_summary(
            db,
            entity_id,
            workspace_id,
            date="2026-01-02",
            timezone_name="UTC",
        )

    assert summary["data_quality"]["source"] == "database"
    assert summary["data_quality"]["simulated"] is False
    assert summary["yesterday_outcomes"]["completed_count"] == 1
    assert summary["yesterday_outcomes"]["failed_count"] == 1
    assert summary["current_health"]["by_status"]["waiting_on_customer"] == 1
    assert summary["current_health"]["automations"]["broken_count"] == 1
    assert summary["current_health"]["executions"]["waiting_human_plans"] == 1
    assert summary["today_focus"]["due_today_count"] == 1
    assert any("Review legal copy" == t["title"] for t in summary["needs_human_handling"]["waiting_tasks"])


@pytest.mark.asyncio
async def test_manor_action_exposes_workspace_daily_summary(client):
    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action, _manor_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task

    headers, entity_id = await _auth(client, "daily_summary_tool")
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Ops Room"},
    )
    workspace_id = ws_resp.json()["id"]

    async with db_module.async_session() as db:
        db.add(
            Task(
                id=generate_ulid(),
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Done yesterday",
                status="completed",
                priority=3,
                details={},
                created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                completed_at=datetime(2026, 2, 1, 12, tzinfo=timezone.utc),
            )
        )
        await db.commit()

    result = json.loads(
        await _dispatch_action(
            "get_workspace_daily_summary",
            {"workspace_id": workspace_id, "date": "2026-02-01", "timezone": "UTC"},
            entity_id,
        )
    )
    assert result["kind"] == "workspace_daily_summary"
    assert result["workspace"]["id"] == workspace_id
    assert result["yesterday_outcomes"]["completed_count"] == 1

    search = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            action="search",
            query="workspace daily summary handoff health",
        )
    )
    assert "get_workspace_daily_summary" in {item["action"] for item in search["matches"]}


@pytest.mark.asyncio
async def test_daily_briefing_template_installs_schedule(client):
    import packages.core.database as db_module
    import packages.core.templates.recipes  # noqa: F401
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.templates import TemplateInput, apply_template
    from sqlalchemy import select

    headers, entity_id = await _auth(client, "daily_briefing_template")
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Briefing Room"},
    )
    workspace_id = ws_resp.json()["id"]

    async with db_module.async_session() as db:
        result = await apply_template(
            db,
            "daily_briefing",
            TemplateInput(
                entity_id=entity_id,
                workspace_id=workspace_id,
                params={"post_time_local": "09:15", "tz": "America/Los_Angeles"},
            ),
        )
        await db.commit()

        job = (
            await db.execute(select(ScheduledJob).where(ScheduledJob.id == result.scheduled_job_ids[0]))
        ).scalar_one()

    assert len(result.task_ids) == 1
    assert len(result.scheduled_job_ids) == 1
    assert job.execution_type == "briefing"
    assert job.execution_target == {"workspace_id": workspace_id}
    assert job.cron_expr == "15 9 * * *"
    assert job.timezone == "America/Los_Angeles"


@pytest.mark.asyncio
async def test_daily_briefing_template_uses_user_schedule_settings(client):
    import packages.core.database as db_module
    import packages.core.templates.recipes  # noqa: F401
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.templates import TemplateInput, apply_template
    from sqlalchemy import select

    headers, entity_id = await _auth(client, "daily_briefing_user_time")
    me_resp = await client.get("/api/v1/auth/me", headers=headers)
    user_id = me_resp.json()["id"]
    await client.put(
        "/api/v1/auth/me",
        headers=headers,
        json={"timezone": "Asia/Tokyo"},
    )
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "User Schedule Room"},
    )
    workspace_id = ws_resp.json()["id"]

    async with db_module.async_session() as db:
        result = await apply_template(
            db,
            "daily_briefing",
            TemplateInput(
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                params={},
            ),
        )
        await db.commit()

        job = (
            await db.execute(select(ScheduledJob).where(ScheduledJob.id == result.scheduled_job_ids[0]))
        ).scalar_one()

    assert job.user_id == user_id
    assert job.cron_expr == "0 8 * * *"
    assert job.timezone == "Asia/Tokyo"

    await client.put(
        "/api/v1/admin/preferences",
        headers=headers,
        json={"daily_briefing_time": "07:45"},
    )

    async with db_module.async_session() as db:
        job = (
            await db.execute(select(ScheduledJob).where(ScheduledJob.id == result.scheduled_job_ids[0]))
        ).scalar_one()

    assert job.cron_expr == "45 7 * * *"
    assert job.timezone == "Asia/Tokyo"

    await client.put(
        "/api/v1/auth/me",
        headers=headers,
        json={"timezone": "America/New_York"},
    )

    async with db_module.async_session() as db:
        job = (
            await db.execute(select(ScheduledJob).where(ScheduledJob.id == result.scheduled_job_ids[0]))
        ).scalar_one()

    assert job.cron_expr == "45 7 * * *"
    assert job.timezone == "America/New_York"


@pytest.mark.asyncio
async def test_briefing_dispatch_passes_scheduled_job_timezone(client, monkeypatch):
    import packages.core.database as db_module
    import packages.core.tasks.ai_tasks as ai_tasks
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.tasks.scheduler_tasks import _dispatch_job
    from sqlalchemy import select

    headers, entity_id = await _auth(client, "daily_briefing_dispatch_tz")
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Dispatch Timezone Room"},
    )
    workspace_id = ws_resp.json()["id"]

    captured: dict = {}

    def fake_apply_async(*, args=None, kwargs=None):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(ai_tasks.run_morning_briefing, "apply_async", fake_apply_async)

    job_id = generate_ulid()
    async with db_module.async_session() as db:
        db.add(
            ScheduledJob(
                id=job_id,
                job_id="br:dispatch-timezone",
                entity_id=entity_id,
                workspace_id=workspace_id,
                name="Morning briefing",
                job_type="cron",
                schedule_kind="cron",
                cron_expr="30 7 * * *",
                timezone="Asia/Tokyo",
                execution_type="briefing",
                execution_target={"workspace_id": workspace_id},
                enabled=True,
            )
        )
        await db.commit()

    async with db_module.async_session() as db:
        job = (await db.execute(select(ScheduledJob).where(ScheduledJob.id == job_id))).scalar_one()
        await _dispatch_job(db, job, datetime(2026, 4, 1, 22, 30, tzinfo=timezone.utc))

    assert captured["args"] == [workspace_id]
    assert captured["kwargs"]["timezone_name"] == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_briefing_fallback_uses_workspace_summary_without_inbox():
    from packages.core.briefing.prompt import generate_briefing_via_llm

    workspace_summary = {
        "workspace": {"id": "ws_summary", "name": "Summary"},
        "window": {"date": "2026-03-02"},
        "recommended_action_items": [
            "Respond to 2 task(s) waiting on input.",
            "Review 1 proposed task(s).",
        ],
    }

    briefing = await generate_briefing_via_llm(
        workspace_name="Summary",
        briefing_id="bf_test",
        signals=[],
        memory_snippets=[],
        goals_snapshot=[],
        workspace_summary=workspace_summary,
    )

    assert briefing.briefing_id == "bf_test"
    assert briefing.items
    assert briefing.items[0].source == "manual"
    assert "waiting on input" in briefing.items[0].summary
    assert briefing.metrics_snapshot["workspace_daily_summary"] == workspace_summary
