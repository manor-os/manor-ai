"""Regression coverage for workspace_operation goal measurement-source patches."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_workspace_operation_goal_measurement_sources_update_materializes_goal_rows(
    client: AsyncClient,
    db_session,
) -> None:
    from sqlalchemy import select

    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_op_goal_measurement_sources")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Measurement Sources"},
    )
    assert create.status_code == 201
    ws_id = create.json()["id"]

    first = await client.put(
        f"/api/v1/workspaces/{ws_id}/goals",
        headers=headers,
        json={
            "goals": [
                {
                    "goal_key": "follower_growth",
                    "title": "Reach 10,000 Followers",
                    "metric_key": "follower_count",
                    "target_value": 10000,
                    "cadence": "weekly",
                },
                {
                    "goal_key": "engagement_rate",
                    "title": "Maintain Strong Engagement Rate",
                    "metric_key": "engagement_rate",
                    "target_value": 3,
                    "cadence": "weekly",
                },
            ],
        },
    )
    assert first.status_code == 200

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "source_event_id": "test_goal_measurement_sources",
            "patches": [
                {
                    "op": "goal.measurement_sources.update",
                    "payload": {
                        "sources": {
                            "follower_growth": {
                                "metric_key": "follower_count",
                                "source_type": "twitter_x_read_only",
                                "action": "get_me",
                                "cadence": "weekly",
                                "baseline_value": 39,
                                "current_value": 39,
                            },
                            "engagement_rate": {
                                "metric_key": "engagement_rate",
                                "source_type": "manual",
                                "cadence": "weekly",
                            },
                        },
                    },
                },
                {
                    "op": "evaluation.update",
                    "payload": {
                        "scorecard": {
                            "metrics": [
                                {"metric_key": "follower_count", "weight": 0.4},
                                {"metric_key": "engagement_rate", "weight": 0.25},
                            ],
                        },
                    },
                },
            ],
        },
    )
    assert draft.status_code == 200
    draft_body = draft.json()
    assert set(draft_body["diff"]["changed_keys"]) >= {"goals", "evaluation"}
    assert draft_body["validation"]["valid"] is True

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft_body['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied.status_code == 200

    goals = list((await db_session.execute(select(Goal).where(Goal.workspace_id == ws_id))).scalars().all())
    by_metric = {goal.metric_key: goal for goal in goals}

    follower = by_metric["follower_count"]
    assert follower.measurement_source == {
        "provider": "twitter_x",
        "action": "get_me",
        "params": {"read_only": True},
    }
    assert float(follower.baseline_value) == 39
    assert float(follower.current_value) == 39
    assert follower.measurement_cadence == "weekly"

    engagement = by_metric["engagement_rate"]
    assert engagement.measurement_source == {
        "provider": "manual",
        "params": {
            "mode": "manual_entry",
            "preserve_workspace_manual": True,
        },
    }
    assert engagement.measurement_cadence == "weekly"

    follower_job = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{follower.id}"))
    ).scalar_one_or_none()
    assert follower_job is not None
    assert follower_job.execution_type == "goal_measurement"

    engagement_job = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{engagement.id}"))
    ).scalar_one_or_none()
    assert engagement_job is None

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert workspace.operating_model["evaluation"]["scorecard"]["metrics"][0]["metric_key"] == "follower_count"
