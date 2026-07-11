"""Regression tests for workspace creation/runtime contracts."""

import asyncio
import json
from unittest.mock import ANY, AsyncMock

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, username: str) -> dict:
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


async def _register_from_staff_invite(
    client: AsyncClient,
    *,
    token: str,
    email: str,
    username: str,
    password: str = "pass123",
):
    return await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": email,
            "password": password,
            "invite_token": token,
        },
    )


@pytest.mark.asyncio
async def test_workspace_create_persists_extended_fields(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "ws_ext")

    resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Extended Workspace",
            "longitude": -122.4194,
            "latitude": 37.7749,
            "attribute_tags": ["premium", "west"],
            "identity_label": "SF Office",
            "heartbeat_enabled": True,
            "heartbeat_cadence": "daily",
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["longitude"] == pytest.approx(-122.4194)
    assert body["latitude"] == pytest.approx(37.7749)
    assert body["attribute_tags"] == ["premium", "west"]
    assert body["identity_label"] == "SF Office"
    assert body["heartbeat_enabled"] is True
    assert body["heartbeat_cadence"] == "daily"
    jobs = (
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == body["id"])))
        .scalars()
        .all()
    )
    assert {f"sr:{body['id']}", f"oe:{body['id']}", f"cie:{body['id']}"} <= set(jobs)


@pytest.mark.asyncio
async def test_workspace_metadata_and_activity_resolve_actor(client: AsyncClient):
    headers = await _register(client, "ws_actor_meta")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Actor Metadata", "description": "Initial"},
    )
    assert created.status_code == 201
    workspace = created.json()
    assert workspace["created_by_user_id"] == me["id"]
    assert workspace["created_by_email"] == me["email"]
    assert workspace["created_by_name"]

    listed = (await client.get("/api/v1/workspaces", headers=headers)).json()
    listed_workspace = next(item for item in listed if item["id"] == workspace["id"])
    assert listed_workspace["created_by_user_id"] == me["id"]

    updated = await client.put(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
        json={"description": "Changed", "category": "ops"},
    )
    assert updated.status_code == 200
    assert updated.json()["created_by_user_id"] == me["id"]

    activity = (
        await client.get(
            f"/api/v1/workspaces/{workspace['id']}/activity",
            headers=headers,
            params={"limit": 10},
        )
    ).json()
    updated_event = next(item for item in activity if item["event_type"] == "workspace.updated")
    assert updated_event["user_id"] == me["id"]
    assert updated_event["user_email"] == me["email"]
    assert updated_event["actor_name"]
    assert set(updated_event["details"]["fields"]) == {"category", "description"}


@pytest.mark.asyncio
async def test_workspace_chat_messages_resolve_author_user(client: AsyncClient, monkeypatch):
    from apps.api.routers import workspace_chat

    monkeypatch.setattr(
        workspace_chat,
        "_schedule_workspace_chat_processing",
        lambda **_kwargs: None,
    )

    headers = await _register(client, "ws_group_chat_author")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (
        await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Group Chat Authors"},
        )
    ).json()

    posted = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
        json={"body": "Everyone should see who wrote this."},
    )
    assert posted.status_code == 201
    posted_message = posted.json()
    assert posted_message["author_user_id"] == me["id"]
    assert posted_message["author_user_email"] == me["email"]
    assert posted_message["author_user_name"]

    messages = (
        await client.get(
            f"/api/v1/workspaces/{workspace['id']}/chat/messages",
            headers=headers,
        )
    ).json()
    persisted = next(item for item in messages if item["id"] == posted_message["id"])
    assert persisted["author_user_id"] == me["id"]
    assert persisted["author_user_email"] == me["email"]
    assert persisted["author_user_name"]


@pytest.mark.asyncio
async def test_legacy_workspace_setup_endpoints_are_gone(client: AsyncClient):
    headers = await _register(client, "ws_setup_gone")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "No Legacy Setup"})
    ws_id = create.json()["id"]

    turn = await client.post(
        f"/api/v1/workspaces/{ws_id}/setup/turn",
        headers=headers,
        json={"message": "begin"},
    )
    finalize = await client.post(
        f"/api/v1/workspaces/{ws_id}/setup/finalize",
        headers=headers,
        json={"session_id": "legacy"},
    )

    assert turn.status_code == 410
    assert finalize.status_code == 410


@pytest.mark.asyncio
async def test_workspace_trash_routes_are_not_shadowed_by_workspace_id(client: AsyncClient):
    headers = await _register(client, "ws_trash_routes")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Trash Route"})
    ws_id = create.json()["id"]

    deleted = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert deleted.status_code == 204

    trash = await client.get("/api/v1/workspaces/trash/list", headers=headers)
    grace = await client.get("/api/v1/workspaces/trash/grace-days", headers=headers)

    assert trash.status_code == 200
    assert any(row["id"] == ws_id for row in trash.json())
    assert grace.status_code == 200
    assert grace.json()["grace_days"] > 0


@pytest.mark.asyncio
async def test_workspace_update_invalidates_chat_context_summary(client: AsyncClient, db_session):
    from packages.core.workspace_chat import context as chat_context

    headers = await _register(client, "ws_context_update")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Context Update", "primary_work": "Old operating brief"},
    )
    body = create.json()

    before = await chat_context.get_summary(db_session, body["id"], body["entity_id"])
    assert "Old operating brief" in before

    updated = await client.put(
        f"/api/v1/workspaces/{body['id']}",
        headers=headers,
        json={"primary_work": "New operating brief"},
    )

    assert updated.status_code == 200
    after = await chat_context.get_summary(db_session, body["id"], body["entity_id"])
    assert "New operating brief" in after
    assert "Old operating brief" not in after


@pytest.mark.asyncio
async def test_workspace_context_summary_lists_service_keys_for_delegation(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.workspace_chat import context as chat_context

    headers = await _register(client, "ws_context_service_keys")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Service Key Context"})
    body = create.json()
    agent_id = generate_ulid()
    sub_id = generate_ulid()
    db_session.add(
        Agent(
            id=agent_id,
            entity_id=body["entity_id"],
            name="Social Publisher",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=sub_id,
            entity_id=body["entity_id"],
            agent_id=agent_id,
            workspace_id=body["id"],
            service_key="social_publisher",
            status="active",
        )
    )
    await db_session.commit()
    chat_context.invalidate(body["id"])

    summary = await chat_context.get_summary(db_session, body["id"], body["entity_id"])

    assert "Agents/services (1):" in summary
    assert 'service_key=social_publisher agent="Social Publisher"' in summary
    assert f"subscription_id={sub_id}" in summary


@pytest.mark.asyncio
async def test_workspace_knowledge_group_changes_refresh_context_and_policy(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import Workspace
    from packages.core.workspace_chat import context as chat_context

    headers = await _register(client, "ws_context_knowledge_mutation")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Knowledge Context"})
    body = create.json()
    workspace_id = body["id"]
    entity_id = body["entity_id"]

    before = await chat_context.get_summary(db_session, workspace_id, entity_id)
    assert "Workspace Knowledge Nets" not in before

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups",
        headers=headers,
        json={"name": "Client KB", "purpose": "Old client policy"},
    )

    assert created.status_code == 201
    group_id = created.json()["id"]
    after_create = await chat_context.get_summary(db_session, workspace_id, entity_id)
    assert "Workspace Knowledge Nets: 1" in after_create

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    workspace.operating_model = {"knowledge": {"group_purposes": {group_id: "Stale purpose from operation model"}}}
    await db_session.commit()

    updated = await client.put(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}",
        headers=headers,
        json={"purpose": "Fresh client policy"},
    )

    assert updated.status_code == 200
    db_session.expire_all()
    search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        query="client",
        category="knowledge",
    )
    assert "Fresh client policy" in search
    assert "Stale purpose from operation model" not in search


@pytest.mark.asyncio
async def test_workspace_chat_context_surfaces_builtin_channels(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.workspace_chat import context as chat_context

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Channel Context",
            status="active",
            operating_model={
                "channel_config": {
                    "primary_external_channel": {
                        "channel_type": "webchat",
                        "linked_service_key": "lead_intake",
                        "purpose": "Inbound leasing inquiries.",
                    },
                    "internal_channel": {
                        "channel_type": "internal_chat",
                        "linked_service_key": "pipeline_tracking",
                        "purpose": "Internal approvals and status review.",
                    },
                }
            },
        )
    )
    await db_session.commit()

    chat_context.invalidate(workspace_id)
    summary = await chat_context.get_summary(db_session, workspace_id, entity_id)

    assert "Configured channels:" in summary
    assert "primary_external: webchat" in summary
    assert "internal: internal_chat" in summary
    assert "do not say the workspace has no inbound channels" in summary
    assert "email/SMS/lead forms" in summary


@pytest.mark.asyncio
async def test_workspace_agent_created_task_links_to_matching_goal(client: AsyncClient):
    from packages.core.ai.tools.workspace_agent_tools import _workspace_create_task_handler

    headers = await _register(client, "ws_agent_goal_link")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Goal Link Workspace"})
    workspace = create.json()
    workspace_id = workspace["id"]
    entity_id = workspace["entity_id"]

    goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Prepare 3 reviewed X drafts per week",
            "description": "Reviewed social copy ready for human approval.",
            "metric_key": "x_drafts_ready",
            "target_value": 3,
            "measurement_cadence": "weekly",
        },
    )
    assert goal.status_code == 201
    goal_id = goal.json()["id"]

    raw = await _workspace_create_task_handler(
        entity_id=entity_id,
        user_id="test-user",
        workspace_id=workspace_id,
        conversation_id="test-conversation",
        title="准备 3 条 X 草稿用于人工审核",
        description="Generate three X post drafts only; do not publish.",
        runtime_instructions="只生成草稿，不发布。",
    )
    payload = json.loads(raw)
    assert payload["created"] is True
    assert payload["goal_links"]["source"] == "inferred"
    assert payload["goal_links"]["goal_ids"] == [goal_id]

    goals = await client.get(f"/api/v1/goals?workspace_id={workspace_id}", headers=headers)
    assert goals.status_code == 200
    linked = goals.json()[0]
    assert payload["task"]["id"] in linked["linked_task_ids"]
    assert linked["task_status_counts"]["pending"] == 1


@pytest.mark.asyncio
async def test_workspace_agent_records_goal_measurement_value(client: AsyncClient):
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler

    headers = await _register(client, "ws_agent_goal_measurement")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Measurement Workspace"},
    )
    workspace = create.json()
    workspace_id = workspace["id"]
    entity_id = workspace["entity_id"]

    goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Reach 10,000 Followers",
            "description": "Grow the founder account.",
            "metric_key": "follower_count",
            "target_value": 10000,
            "measurement_cadence": "weekly",
        },
    )
    assert goal.status_code == 201
    goal_id = goal.json()["id"]

    status = json.loads(
        await _workspace_agent_handler(
            entity_id=entity_id,
            user_id="test-user",
            workspace_id=workspace_id,
            conversation_id="test-conversation",
            action="get_goal_status",
            params={},
        )
    )
    assert [row["id"] for row in status["goals"]] == [goal_id]

    updated = json.loads(
        await _workspace_agent_handler(
            entity_id=entity_id,
            user_id="test-user",
            workspace_id=workspace_id,
            conversation_id="test-conversation",
            action="update_goal_value",
            params={
                "goal_id": goal_id,
                "value": 39,
                "note": "Verified follower count baseline.",
            },
        )
    )
    assert updated["goal_id"] == goal_id
    assert updated["workspace_id"] == workspace_id
    assert updated["value"] == 39.0

    goals = await client.get(f"/api/v1/goals?workspace_id={workspace_id}", headers=headers)
    assert goals.status_code == 200
    measured = goals.json()[0]
    assert measured["current_value"] == 39.0
    assert measured["baseline_value"] == 0.0


@pytest.mark.asyncio
async def test_workspace_agent_request_strategist_review_enqueues_and_records_activity(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_request_strategist_review_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import WorkspaceActivity
    from packages.core.tasks import ai_tasks

    headers = await _register(client, "ws_agent_request_strategist")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Manual Strategist Request"})
    workspace = create.json()
    calls: list[dict] = []

    class _AsyncResult:
        id = "celery-manual-strategist"

    class _FakeStrategistTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return _AsyncResult()

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeStrategistTask)

    user_id = generate_ulid()
    payload = json.loads(
        await _workspace_request_strategist_review_handler(
            entity_id=workspace["entity_id"],
            user_id=user_id,
            workspace_id=workspace["id"],
            reason="User asked for a new plan after updated leasing priorities.",
            countdown_seconds=0,
        )
    )

    assert payload["requested"] is True
    assert payload["workspace_id"] == workspace["id"]
    assert payload["trigger"].startswith("user_request:")
    assert payload["countdown_seconds"] == 0
    assert payload["celery_task_id"] == "celery-manual-strategist"
    assert calls == [
        {
            "args": (),
            "kwargs": {
                "args": [workspace["id"], payload["trigger"]],
                "countdown": 0,
            },
        }
    ]

    activity = (
        await db_session.execute(
            select(WorkspaceActivity).where(
                WorkspaceActivity.workspace_id == workspace["id"],
                WorkspaceActivity.event_type == "workspace_agent.strategist_requested",
            )
        )
    ).scalar_one()
    assert activity.user_id == user_id
    assert activity.details["trigger"] == payload["trigger"]
    assert activity.details["celery_task_id"] == "celery-manual-strategist"


@pytest.mark.asyncio
async def test_workspace_chat_message_schedules_workspace_agent_processing(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import workspace_chat

    headers = await _register(client, "ws_chat_agent_schedule")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Workspace Chat Agent"})
    workspace = create.json()
    calls: list[dict] = []

    def _fake_schedule(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(workspace_chat, "_schedule_workspace_chat_processing", _fake_schedule)

    posted = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
        json={"body": "Please review the latest goal status and propose the next safe task wave."},
    )

    assert posted.status_code == 201
    message = posted.json()
    assert calls == [
        {
            "conversation_id": message["conversation_id"],
            "workspace_id": workspace["id"],
            "entity_id": workspace["entity_id"],
            "user_id": ANY,
            "message": "Please review the latest goal status and propose the next safe task wave.",
            "message_id": message["id"],
        }
    ]


@pytest.mark.asyncio
async def test_workspace_operation_draft_budget_requires_apply(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_op_budget")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Budget Runtime"})
    ws_id = create.json()["id"]

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "source_event_id": "test_budget_patch",
            "patches": [
                {
                    "op": "budget_policy.update",
                    "payload": {"monthly_budget_credits": 1000, "auto_pause_on_budget": False},
                }
            ],
        },
    )

    assert draft.status_code == 200
    draft_id = draft.json()["id"]
    before_apply = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert before_apply.monthly_budget_usd is None

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft_id}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    await db_session.refresh(before_apply)
    assert before_apply.monthly_budget_usd is not None
    assert before_apply.auto_pause_on_budget is False
    assert before_apply.operation_revision == 1


@pytest.mark.asyncio
async def test_workspace_operation_rule_apply_syncs_governance(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_op_rule")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Rule Runtime"})
    ws_id = create.json()["id"]
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    workspace.budget_alert_state = "critical_100"
    await db_session.commit()

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "rule.add",
                    "payload": {
                        "rule": {
                            "rule_key": "review_social_posts",
                            "description": "发 post 前必须给用户审核，得到同意才能发",
                            "rule_type": "approval_required",
                            "action_patterns": ["social_post.publish"],
                        },
                    },
                }
            ],
        },
    )
    draft_id = draft.json()["id"]

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft_id}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id))
    ).scalar_one()
    assert "social_post.publish" in policy.policy["hitl_required_actions"]
    await db_session.refresh(workspace)
    assert workspace.budget_alert_state == "critical_100"


@pytest.mark.asyncio
async def test_workspace_operation_rules_replace_drops_stale_inferred_governance(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_op_rule_replace")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Rule Replace Runtime"})
    ws_id = create.json()["id"]

    first = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "rule.add",
                    "payload": {
                        "rule": {
                            "rule_key": "draft_only_social",
                            "description": "Never publish social posts; keep drafts only.",
                            "rule_type": "draft_only",
                            "action_patterns": ["social_post.publish"],
                        },
                    },
                }
            ],
        },
    )
    assert first.status_code == 200
    applied_first = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{first.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied_first.status_code == 200
    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id))
    ).scalar_one()
    assert policy.policy["never_allow_actions"] == ["social_post.publish"]

    second = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "rules.replace",
                    "payload": {
                        "rules": [
                            {
                                "rule_key": "review_social",
                                "description": "Review public social posts before publishing.",
                                "rule_type": "approval_required",
                                "action_patterns": ["social_post.publish"],
                            }
                        ],
                    },
                }
            ],
        },
    )
    assert second.status_code == 200
    applied_second = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{second.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied_second.status_code == 200

    db_session.expire_all()
    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id))
    ).scalar_one()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()

    assert policy.policy["never_allow_actions"] == []
    assert policy.policy["hitl_required_actions"] == ["social_post.publish"]
    assert workspace.operating_model["governance"]["never_allow_actions"] == []
    assert workspace.operating_model["governance"]["hitl_required_actions"] == ["social_post.publish"]

    stale_policy = dict(policy.policy or {})
    stale_policy["never_allow_actions"] = ["social_post.publish"]
    policy.policy = stale_policy
    stale_model = dict(workspace.operating_model or {})
    stale_model["governance"] = stale_policy
    workspace.operating_model = stale_model
    await db_session.commit()

    third = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "rules.replace",
                    "payload": {
                        "rules": [
                            {
                                "rule_key": "review_social",
                                "description": "Review public social posts before publishing.",
                                "rule_type": "approval_required",
                                "action_patterns": ["social_post.publish"],
                            }
                        ],
                    },
                }
            ],
        },
    )
    assert third.status_code == 200
    applied_third = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{third.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert applied_third.status_code == 200

    db_session.expire_all()
    repaired_policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id))
    ).scalar_one()
    repaired_workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert repaired_policy.policy["never_allow_actions"] == []
    assert repaired_workspace.operating_model["governance"]["never_allow_actions"] == []


@pytest.mark.asyncio
async def test_workspace_operation_goal_apply_materializes_runtime_goal_rows(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "ws_op_goal_runtime")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Goal Runtime"})
    ws_id = create.json()["id"]

    first = await client.put(
        f"/api/v1/workspaces/{ws_id}/goals",
        headers=headers,
        json={
            "goals": [
                {
                    "goal_key": "lease_conversion",
                    "title": "Increase lease conversions",
                    "metric_key": "leases_signed",
                    "target": "10,000",
                    "baseline_value": "25",
                    "measurement_source": {"provider": "manual", "params": {}},
                    "cadence": "weekly",
                    "priority": 5,
                }
            ],
        },
    )

    assert first.status_code == 200
    goal = (
        await db_session.execute(select(Goal).where(Goal.workspace_id == ws_id, Goal.metric_key == "leases_signed"))
    ).scalar_one()
    assert goal.title == "Increase lease conversions"
    assert float(goal.target_value) == 10000
    assert float(goal.baseline_value) == 25
    assert goal.measurement_source == {
        "provider": "workspace_internal",
        "params": {"mode": "linked_task_impact"},
    }
    assert goal.measurement_cadence == "weekly"
    assert goal.status == "active"
    # Workspace goals default to Manor runtime evidence instead of a manual
    # number entry so Strategist can reason over fresh execution state.
    job = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal.id}"))).scalar_one()
    assert job.execution_type == "goal_measurement"
    assert job.enabled is True
    assert job.schedule_kind == "every"
    assert job.every_seconds == 604800.0

    second = await client.put(
        f"/api/v1/workspaces/{ws_id}/goals",
        headers=headers,
        json={
            "goals": [
                {
                    "goal_key": "lease_conversion",
                    "title": "Increase signed leases",
                    "metric_key": "leases_signed",
                    "target_value": 12000,
                    "measurement_source": {"provider": "manual", "params": {}},
                    "cadence": "daily",
                }
            ],
        },
    )

    assert second.status_code == 200
    rows = list(
        (await db_session.execute(select(Goal).where(Goal.workspace_id == ws_id, Goal.metric_key == "leases_signed")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    await db_session.refresh(goal)
    assert goal.title == "Increase signed leases"
    assert float(goal.target_value) == 12000
    updated_job = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal.id}"))
    ).scalar_one_or_none()
    assert updated_job is not None
    assert updated_job.enabled is True
    assert updated_job.every_seconds == 86400.0

    cleared = await client.put(
        f"/api/v1/workspaces/{ws_id}/goals",
        headers=headers,
        json={"goals": []},
    )

    assert cleared.status_code == 200
    await db_session.refresh(goal)
    assert goal.status == "paused"
    removed_job = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal.id}"))
    ).scalar_one_or_none()
    assert removed_job is None


@pytest.mark.asyncio
async def test_workspace_internal_goal_measurement_uses_linked_task_impact(client: AsyncClient, db_session):
    from decimal import Decimal
    from sqlalchemy import select
    from packages.core.goals.measurement import measure_goal
    from packages.core.goals.scheduling import install_measurement_schedule
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.task import Task

    headers = await _register(client, "ws_internal_goal_measurement")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Internal Goal Measurement"})
    ws_body = create.json()

    goal = Goal(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Prepare reviewed X drafts",
        metric_key="x_drafts_ready",
        target_value=Decimal("3"),
        baseline_value=None,
        measurement_source={
            "provider": "workspace_internal",
            "params": {"mode": "linked_task_impact"},
        },
        measurement_cadence="daily",
        status="active",
        pace_status="unknown",
    )
    completed_task = Task(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Draft three X posts",
        status="completed",
        details={},
    )
    pending_task = Task(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Review another draft",
        status="pending",
        details={},
    )
    db_session.add_all([goal, completed_task, pending_task])
    await db_session.flush()
    db_session.add_all(
        [
            GoalTaskLink(
                goal_id=goal.id,
                task_id=completed_task.id,
                contribution="direct",
                estimated_impact=Decimal("3"),
            ),
            GoalTaskLink(
                goal_id=goal.id,
                task_id=pending_task.id,
                contribution="direct",
                estimated_impact=Decimal("2"),
            ),
        ]
    )
    await db_session.flush()
    await install_measurement_schedule(db_session, goal)

    result = await measure_goal(goal.id, db=db_session)

    assert result["value"] == 3.0
    await db_session.refresh(goal)
    assert float(goal.baseline_value) == 0
    assert float(goal.current_value) == 3
    assert goal.status == "achieved"
    measurement = (
        await db_session.execute(select(GoalMeasurement).where(GoalMeasurement.goal_id == goal.id))
    ).scalar_one()
    assert measurement.source == "workspace_internal"
    evidence = measurement.meta["measurement"]["evidence"]
    assert {item["source"] for item in evidence} == {"estimated_impact_proxy", "not_completed"}
    removed_job = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal.id}"))
    ).scalar_one_or_none()
    assert removed_job is None


@pytest.mark.asyncio
async def test_monthly_goal_measurement_installs_calendar_schedule(client: AsyncClient, db_session):
    from decimal import Decimal
    from sqlalchemy import select
    from packages.core.goals.scheduling import install_measurement_schedule
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "ws_monthly_goal_measurement")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Monthly Goal Measurement"})
    ws_body = create.json()

    goal = Goal(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Keep monthly cost under budget",
        metric_key="monthly_cost_usd",
        target_value=Decimal("50"),
        measurement_source={
            "provider": "workspace_internal",
            "params": {"mode": "linked_task_impact"},
        },
        measurement_cadence="monthly",
        status="active",
        pace_status="unknown",
    )
    db_session.add(goal)
    await db_session.flush()

    await install_measurement_schedule(db_session, goal)

    job = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal.id}"))).scalar_one()
    assert job.schedule_kind == "cron"
    assert job.job_type == "cron"
    assert job.cron_expr == "0 9 1 * *"
    assert job.every_seconds is None


@pytest.mark.asyncio
async def test_get_skill_by_slug_prefers_entity_skill_over_global_duplicate(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import Skill
    from packages.core.services.skill_service import get_skill_by_slug

    entity_id = generate_ulid()
    global_skill = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Global Research",
        slug="competitive_research_topic_scoring",
        system_prompt="Global version",
        tools=[],
        input_schema={},
        status="active",
        is_public=True,
    )
    entity_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Workspace Research",
        slug="competitive_research_topic_scoring",
        system_prompt="Entity version",
        tools=[],
        input_schema={},
        status="active",
        is_public=True,
    )
    db_session.add_all([global_skill, entity_skill])
    await db_session.flush()

    selected = await get_skill_by_slug(
        db_session,
        "competitive_research_topic_scoring",
        entity_id,
    )

    assert selected is not None
    assert selected.id == entity_skill.id


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["twitter_x", "stripe", "linkedin_browser", "analytics"])
async def test_external_goal_measurement_requires_provider_evidence(
    client: AsyncClient,
    db_session,
    provider: str,
):
    from decimal import Decimal
    from sqlalchemy import select
    from packages.core.goals.measurement import MeasurementError, measure_goal
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal, GoalMeasurement

    headers = await _register(client, "ws_external_goal_no_fake")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "External Goal Evidence"},
    )
    ws_body = create.json()

    goal = Goal(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Grow X followers",
        metric_key="followers_count",
        target_value=Decimal("1000"),
        baseline_value=Decimal("100"),
        current_value=Decimal("100"),
        measurement_source={"provider": provider, "params": {}},
        measurement_cadence="daily",
        status="active",
        pace_status="unknown",
    )
    db_session.add(goal)
    await db_session.flush()

    with pytest.raises(MeasurementError):
        await measure_goal(goal.id, db=db_session)

    measurement = (
        await db_session.execute(select(GoalMeasurement).where(GoalMeasurement.goal_id == goal.id))
    ).scalar_one_or_none()
    assert measurement is None
    await db_session.refresh(goal)
    assert float(goal.current_value) == 100


@pytest.mark.asyncio
async def test_goal_measurement_cleans_stale_schedule_for_achieved_goal(client: AsyncClient, db_session):
    from decimal import Decimal
    from sqlalchemy import select
    from packages.core.goals.measurement import measure_goal
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "ws_achieved_goal_measurement_cleanup")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Achieved Goal Cleanup"})
    ws_body = create.json()

    goal = Goal(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Already achieved",
        metric_key="done_metric",
        target_value=Decimal("1"),
        baseline_value=Decimal("0"),
        current_value=Decimal("1"),
        measurement_source={
            "provider": "workspace_internal",
            "params": {"mode": "linked_task_impact"},
        },
        measurement_cadence="daily",
        status="achieved",
        pace_status="achieved",
    )
    db_session.add(goal)
    db_session.add(
        ScheduledJob(
            id=generate_ulid(),
            job_id=f"gm:{goal.id}",
            entity_id=goal.entity_id,
            workspace_id=goal.workspace_id,
            name=f"Measure goal: {goal.title}",
            job_type="interval",
            schedule_kind="every",
            every_seconds=86400,
            execution_type="goal_measurement",
            execution_target={"goal_id": goal.id},
            goal_id=goal.id,
            enabled=True,
        )
    )
    await db_session.flush()

    result = await measure_goal(goal.id, db=db_session)

    assert result == {"goal_id": goal.id, "skipped": True, "reason": "status=achieved"}
    stale_job = (
        await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal.id}"))
    ).scalar_one_or_none()
    assert stale_job is None


@pytest.mark.asyncio
async def test_workspace_operation_agent_mapping_materializes_subscription(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import AgentSubscription, Workspace
    from packages.core.strategist.context import gather_context

    headers = await _register(client, "ws_op_agent_runtime")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Runtime"})
    ws_id = create.json()["id"]
    agent_one = await client.post("/api/v1/agents", headers=headers, json={"name": "Leasing Agent One"})
    agent_two = await client.post("/api/v1/agents", headers=headers, json={"name": "Leasing Agent Two"})

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "service_role.upsert",
                    "payload": {"service": {"key": "leasing_consultant", "title": "Leasing Consultant"}},
                },
                {
                    "op": "agent_mapping.upsert",
                    "payload": {
                        "mapping": {
                            "service_key": "leasing_consultant",
                            "agent_id": agent_one.json()["id"],
                            "custom_prompt": "Handle leasing requests for this workspace.",
                        },
                    },
                },
            ],
        },
    )
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    assert applied.json()["agent_mappings"]["created"] == 1
    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == ws_id,
                AgentSubscription.service_key == "leasing_consultant",
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    ctx = await gather_context(db_session, workspace)
    assert sub.agent_id == agent_one.json()["id"]
    assert sub.custom_prompt == "Handle leasing requests for this workspace."
    assert "leasing_consultant" in ctx.allowed_service_keys

    update = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "agent_mapping.upsert",
                    "payload": {
                        "mapping": {
                            "service_key": "leasing_consultant",
                            "agent_id": agent_two.json()["id"],
                        },
                    },
                }
            ],
        },
    )
    reapplied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{update.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert reapplied.status_code == 200
    assert reapplied.json()["agent_mappings"]["updated"] == 1
    db_session.expire_all()
    rows = list(
        (
            await db_session.execute(
                select(AgentSubscription).where(
                    AgentSubscription.workspace_id == ws_id,
                    AgentSubscription.service_key == "leasing_consultant",
                    AgentSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].agent_id == agent_two.json()["id"]
    assert rows[0].custom_prompt == "Handle leasing requests for this workspace."


@pytest.mark.asyncio
async def test_workspace_agent_mapping_endpoint_uses_operation_runtime(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import AgentSubscription, Workspace

    headers = await _register(client, "ws_agent_endpoint_runtime")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Endpoint Runtime"})
    ws_id = create.json()["id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Endpoint Agent"})

    service = await client.post(
        f"/api/v1/workspaces/{ws_id}/services",
        headers=headers,
        json={
            "key": "leasing_consultant",
            "name": "Leasing Consultant",
            "description": "Handle leasing requests.",
        },
    )
    assert service.status_code == 200

    mapped = await client.post(
        f"/api/v1/workspaces/{ws_id}/agents",
        headers=headers,
        json={
            "service_key": "leasing_consultant",
            "agent_id": agent.json()["id"],
            "custom_prompt": "Handle leasing requests for this endpoint test.",
        },
    )

    assert mapped.status_code == 200
    assert mapped.json()["agent_mappings"]["created"] == 1
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == ws_id,
                AgentSubscription.service_key == "leasing_consultant",
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()
    assert workspace.operation_revision == 2
    assert sub.agent_id == agent.json()["id"]
    assert any(
        mapping.get("service_key") == "leasing_consultant"
        for mapping in workspace.operating_model.get("agent_mappings", [])
    )

    unmapped = await client.delete(
        f"/api/v1/workspaces/{ws_id}/agents/leasing_consultant",
        headers=headers,
    )

    assert unmapped.status_code == 200
    assert unmapped.json()["agent_mappings"]["deactivated"] == 1
    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    active_subs = list(
        (
            await db_session.execute(
                select(AgentSubscription).where(
                    AgentSubscription.workspace_id == ws_id,
                    AgentSubscription.service_key == "leasing_consultant",
                    AgentSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    assert workspace.operation_revision == 3
    assert active_subs == []
    assert all(
        mapping.get("service_key") != "leasing_consultant"
        for mapping in workspace.operating_model.get("agent_mappings", [])
    )


@pytest.mark.asyncio
async def test_strategist_context_treats_webchat_as_configured_channel(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.workspace import Workspace
    from packages.core.strategist.context import gather_context

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Webchat Runtime",
        status="active",
        operating_model={
            "channel_config": {
                "primary_external_channel": {
                    "channel_type": "webchat",
                    "purpose": "Primary website lead intake.",
                    "linked_service_key": "lead_intake",
                },
                "internal_channel": {
                    "channel_type": "internal_chat",
                    "purpose": "Team review and approvals.",
                },
            },
        },
    )
    db_session.add(workspace)
    db_session.add(
        ChannelConfig(
            id=generate_ulid(),
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            channel_type="webchat",
            provider="webchat",
            name="primary_external: webchat",
            status="active",
            config={
                "role": "primary_external",
                "purpose": "Primary website lead intake.",
                "linked_service_key": "lead_intake",
            },
        )
    )
    await db_session.flush()

    ctx = await gather_context(db_session, workspace)

    channel_types = {item["channel_type"] for item in ctx.configured_channels}
    assert {"webchat", "internal_chat"} <= channel_types
    assert "no_channels" not in ctx.missing_setup
    assert "no_integrations" not in ctx.missing_setup


@pytest.mark.asyncio
async def test_strategist_context_scopes_integrations_to_workspace_declared_providers(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount
    from packages.core.models.workspace import Workspace
    from packages.core.strategist.context import gather_context

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "ws_x_scope",
            "email": "ws_x_scope@test.com",
            "password": "pass123",
            "entity_name": "ws_x_scope Corp",
        },
    )
    assert registered.status_code == 200
    body = registered.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "X Growth - Startup Founder"},
    )
    assert created.status_code == 201

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == created.json()["id"]))).scalar_one()
    workspace.settings = {
        "flagged_integrations": [
            {
                "provider": "twitter_x",
                "required": True,
                "linked_service_keys": ["content_creation", "growth_analytics"],
            }
        ],
    }
    workspace.operating_model = {
        "services": [
            {
                "key": "content_creation",
                "name": "Content Creation & Scheduling",
                "description": "Write tweets and threads for the founder's X account.",
            }
        ],
    }
    db_session.add(
        Integration(
            id=generate_ulid(),
            entity_id=body["entity_id"],
            provider="linkedin",
            status="active",
            config={},
            credentials={},
        )
    )
    db_session.add(
        OAuthAccount(
            id=generate_ulid(),
            user_id=body["user_id"],
            provider="twitter_x",
            provider_user_id="x-user",
            access_token="tok",
            profile={},
        )
    )
    await db_session.flush()

    ctx = await gather_context(db_session, workspace)

    assert ctx.configured_integrations == ["twitter_x"]
    assert "linkedin" not in ctx.configured_integrations
    assert "no_integrations" not in ctx.missing_setup


@pytest.mark.asyncio
async def test_strategist_context_marks_missing_declared_provider_when_only_other_platform_connected(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.workspace import Workspace
    from packages.core.strategist.context import gather_context

    headers = await _register(client, "ws_x_scope_missing")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "X Growth - Startup Founder"},
    )
    assert created.status_code == 201
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == created.json()["id"]))).scalar_one()
    workspace.settings = {
        "flagged_integrations": [
            {
                "provider": "twitter_x",
                "required": True,
            }
        ],
    }
    db_session.add(
        Integration(
            id=generate_ulid(),
            entity_id=created.json()["entity_id"],
            provider="linkedin",
            status="active",
            config={},
            credentials={},
        )
    )
    await db_session.flush()

    ctx = await gather_context(db_session, workspace)

    assert ctx.configured_integrations == []
    assert "no_integrations" in ctx.missing_setup


def test_strategist_integration_scope_drops_out_of_scope_social_tasks():
    from types import SimpleNamespace
    from packages.core.strategist.proposal import Deliverable, Proposal, ProposedTask
    from packages.core.strategist.service import _enforce_integration_scope

    proposal = Proposal(
        review_id="rv_test",
        summary="Review",
        tasks=[
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="linkedin_post",
                title="Publish LinkedIn founder story",
                owner_service_key="content_creation",
            ),
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="x_post",
                title="Publish X post",
                description="Draft a tweet for the founder's X account.",
                owner_service_key="content_creation",
                depends_on_task_keys=["linkedin_post"],
            ),
        ],
    )

    _enforce_integration_scope(
        proposal,
        SimpleNamespace(configured_integrations=["twitter_x"]),
    )

    assert [task.task_key for task in proposal.tasks] == ["x_post"]
    assert proposal.tasks[0].depends_on_task_keys == []
    assert "LinkedIn" in (proposal.notes or "")
    assert "X/Twitter" in (proposal.notes or "")


@pytest.mark.asyncio
async def test_workspace_service_remove_deactivates_related_agent_mapping(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import AgentSubscription, Workspace

    headers = await _register(client, "ws_service_remove_agent_runtime")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Service Remove Runtime"})
    ws_id = create.json()["id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Service Remove Agent"})

    await client.post(
        f"/api/v1/workspaces/{ws_id}/services",
        headers=headers,
        json={
            "key": "leasing_consultant",
            "name": "Leasing Consultant",
            "description": "Handle leasing requests.",
        },
    )
    await client.post(
        f"/api/v1/workspaces/{ws_id}/agents",
        headers=headers,
        json={
            "service_key": "leasing_consultant",
            "agent_id": agent.json()["id"],
        },
    )

    removed = await client.delete(
        f"/api/v1/workspaces/{ws_id}/services/leasing_consultant",
        headers=headers,
    )

    assert removed.status_code == 200
    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    active_subs = list(
        (
            await db_session.execute(
                select(AgentSubscription).where(
                    AgentSubscription.workspace_id == ws_id,
                    AgentSubscription.service_key == "leasing_consultant",
                    AgentSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    assert active_subs == []
    assert all(
        (service.get("key") or service.get("service_key")) != "leasing_consultant"
        for service in workspace.operating_model.get("services", [])
    )
    assert all(
        mapping.get("service_key") != "leasing_consultant"
        for mapping in workspace.operating_model.get("agent_mappings", [])
    )


@pytest.mark.asyncio
async def test_manor_tool_workspace_mutations_use_operation_runtime(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.models.user import User
    from packages.core.models.workspace import AgentSubscription, Workspace

    username = "ws_manor_tool_runtime"
    headers = await _register(client, username)
    user = (await db_session.execute(select(User).where(User.email == f"{username}@test.com"))).scalar_one()
    entity_id = user.entity_id
    user_id = user.id
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Manor Tool Runtime"})
    ws_id = create.json()["id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Manor Tool Agent"})

    add_service = json.loads(
        await _dispatch_action(
            "add_workspace_service",
            {
                "workspace_id": ws_id,
                "service": {
                    "key": "leasing_consultant",
                    "name": "Leasing Consultant",
                    "description": "Handle leasing requests.",
                },
            },
            entity_id,
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    assert add_service["workspace_revision"] == 1

    mapped = json.loads(
        await _dispatch_action(
            "map_agent_to_service",
            {
                "workspace_id": ws_id,
                "service_key": "leasing_consultant",
                "agent_id": agent.json()["id"],
            },
            entity_id,
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    assert mapped["agent_mappings"]["created"] == 1

    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    active_sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == ws_id,
                AgentSubscription.service_key == "leasing_consultant",
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()
    assert workspace.operation_revision == 2
    assert active_sub.agent_id == agent.json()["id"]
    assert any(
        mapping.get("service_key") == "leasing_consultant"
        for mapping in workspace.operating_model.get("agent_mappings", [])
    )

    unmapped = json.loads(
        await _dispatch_action(
            "unmap_agent_from_service",
            {"workspace_id": ws_id, "service_key": "leasing_consultant"},
            entity_id,
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    assert unmapped["agent_mappings"]["deactivated"] == 1

    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    active_subs = list(
        (
            await db_session.execute(
                select(AgentSubscription).where(
                    AgentSubscription.workspace_id == ws_id,
                    AgentSubscription.service_key == "leasing_consultant",
                    AgentSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    assert workspace.operation_revision == 3
    assert active_subs == []
    assert all(
        mapping.get("service_key") != "leasing_consultant"
        for mapping in workspace.operating_model.get("agent_mappings", [])
    )


@pytest.mark.asyncio
async def test_workspace_heartbeat_endpoint_advances_operation_revision(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_heartbeat_revision")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Heartbeat Revision"})
    ws_id = create.json()["id"]

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={"source_event_id": "before_heartbeat", "patches": []},
    )
    assert draft.status_code == 200
    assert draft.json()["base_revision"] == 0

    enabled = await client.post(
        f"/api/v1/workspaces/{ws_id}/heartbeat/enable?cadence=weekly",
        headers=headers,
    )
    assert enabled.status_code == 200

    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert workspace.operation_revision == 1
    assert workspace.heartbeat_enabled is True
    assert workspace.heartbeat_cadence == "weekly"

    stale_apply = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert stale_apply.status_code == 409


@pytest.mark.asyncio
async def test_workspace_operation_capability_binding_overlays_runtime_tool_scope(client: AsyncClient, db_session):
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    headers = await _register(client, "ws_op_tool_scope")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Tool Scope Runtime"})
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Tool Scoped Agent"})
    tool_name = "test_leasing_unit_search"
    mcp_tool_name = "mcp__linkedin_browser__send_message"

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "service_role.upsert",
                    "payload": {"service": {"key": "leasing_consultant", "title": "Leasing Consultant"}},
                },
                {
                    "op": "agent_mapping.upsert",
                    "payload": {
                        "mapping": {
                            "service_key": "leasing_consultant",
                            "agent_id": agent.json()["id"],
                        },
                    },
                },
                {
                    "op": "capability_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "service",
                            "owner_service_key": "leasing_consultant",
                            "capability_type": "tool",
                            "tool_name": tool_name,
                        },
                    },
                },
                {
                    "op": "capability_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "service",
                            "owner_service_key": "leasing_consultant",
                            "capability_type": "capability",
                            "capability_id": "workspace.task",
                        },
                    },
                },
                {
                    "op": "capability_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "service",
                            "owner_service_key": "leasing_consultant",
                            "capability_type": "mcp",
                            "integration_key": "linkedin_browser",
                            "allowed_tools": ["send_message"],
                        },
                    },
                },
            ],
        },
    )
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    db_session.expire_all()
    service_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=ws_id,
        agent_id=agent.json()["id"],
        is_master=False,
    )
    master_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=ws_id,
        agent_id=None,
        is_master=True,
    )

    assert tool_name in (service_runtime.bound_tool_names or set())
    assert tool_name in (master_runtime.bound_tool_names or set())
    assert "workspace_create_task" in (service_runtime.bound_tool_names or set())
    assert "workspace_update_task_runtime" in (service_runtime.bound_tool_names or set())
    assert "workspace.task" in service_runtime.capability_ids
    assert "workspace.task" in master_runtime.capability_ids
    assert mcp_tool_name in (service_runtime.mcp_allowed_names or set())
    assert mcp_tool_name in (master_runtime.mcp_allowed_names or set())


@pytest.mark.asyncio
async def test_workspace_operation_skill_binding_overlays_agent_skills(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.services.skill_service import list_skills_for_agent
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    headers = await _register(client, "ws_op_skill_scope")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Skill Scope Runtime"})
    other = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Other Skill Scope"})
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Skill Scoped Agent"})
    skill_id = generate_ulid()
    db_session.add(
        Skill(
            id=skill_id,
            entity_id=entity_id,
            name="Lease Reply",
            slug="lease_reply",
            description="Draft leasing replies.",
            system_prompt="Write concise leasing replies.",
            status="active",
            is_public=False,
        )
    )
    await db_session.commit()

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "service_role.upsert",
                    "payload": {"service": {"key": "leasing_consultant", "title": "Leasing Consultant"}},
                },
                {
                    "op": "agent_mapping.upsert",
                    "payload": {
                        "mapping": {
                            "service_key": "leasing_consultant",
                            "agent_id": agent.json()["id"],
                        },
                    },
                },
                {
                    "op": "skill_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "service",
                            "owner_service_key": "leasing_consultant",
                            "skill_key": "lease_reply",
                        },
                    },
                },
            ],
        },
    )
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    db_session.expire_all()
    skills = await list_skills_for_agent(
        db_session,
        entity_id,
        agent.json()["id"],
        workspace_id=ws_id,
    )
    other_skills = await list_skills_for_agent(
        db_session,
        entity_id,
        agent.json()["id"],
        workspace_id=other.json()["id"],
    )
    runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=ws_id,
        agent_id=agent.json()["id"],
        is_master=False,
    )
    persisted_binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == agent.json()["id"],
                AgentSkillBinding.skill_id == skill_id,
            )
        )
    ).scalar_one_or_none()

    assert "lease_reply" in {skill.slug for skill in skills}
    assert "lease_reply" not in {skill.slug for skill in other_skills}
    assert "invoke_skill" in (runtime.bound_tool_names or set())
    assert persisted_binding is None


@pytest.mark.asyncio
async def test_workspace_operation_skill_binding_is_enforced_at_invoke_time(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import Skill
    from packages.core.services.skill_service import invoke_skill

    headers = await _register(client, "ws_op_skill_gate")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Skill Gate Runtime"})
    other = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Other Skill Gate"})
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Skill Gate Agent"})
    db_session.add(
        Skill(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Lease Gate",
            slug="lease_gate",
            description="A private leasing skill.",
            system_prompt="Draft leasing copy.",
            status="active",
            is_public=False,
        )
    )
    await db_session.commit()

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "service_role.upsert",
                    "payload": {"service": {"key": "leasing_consultant", "title": "Leasing Consultant"}},
                },
                {
                    "op": "agent_mapping.upsert",
                    "payload": {
                        "mapping": {
                            "service_key": "leasing_consultant",
                            "agent_id": agent.json()["id"],
                        },
                    },
                },
                {
                    "op": "skill_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "service",
                            "owner_service_key": "leasing_consultant",
                            "skill_key": "lease_gate",
                        },
                    },
                },
            ],
        },
    )
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    denied = await invoke_skill(
        db_session,
        "lease_gate",
        entity_id,
        "draft a reply",
        agent_id=agent.json()["id"],
        workspace_id=other.json()["id"],
    )

    assert denied["code"] == "skill_not_allowed"
    assert "not available" in denied["error"]


@pytest.mark.asyncio
async def test_workspace_operation_channel_apply_materializes_webchat_binding(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    headers = await _register(client, "ws_op_channel_runtime")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Channel Runtime"})
    ws_id = create.json()["id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Channel Agent"})

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "service_role.upsert",
                    "payload": {"service": {"key": "leasing_consultant", "title": "Leasing Consultant"}},
                },
                {
                    "op": "agent_mapping.upsert",
                    "payload": {
                        "mapping": {
                            "service_key": "leasing_consultant",
                            "agent_id": agent.json()["id"],
                        },
                    },
                },
                {
                    "op": "channel.upsert",
                    "payload": {
                        "channel": {
                            "role": "primary_external",
                            "channel_type": "webchat",
                            "provider": "webchat",
                            "name": "Leasing Webchat",
                            "purpose": "Route leasing inquiries.",
                            "linked_service_key": "leasing_consultant",
                        },
                    },
                },
            ],
        },
    )
    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200
    assert applied.json()["channels"]["created_configs"] == 1
    assert applied.json()["channels"]["created_bindings"] == 1
    db_session.expire_all()
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.workspace_id == ws_id,
                ChannelConfig.channel_type == "webchat",
            )
        )
    ).scalar_one()
    binding = (
        await db_session.execute(
            select(Channel).where(
                Channel.workspace_id == ws_id,
                Channel.config["channel_config_id"].as_string() == config.id,
            )
        )
    ).scalar_one()
    assert config.name == "Leasing Webchat"
    assert config.config["public_token"]
    assert config.config["linked_service_key"] == "leasing_consultant"
    assert binding.agent_subscription_id is not None
    assert binding.agent_id == agent.json()["id"]

    update = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "channel.upsert",
                    "payload": {
                        "channel": {
                            "role": "primary_external",
                            "channel_type": "webchat",
                            "provider": "webchat",
                            "name": "Leasing Webchat Updated",
                            "purpose": "Updated routing purpose.",
                            "linked_service_key": "leasing_consultant",
                        },
                    },
                }
            ],
        },
    )
    reapplied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{update.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert reapplied.status_code == 200
    assert reapplied.json()["channels"]["created_configs"] == 0
    assert reapplied.json()["channels"]["created_bindings"] == 0
    db_session.expire_all()
    configs = list(
        (
            await db_session.execute(
                select(ChannelConfig).where(
                    ChannelConfig.workspace_id == ws_id,
                    ChannelConfig.channel_type == "webchat",
                )
            )
        )
        .scalars()
        .all()
    )
    bindings = list((await db_session.execute(select(Channel).where(Channel.workspace_id == ws_id))).scalars().all())
    assert len(configs) == 1
    assert len(bindings) == 1
    assert configs[0].name == "Leasing Webchat Updated"
    assert configs[0].config["purpose"] == "Updated routing purpose."


@pytest.mark.asyncio
async def test_workspace_operation_repair_materializes_existing_operating_model(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.goal import Goal
    from packages.core.models.workspace import AgentSubscription, Workspace

    headers = await _register(client, "ws_op_repair_runtime")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Repair Runtime"})
    ws_id = create.json()["id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Repair Agent"})
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    workspace.operation_revision = 7
    workspace.operating_model = {
        "services": [{"key": "leasing_consultant", "title": "Leasing Consultant"}],
        "goals": [
            {
                "goal_key": "repair_goal",
                "title": "Repair Goal",
                "metric_key": "repair_metric",
                "target": "42",
            }
        ],
        "agent_mappings": [
            {
                "service_key": "leasing_consultant",
                "agent_id": agent.json()["id"],
            }
        ],
        "channel_config": {
            "channels": [
                {
                    "role": "primary_external",
                    "channel_type": "webchat",
                    "provider": "webchat",
                    "name": "Repair Webchat",
                    "purpose": "Repair existing JSON into runtime rows.",
                    "linked_service_key": "leasing_consultant",
                }
            ],
        },
    }
    await db_session.commit()

    repaired = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/repair",
        headers=headers,
    )

    assert repaired.status_code == 200
    body = repaired.json()
    assert body["workspace_revision"] == 7
    assert body["goals"]["created"] == 1
    assert body["agent_mappings"]["created"] == 1
    assert body["channels"]["created_configs"] == 1
    assert body["channels"]["created_bindings"] == 1
    db_session.expire_all()

    goal = (
        await db_session.execute(select(Goal).where(Goal.workspace_id == ws_id, Goal.metric_key == "repair_metric"))
    ).scalar_one()
    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == ws_id,
                AgentSubscription.service_key == "leasing_consultant",
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()
    config = (await db_session.execute(select(ChannelConfig).where(ChannelConfig.workspace_id == ws_id))).scalar_one()
    binding = (await db_session.execute(select(Channel).where(Channel.workspace_id == ws_id))).scalar_one()

    assert float(goal.target_value) == 42
    assert sub.agent_id == agent.json()["id"]
    assert config.config["linked_service_key"] == "leasing_consultant"
    assert binding.agent_subscription_id == sub.id

    repaired_again = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/repair",
        headers=headers,
    )

    assert repaired_again.status_code == 200
    assert repaired_again.json()["workspace_revision"] == 7
    assert repaired_again.json()["channels"]["created_configs"] == 0
    assert repaired_again.json()["channels"]["created_bindings"] == 0


@pytest.mark.asyncio
async def test_workspace_operation_repair_reuses_legacy_builtin_channel(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription, Workspace

    headers = await _register(client, "ws_op_repair_legacy_channel")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Legacy Channel Repair"})
    ws_id = create.json()["id"]
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Legacy Channel Agent"})

    legacy_config = ChannelConfig(
        entity_id=create.json()["entity_id"],
        workspace_id=ws_id,
        channel_type="webchat",
        provider="webchat",
        name="Legacy Webchat",
        config={"public_token": "legacy-token"},
        credentials={},
        status="active",
    )
    db_session.add(legacy_config)
    await db_session.flush()
    db_session.add(
        Channel(
            entity_id=create.json()["entity_id"],
            workspace_id=ws_id,
            type="webchat",
            name="Legacy Webchat",
            config={"channel_config_id": legacy_config.id, "public_token": "legacy-token"},
            status="active",
        )
    )
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    workspace.operating_model = {
        "services": [{"key": "lead_intake", "title": "Lead Intake"}],
        "agent_mappings": [
            {
                "service_key": "lead_intake",
                "agent_id": agent.json()["id"],
            }
        ],
        "channel_config": {
            "primary_external_channel": {
                "channel_type": "webchat",
                "provider": "webchat",
                "name": "Repaired Webchat",
                "purpose": "Reuse pre-operation webchat instead of duplicating it.",
                "linked_service_key": "lead_intake",
            },
        },
    }
    await db_session.commit()

    repaired = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/repair",
        headers=headers,
    )

    assert repaired.status_code == 200
    assert repaired.json()["channels"]["created_configs"] == 0
    assert repaired.json()["channels"]["created_bindings"] == 0
    assert repaired.json()["channels"]["updated_configs"] == 1
    assert repaired.json()["channels"]["updated_bindings"] == 1
    db_session.expire_all()
    configs = list(
        (await db_session.execute(select(ChannelConfig).where(ChannelConfig.workspace_id == ws_id))).scalars().all()
    )
    bindings = list((await db_session.execute(select(Channel).where(Channel.workspace_id == ws_id))).scalars().all())
    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == ws_id,
                AgentSubscription.service_key == "lead_intake",
            )
        )
    ).scalar_one()
    assert len(configs) == 1
    assert len(bindings) == 1
    assert configs[0].config["role"] == "primary_external"
    assert configs[0].config["linked_service_key"] == "lead_intake"
    assert configs[0].config["public_token"] == "legacy-token"
    assert bindings[0].agent_subscription_id == sub.id


@pytest.mark.asyncio
async def test_workspace_operation_startup_repair_backfills_once(db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_operation_repair import (
        repair_workspace_operation_runtime_backfill,
    )

    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=generate_ulid(),
        name="Startup Repair Runtime",
        status="active",
        heartbeat_enabled=True,
        heartbeat_cadence="daily",
        operating_model={
            "goals": [
                {
                    "goal_key": "startup_repair_goal",
                    "title": "Startup Repair Goal",
                    "metric_key": "startup_repair_metric",
                    "target": "12",
                }
            ],
        },
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()

    report = await repair_workspace_operation_runtime_backfill(db_session, limit=10)

    assert report.repaired >= 1
    assert report.errors == 0
    db_session.expire_all()
    repaired_workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    marker = repaired_workspace.settings["workspace_operation_runtime_repair_v1"]
    assert marker["completed"] is True
    assert marker["result"]["workspace_revision"] == 0

    goal = (
        await db_session.execute(
            select(Goal).where(
                Goal.workspace_id == workspace_id,
                Goal.metric_key == "startup_repair_metric",
            )
        )
    ).scalar_one()
    jobs = set(
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == workspace_id)))
        .scalars()
        .all()
    )

    assert float(goal.target_value) == 12
    assert {f"sr:{workspace_id}", f"oe:{workspace_id}", f"cie:{workspace_id}"} <= jobs

    second_report = await repair_workspace_operation_runtime_backfill(db_session, limit=10)

    assert second_report.repaired == 0
    assert second_report.skipped_marked >= 1


@pytest.mark.asyncio
async def test_workspace_operation_rejects_unowned_capability_binding(client: AsyncClient):
    headers = await _register(client, "ws_op_binding")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Binding Runtime"})
    ws_id = create.json()["id"]

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "capability_binding.upsert",
                    "payload": {"tool_name": "generate_file"},
                }
            ],
        },
    )

    assert draft.status_code == 200
    body = draft.json()
    assert body["validation"]["valid"] is False
    assert any(err["path"].endswith(".owner_scope") for err in body["validation"]["errors"])

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{body['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 400


@pytest.mark.asyncio
async def test_workspace_operation_allows_mcp_binding_without_allowed_tools(client: AsyncClient):
    headers = await _register(client, "ws_op_mcp_binding")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "MCP Binding Runtime"})
    ws_id = create.json()["id"]

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "capability_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "workspace_agent",
                            "capability_type": "mcp",
                            "integration_key": "linkedin_browser",
                        },
                    },
                }
            ],
        },
    )

    assert draft.status_code == 200
    body = draft.json()
    assert body["validation"]["valid"] is True

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{body['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 200


@pytest.mark.asyncio
async def test_workspace_operation_rejects_unknown_runtime_capability(client: AsyncClient):
    headers = await _register(client, "ws_op_unknown_capability")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Capability Runtime"})
    ws_id = create.json()["id"]

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "capability_binding.upsert",
                    "payload": {
                        "binding": {
                            "owner_scope": "workspace_agent",
                            "capability_type": "capability",
                            "capability_id": "workspace.nope",
                        },
                    },
                }
            ],
        },
    )

    assert draft.status_code == 200
    body = draft.json()
    assert body["validation"]["valid"] is False
    assert any(
        err["path"].endswith(".capability_id") and "unknown runtime capability" in err["message"]
        for err in body["validation"]["errors"]
    )


@pytest.mark.asyncio
async def test_workspace_operation_stale_revision_is_rejected(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_op_stale")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Stale Runtime"})
    ws_id = create.json()["id"]

    draft = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts",
        headers=headers,
        json={"patches": [{"op": "heartbeat_policy.update", "payload": {"enabled": True, "cadence": "daily"}}]},
    )
    draft_id = draft.json()["id"]

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    workspace.operation_revision = 3
    await db_session.commit()

    applied = await client.post(
        f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft_id}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )

    assert applied.status_code == 409


@pytest.mark.asyncio
async def test_workspace_budget_endpoint_uses_operation_runtime(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_budget_runtime_endpoint")
    create = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Budget API Runtime"})
    ws_id = create.json()["id"]

    updated = await client.put(
        f"/api/v1/workspaces/{ws_id}/budget",
        headers=headers,
        json={"monthly_budget_credits": 2500, "auto_pause_on_budget": False},
    )

    assert updated.status_code == 200
    assert updated.json()["monthly_budget_credits"] == 2500
    assert updated.json()["auto_pause_on_budget"] is False

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert workspace.operation_revision == 1
    assert workspace.monthly_budget_usd is not None

    updated_usd = await client.put(
        f"/api/v1/workspaces/{ws_id}/budget",
        headers=headers,
        json={"monthly_budget_usd": 3.5},
    )
    assert updated_usd.status_code == 200
    assert updated_usd.json()["monthly_budget_usd"] == 3.5
    assert updated_usd.json()["monthly_budget_credits"] == 3500

    await db_session.refresh(workspace)
    assert float(workspace.monthly_budget_usd) == 3.5
    assert workspace.operation_revision == 2

    cleared = await client.put(
        f"/api/v1/workspaces/{ws_id}/budget",
        headers=headers,
        json={"monthly_budget_usd": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["monthly_budget_usd"] is None
    assert cleared.json()["monthly_budget_credits"] is None

    await db_session.refresh(workspace)
    assert workspace.monthly_budget_usd is None
    assert workspace.operation_revision == 3

    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id))
    ).scalar_one_or_none()
    assert policy is None


@pytest.mark.asyncio
async def test_workspace_work_batch_triggers_strategist_after_all_tasks_terminal(db_session, monkeypatch):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace, WorkspaceWorkBatch
    from packages.core.services.task_service import update_task
    from packages.core.tasks import ai_tasks

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    batch_id = generate_ulid()
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    calls: list[dict] = []

    class _FakeStrategistTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return None

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeStrategistTask)

    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Batch Runtime",
            operating_model={},
        )
    )
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Task A",
                status="pending",
                details={"workspace_work_batch_id": batch_id},
            ),
            Task(
                id=task_b_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Task B",
                status="pending",
                details={"workspace_work_batch_id": batch_id},
            ),
        ]
    )
    db_session.add(
        WorkspaceWorkBatch(
            id=batch_id,
            workspace_id=workspace_id,
            entity_id=entity_id,
            summary="Two-task wave",
            status="active",
            task_ids=[task_a_id, task_b_id],
            details={},
        )
    )
    await db_session.commit()

    await update_task(db_session, task_a_id, entity_id, status="completed")
    batch = (await db_session.execute(select(WorkspaceWorkBatch).where(WorkspaceWorkBatch.id == batch_id))).scalar_one()
    assert batch.status == "active"
    assert batch.details["progress"]["total"] == 2
    assert batch.details["progress"]["terminal"] == 1
    assert batch.details["progress"]["open_task_ids"] == [task_b_id]
    assert batch.details["progress"]["missing_task_ids"] == []
    assert calls == []

    await update_task(db_session, task_b_id, entity_id, status="failed")
    await db_session.commit()

    batch = (await db_session.execute(select(WorkspaceWorkBatch).where(WorkspaceWorkBatch.id == batch_id))).scalar_one()
    assert batch.status == "completed"
    assert batch.completed_at is not None
    assert len(calls) == 1
    assert calls[0]["kwargs"]["args"] == [workspace_id, f"work_batch_completed:{batch_id}"]


@pytest.mark.asyncio
async def test_task_details_update_preserves_workspace_batch_runtime(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select
    from packages.core.models.workspace import WorkspaceWorkBatch
    from packages.core.tasks import ai_tasks

    headers = await _register(client, "ws_task_details_merge")
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Task Details Merge Runtime"},
    )
    assert ws_resp.status_code == 201
    workspace = ws_resp.json()
    workspace_id = workspace["id"]
    entity_id = workspace["entity_id"]

    task_resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Complete runtime metadata task",
            "details": {
                "workspace_work_batch_id": "batch_details_merge",
                "workspace_operation_draft_id": "draft_details_merge",
                "runtime_context": {"instructions": "Preserve runtime metadata."},
            },
        },
    )
    assert task_resp.status_code == 201
    task_id = task_resp.json()["id"]

    calls: list[dict] = []

    class _FakeStrategistTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return None

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeStrategistTask)
    db_session.add(
        WorkspaceWorkBatch(
            id="batch_details_merge",
            workspace_id=workspace_id,
            entity_id=entity_id,
            summary="Details merge wave",
            status="active",
            task_ids=[task_id],
            details={},
        )
    )
    await db_session.commit()

    updated = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={
            "status": "completed",
            "details": {"qa_note": "status update should merge details"},
        },
    )

    assert updated.status_code == 200
    details = updated.json()["details"]
    assert details["qa_note"] == "status update should merge details"
    assert details["workspace_work_batch_id"] == "batch_details_merge"
    assert details["workspace_operation_draft_id"] == "draft_details_merge"
    assert details["runtime_context"]["instructions"] == "Preserve runtime metadata."

    batch = (
        await db_session.execute(select(WorkspaceWorkBatch).where(WorkspaceWorkBatch.id == "batch_details_merge"))
    ).scalar_one()
    assert batch.status == "completed"
    assert len(calls) == 1
    assert calls[0]["kwargs"]["args"] == [
        workspace_id,
        "work_batch_completed:batch_details_merge",
    ]


@pytest.mark.asyncio
async def test_workspace_agent_mapping_rejects_cross_entity_agent(client: AsyncClient):
    headers_a = await _register(client, "ws_agent_a")
    headers_b = await _register(client, "ws_agent_b")
    ws = await client.post("/api/v1/workspaces", headers=headers_a, json={"name": "A Workspace"})
    agent = await client.post("/api/v1/agents", headers=headers_b, json={"name": "B Agent"})

    resp = await client.post(
        f"/api/v1/workspaces/{ws.json()['id']}/agents",
        headers=headers_a,
        json={"service_key": "ops", "agent_id": agent.json()["id"]},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_workspace_unmap_agent_requires_existing_workspace(client: AsyncClient):
    headers = await _register(client, "ws_unmap_missing")

    resp = await client.delete(
        "/api/v1/workspaces/01HZZZZZZZZZZZZZZZZZZZZZZZ/agents/ops",
        headers=headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_workspace_staff_assignment_rejects_cross_entity_staff(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import WorkspaceStaff

    headers_a = await _register(client, "ws_staff_a")
    headers_b = await _register(client, "ws_staff_b")
    ws = await client.post("/api/v1/workspaces", headers=headers_a, json={"name": "A Workspace"})
    ws_id = ws.json()["id"]
    staff_b = await client.post("/api/v1/staff", headers=headers_b, json={"name": "B Staff"})
    staff_b_id = staff_b.json()["id"]

    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/staff",
        headers=headers_a,
        json={"staff_id": staff_b_id, "role": "reviewer"},
    )

    assert resp.status_code == 404

    db_session.add(
        WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=ws_id,
            staff_id=staff_b_id,
            role="legacy_dirty_row",
        )
    )
    await db_session.commit()

    listed = await client.get(f"/api/v1/workspaces/{ws_id}/staff", headers=headers_a)

    assert listed.status_code == 200
    assert all(row.get("staff_id") != staff_b_id for row in listed.json())


@pytest.mark.asyncio
async def test_staff_invite_rejects_cross_entity_workspace_ids(client: AsyncClient):
    headers_a = await _register(client, "ws_invite_a")
    headers_b = await _register(client, "ws_invite_b")
    ws_b = await client.post("/api/v1/workspaces", headers=headers_b, json={"name": "B Workspace"})
    ws_b_id = ws_b.json()["id"]

    resp = await client.post(
        "/api/v1/staff/invite",
        headers=headers_a,
        json={
            "email": "foreign.workspace.invite@test.com",
            "workspace_ids": [ws_b_id],
        },
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_accept_invite_skips_deleted_workspace_assignments(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import WorkspaceStaff

    headers = await _register(client, "ws_invite_deleted")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Soon Deleted"})
    ws_id = ws.json()["id"]
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={
            "email": "deleted.workspace.invite@test.com",
            "workspace_ids": [ws_id],
        },
    )
    invite_body = invite.json()

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    accept = await _register_from_staff_invite(
        client,
        token=invite_body["invite_token"],
        email="deleted.workspace.invite@test.com",
        username="Deleted Workspace Invitee",
    )

    row = (
        await db_session.execute(
            select(WorkspaceStaff).where(
                WorkspaceStaff.workspace_id == ws_id,
                WorkspaceStaff.staff_id == invite_body["staff_id"],
            )
        )
    ).scalar_one_or_none()

    assert delete.status_code == 204
    assert accept.status_code == 200
    assert row is None


@pytest.mark.asyncio
async def test_workspace_staff_invite_links_user_for_task_assignment(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import WorkspaceStaff

    headers = await _register(client, "ws_staff_task_assign")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Staff Task Workspace"})
    ws_id = ws.json()["id"]
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={
            "email": "task.staff.invite@test.com",
            "name": "Task Staff",
            "workspace_ids": [ws_id],
        },
    )
    invite_body = invite.json()
    accept = await _register_from_staff_invite(
        client,
        token=invite_body["invite_token"],
        email="task.staff.invite@test.com",
        username="Task Staff",
    )
    user_id = accept.json()["user_id"]
    staff_id = invite_body["staff_id"]

    membership = (
        await db_session.execute(
            select(WorkspaceStaff).where(
                WorkspaceStaff.workspace_id == ws_id,
                WorkspaceStaff.staff_id == staff_id,
            )
        )
    ).scalar_one_or_none()
    listed = await client.get(f"/api/v1/workspaces/{ws_id}/staff", headers=headers)
    task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Assign to workspace staff",
            "workspace_id": ws_id,
            "assignee_id": user_id,
        },
    )

    assert invite.status_code == 201
    assert accept.status_code == 200
    assert membership is not None
    assert membership.user_id == user_id
    assert listed.status_code == 200
    listed_membership = next(row for row in listed.json() if row["staff_id"] == staff_id)
    assert listed_membership["user_id"] == user_id
    assert task.status_code == 201
    assert task.json()["assignee_id"] == user_id
    assert task.json()["assignee_name"] == "Task Staff"


@pytest.mark.asyncio
async def test_workspace_channels_return_bound_agent(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    headers = await _register(client, "ws_channels")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Channel Workspace"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Channel Agent"})
    ws_body = ws.json()
    agent_body = agent.json()

    channel_config_id = generate_ulid()
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={"public_token": "test-token"},
        )
    )
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            agent_id=agent_body["id"],
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/workspaces/{ws_body['id']}/channels", headers=headers)

    assert resp.status_code == 200
    assert resp.json()[0]["bound_agent"]["id"] == agent_body["id"]
    assert resp.json()[0]["bound_agent"]["name"] == "Channel Agent"


@pytest.mark.asyncio
async def test_workspace_channel_update_edits_routing_and_config(client: AsyncClient, db_session):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription

    headers = await _register(client, "ws_channel_update")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Channel Update Workspace"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Updated Route Agent"})
    ws_body = ws.json()
    agent_body = agent.json()
    channel_config_id = generate_ulid()
    channel_binding_id = generate_ulid()
    subscription_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=subscription_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_body["id"],
            workspace_id=ws_body["id"],
            name="Lead Intake",
            service_key="lead_intake",
            status="active",
        )
    )
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={"public_token": "update-token", "login_required": False},
            status="active",
        )
    )
    db_session.add(
        Channel(
            id=channel_binding_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/workspaces/{ws_body['id']}/channels/{channel_binding_id}",
        headers=headers,
        json={
            "name": "Edited Webchat",
            "purpose": "Updated public entry point.",
            "linked_service_key": "lead_intake",
            "agent_subscription_id": subscription_id,
            "config": {"login_required": True},
        },
    )

    db_session.expire_all()
    binding = (await db_session.execute(select(Channel).where(Channel.id == channel_binding_id))).scalar_one()
    channel_config = (
        await db_session.execute(select(ChannelConfig).where(ChannelConfig.id == channel_config_id))
    ).scalar_one()

    assert resp.status_code == 200
    assert binding.name == "Edited Webchat"
    assert binding.agent_id == agent_body["id"]
    assert binding.agent_subscription_id == subscription_id
    assert binding.config["purpose"] == "Updated public entry point."
    assert binding.config["linked_service_key"] == "lead_intake"
    assert binding.config["login_required"] is True
    assert channel_config.name == "Edited Webchat"
    assert channel_config.config["purpose"] == "Updated public entry point."
    assert channel_config.config["linked_service_key"] == "lead_intake"
    assert channel_config.config["login_required"] is True


@pytest.mark.asyncio
async def test_public_webchat_info_resolves_subscription_agent_name(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription

    headers = await _register(client, "ws_public_chat_info")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Public Chat Workspace"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Base Agent Name"})
    ws_body = ws.json()
    agent_body = agent.json()
    token = "public-chat-token"
    channel_config_id = generate_ulid()
    subscription_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=subscription_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_body["id"],
            workspace_id=ws_body["id"],
            name="Lead Intake Concierge",
            service_key="lead_intake",
            status="active",
        )
    )
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={
                "public_token": token,
                "welcome_message": "Tell us what you need.",
                "purpose": "Website lead intake.",
            },
            status="active",
        )
    )
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            agent_id=agent_body["id"],
            agent_subscription_id=subscription_id,
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/public/chat/{token}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_name"] == "Website Chat"
    assert body["workspace_name"] == "Public Chat Workspace"
    assert body["agent_name"] == "Lead Intake Concierge"
    assert body["welcome_message"] == "Tell us what you need."
    assert body["purpose"] == "Website lead intake."


@pytest.mark.asyncio
async def test_public_webchat_poll_reads_gateway_conversation(client: AsyncClient, db_session, monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription

    headers = await _register(client, "ws_public_chat_poll")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Public Poll Workspace"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Poll Agent"})
    ws_body = ws.json()
    agent_body = agent.json()
    token = "public-poll-token"
    channel_config_id = generate_ulid()
    subscription_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=subscription_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_body["id"],
            workspace_id=ws_body["id"],
            name="Poll Concierge",
            service_key="lead_intake",
            status="active",
        )
    )
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={"public_token": token},
            status="active",
        )
    )
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            agent_id=agent_body["id"],
            agent_subscription_id=subscription_id,
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await db_session.commit()

    from packages.core.services.channel_agent_runtime import ChannelAgentRunResult

    async def fake_run_agent(**kwargs):
        return ChannelAgentRunResult(content="Thanks, I can help with that.")

    monkeypatch.setattr(
        "packages.core.services.channel_gateway.run_channel_agent_turn",
        fake_run_agent,
    )

    session = await client.post(
        f"/api/v1/public/chat/{token}/session",
        json={"visitor_name": "QA Prospect"},
    )
    assert session.status_code == 200
    session_id = session.json()["session_id"]
    send = await client.post(
        f"/api/v1/public/chat/{token}/message",
        json={"session_id": session_id, "text": "I need a one bedroom."},
    )
    assert send.status_code == 200
    send_body = send.json()
    assert send_body["status"] == "ok"
    assert send_body["reply"] == "Thanks, I can help with that."
    assert send_body["sent"]

    poll = await client.get(f"/api/v1/public/chat/{token}/messages?session_id={session_id}")

    assert poll.status_code == 200
    messages = poll.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "I need a one bedroom."
    assert messages[1]["content"] == "Thanks, I can help with that."

    history = await client.get("/api/v1/chat/conversations", headers=headers)

    assert history.status_code == 200
    webchat_conv = next(c for c in history.json() if c["channel"] == "webchat")
    assert webchat_conv["workspace_id"] == ws_body["id"]
    assert webchat_conv["title"] == "webchat: QA Prospect"

    detail = await client.get(
        f"/api/v1/chat/conversations/{webchat_conv['id']}/messages",
        headers=headers,
    )
    assert detail.status_code == 200
    assert [m["role"] for m in detail.json()] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_public_webchat_poll_hides_internal_assistant_messages(client: AsyncClient, db_session, monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.task import Message
    from packages.core.models.workspace import AgentSubscription

    headers = await _register(client, "ws_public_chat_internal_poll")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Public Internal Poll"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Poll Isolation Agent"})
    ws_body = ws.json()
    agent_body = agent.json()
    token = "public-internal-poll-token"
    channel_config_id = generate_ulid()
    subscription_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=subscription_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_body["id"],
            workspace_id=ws_body["id"],
            name="Poll Isolation Concierge",
            service_key="lead_intake",
            status="active",
        )
    )
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={"public_token": token},
            status="active",
        )
    )
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            agent_id=agent_body["id"],
            agent_subscription_id=subscription_id,
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await db_session.commit()

    from packages.core.services.channel_agent_runtime import ChannelAgentRunResult

    async def fake_run_agent(**kwargs):
        return ChannelAgentRunResult(content="Public reply for this visitor.")

    monkeypatch.setattr(
        "packages.core.services.channel_gateway.run_channel_agent_turn",
        fake_run_agent,
    )

    session = await client.post(
        f"/api/v1/public/chat/{token}/session",
        json={"visitor_name": "QA Prospect"},
    )
    assert session.status_code == 200
    session_id = session.json()["session_id"]

    send = await client.post(
        f"/api/v1/public/chat/{token}/message",
        json={"session_id": session_id, "text": "Can you help me?"},
    )
    assert send.status_code == 200
    conversation_id = send.json()["conversation_id"]

    db_session.add(
        Message(
            id=generate_ulid(),
            conversation_id=conversation_id,
            role="assistant",
            content=(
                "Heads up — I noticed a few items that may need your attention:\n\n"
                "- 6 plan(s) waiting for your input\n\n"
                "Let me know if you'd like me to help with any of these."
            ),
            author_kind="agent",
            meta={},
        )
    )
    await db_session.commit()

    poll = await client.get(f"/api/v1/public/chat/{token}/messages?session_id={session_id}")

    assert poll.status_code == 200
    contents = [m["content"] for m in poll.json()["messages"]]
    assert contents == ["Can you help me?", "Public reply for this visitor."]
    assert all("waiting for your input" not in content for content in contents)

    last_public_id = poll.json()["messages"][-1]["id"]
    poll_after = await client.get(
        f"/api/v1/public/chat/{token}/messages?session_id={session_id}&after={last_public_id}"
    )

    assert poll_after.status_code == 200
    assert poll_after.json()["messages"] == []


@pytest.mark.asyncio
async def test_public_webchat_auto_replies_even_when_external_approval_rule_exists(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.governance import WorkspacePolicy, update_policy
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription

    headers = await _register(client, "ws_public_chat_approval")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Public Approval Workspace"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Approval Agent"})
    ws_body = ws.json()
    agent_body = agent.json()
    token = "public-approval-token"
    channel_config_id = generate_ulid()
    subscription_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=subscription_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_body["id"],
            workspace_id=ws_body["id"],
            name="Approval Concierge",
            service_key="lead_intake",
            status="active",
        )
    )
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={"public_token": token},
            status="active",
        )
    )
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            agent_id=agent_body["id"],
            agent_subscription_id=subscription_id,
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await update_policy(
        db_session,
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        policy=WorkspacePolicy(hitl_required_actions=["external_message.send"]),
        changed_by="test",
        change_summary="Require review for external replies",
    )
    await db_session.commit()

    from packages.core.services.channel_agent_runtime import ChannelAgentRunResult

    async def fake_run_agent(**kwargs):
        return ChannelAgentRunResult(content="Here is the drafted public reply.")

    monkeypatch.setattr(
        "packages.core.services.channel_gateway.run_channel_agent_turn",
        fake_run_agent,
    )

    session = await client.post(
        f"/api/v1/public/chat/{token}/session",
        json={"visitor_name": "QA Prospect"},
    )
    session_id = session.json()["session_id"]
    send = await client.post(
        f"/api/v1/public/chat/{token}/message",
        json={"session_id": session_id, "text": "Can I tour tomorrow?"},
    )
    assert send.status_code == 200
    send_body = send.json()
    assert send_body["status"] == "ok"
    assert send_body["sent"] is True
    assert send_body["reply"] == "Here is the drafted public reply."

    poll_after = await client.get(f"/api/v1/public/chat/{token}/messages?session_id={session_id}")
    messages = poll_after.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Here is the drafted public reply."


@pytest.mark.asyncio
async def test_public_webchat_stream_uses_session_visitor_name_in_runtime_context(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.sse_events import format_sse

    headers = await _register(client, "ws_public_stream_name")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Public Stream Name"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Stream Name Agent"})
    ws_body = ws.json()
    agent_body = agent.json()
    token = "public-stream-name-token"
    channel_config_id = generate_ulid()
    subscription_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=subscription_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_body["id"],
            workspace_id=ws_body["id"],
            name="Stream Name Concierge",
            service_key="lead_intake",
            status="active",
        )
    )
    db_session.add(
        ChannelConfig(
            id=channel_config_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            channel_type="webchat",
            provider="webchat",
            name="Website Chat",
            config={"public_token": token},
            status="active",
        )
    )
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            type="webchat",
            name="Website Chat",
            agent_id=agent_body["id"],
            agent_subscription_id=subscription_id,
            config={"channel_config_id": channel_config_id},
            status="active",
        )
    )
    await db_session.commit()

    captured = {}

    async def fake_runtime_stream_chat_turn(*_args, **kwargs):
        captured["channel_context"] = kwargs["channel_context"]
        yield format_sse(
            "stream_end",
            {
                "conversation_id": kwargs["channel_context"].conversation_id,
                "usage": {},
                "rounds": 0,
                "tool_calls": [],
            },
        )

    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_stream_chat_turn",
        fake_runtime_stream_chat_turn,
    )

    session = await client.post(
        f"/api/v1/public/chat/{token}/session",
        json={"visitor_name": "QA Visitor"},
    )
    session_id = session.json()["session_id"]
    send = await client.post(
        f"/api/v1/public/chat/{token}/message/stream",
        data={"session_id": session_id, "message": "Please create a ticket."},
    )

    assert send.status_code == 200
    assert captured["channel_context"].display_name == "QA Visitor"
    db_session.expire_all()
    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == session.json()["conversation_id"])
        )
    ).scalar_one()
    assert conv.meta["visitor_name"] == "QA Visitor"
    assert conv.meta["sender_name"] == "QA Visitor"


@pytest.mark.asyncio
async def test_channel_gateway_fallback_does_not_cross_workspace(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.services.channel_bindings import load_channel_binding_for_config

    headers = await _register(client, "ws_gateway_scope")
    ws_a = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Gateway A"})
    ws_b = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Gateway B"})
    ws_a_body = ws_a.json()
    ws_b_body = ws_b.json()
    entity_id = ws_a_body["entity_id"]

    config_b = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_b_body["id"],
        channel_type="webchat",
        provider="webchat",
        name="B Website Chat",
        config={},
    )
    db_session.add(config_b)
    db_session.add(
        Channel(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=ws_a_body["id"],
            type="webchat",
            name="A Website Chat",
            agent_id=generate_ulid(),
            config={},
            status="active",
        )
    )
    await db_session.commit()

    binding = await load_channel_binding_for_config(db_session, config_b)

    assert binding is None


@pytest.mark.asyncio
async def test_workspace_documents_filter_cross_entity_members(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember

    headers = await _register(client, "ws_docs_scope")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Docs Scope"})
    ws_body = ws.json()
    group_id = generate_ulid()
    foreign_doc_id = generate_ulid()
    db_session.add(
        DocumentGroup(
            id=group_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            name="Scoped Group",
        )
    )
    db_session.add(
        Document(
            id=foreign_doc_id,
            entity_id=generate_ulid(),
            name="foreign.xlsx",
            file_type="xlsx",
            vector_status="ready",
        )
    )
    db_session.add(DocumentGroupMember(document_id=foreign_doc_id, group_id=group_id))
    await db_session.commit()

    resp = await client.get(f"/api/v1/workspaces/{ws_body['id']}/documents", headers=headers)

    assert resp.status_code == 200
    assert resp.json()[0]["documents"] == []


@pytest.mark.asyncio
async def test_workspace_documents_include_generated_artifacts(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document

    headers = await _register(client, "ws_docs_artifacts")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Docs Artifacts"})
    ws_body = ws.json()
    doc_id = generate_ulid()
    db_session.add(
        Document(
            id=doc_id,
            entity_id=ws_body["entity_id"],
            name="draft-pack.md",
            fs_path="Workspaces/Docs Artifacts/Artifacts/draft-pack.md",
            file_type="md",
            vector_status="ready",
            source="ai_generated",
            metadata_={
                "origin": {"workspace_id": ws_body["id"], "task_id": generate_ulid()},
                "artifact": {"role": "final"},
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/workspaces/{ws_body['id']}/documents", headers=headers)

    assert resp.status_code == 200
    artifacts = next(g for g in resp.json() if g["kind"] == "workspace_artifacts")
    assert artifacts["is_workspace_file_bucket"] is True
    assert artifacts["document_count"] == 1
    assert artifacts["documents"][0]["id"] == doc_id
    assert artifacts["documents"][0]["name"] == "draft-pack.md"


@pytest.mark.asyncio
async def test_workspace_knowledge_groups_are_user_manageable_without_deleting_documents(
    client: AsyncClient, db_session
):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember

    headers = await _register(client, "ws_knowledge_manage")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Knowledge Manage"})
    ws_body = ws.json()
    entity_id = ws_body["entity_id"]
    workspace_id = ws_body["id"]
    doc_id = generate_ulid()
    bucket_id = generate_ulid()

    db_session.add(
        Document(
            id=doc_id,
            entity_id=entity_id,
            name="customer-faq.md",
            file_type="md",
            mime_type="text/markdown",
            vector_status="ready",
            source="upload",
        )
    )
    db_session.add(
        DocumentGroup(
            id=bucket_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            name="Workspace Files",
            settings={"workspace_file_bucket": True, "user_manageable": False},
        )
    )
    await db_session.commit()

    cannot_delete_bucket = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{bucket_id}",
        headers=headers,
    )
    assert cannot_delete_bucket.status_code == 400

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups",
        headers=headers,
        json={
            "name": "Customer-facing FAQ",
            "kind": "agent_source",
            "purpose": "Answer customer questions.",
        },
    )
    assert created.status_code == 201
    group = created.json()
    group_id = group["id"]
    assert group["kind"] == "agent_source"
    assert group["purpose"] == "Answer customer questions."
    assert group["is_workspace_file_bucket"] is False

    added = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}/members",
        headers=headers,
        json={"document_ids": [doc_id, doc_id]},
    )
    assert added.status_code == 200
    assert added.json() == {"added": 1, "skipped": [], "total": 1}

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/documents", headers=headers)
    assert listed.status_code == 200
    default_group = next(g for g in listed.json() if g["is_default_collection"])
    assert default_group["name"] == "Workspace Knowledge"
    assert default_group["kind"] == "workspace_collection"
    assert default_group["document_count"] == 0
    listed_group = next(g for g in listed.json() if g["id"] == group_id)
    assert listed_group["document_count"] == 1
    assert listed_group["documents"][0]["id"] == doc_id
    assert all(g["id"] != bucket_id for g in listed.json())

    cannot_delete_default = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{default_group['id']}",
        headers=headers,
    )
    assert cannot_delete_default.status_code == 400

    removed = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}/members/{doc_id}",
        headers=headers,
    )
    assert removed.status_code == 204

    member = (
        await db_session.execute(
            select(DocumentGroupMember).where(
                DocumentGroupMember.document_id == doc_id,
                DocumentGroupMember.group_id == group_id,
            )
        )
    ).scalar_one_or_none()
    assert member is None
    assert (await db_session.get(Document, doc_id)) is not None

    deleted_group = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}",
        headers=headers,
    )
    assert deleted_group.status_code == 204
    assert (await db_session.get(DocumentGroup, group_id)) is None
    assert (await db_session.get(Document, doc_id)) is not None
    assert (await db_session.get(DocumentGroup, bucket_id)) is not None


@pytest.mark.asyncio
async def test_workspace_chat_context_filters_cross_entity_dirty_rows(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import DocumentGroup
    from packages.core.models.goal import Goal
    from packages.core.models.task import Task
    from packages.core.workspace_chat import context as chat_context

    headers = await _register(client, "ws_context_scope")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Context Scope"})
    ws_body = ws.json()
    foreign_entity_id = generate_ulid()
    db_session.add(
        Task(
            id=generate_ulid(),
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            title="Foreign task should never be summarized",
            status="pending",
        )
    )
    db_session.add(
        Goal(
            id=generate_ulid(),
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            title="Foreign goal should never be summarized",
            metric_key="foreign_metric",
            target_value=10,
            status="active",
        )
    )
    db_session.add(
        DocumentGroup(
            id=generate_ulid(),
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            name="Foreign knowledge should never be summarized",
        )
    )
    await db_session.commit()

    chat_context.invalidate(ws_body["id"])
    summary = await chat_context.get_summary(db_session, ws_body["id"], ws_body["entity_id"])
    search = await chat_context.workspace_search(
        db_session,
        ws_body["id"],
        ws_body["entity_id"],
        query="foreign",
        category="all",
    )

    assert "Foreign" not in summary
    assert "Foreign" not in search


@pytest.mark.asyncio
async def test_workspace_chat_context_lists_active_tasks_and_running_alias(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.workspace_chat import context as chat_context

    headers = await _register(client, "ws_context_active_task")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Active Task Context"})
    ws_body = ws.json()
    task_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Generate product drawing pack",
            description="Create views and dimensions.",
            status="in_progress",
            owner_service_key="designer",
            delegate_service_keys=["market", "qa"],
            details={
                "runtime_context": {
                    "instructions": "Preserve original drawing requirements.",
                },
            },
        )
    )
    await db_session.commit()

    chat_context.invalidate(ws_body["id"])
    summary = await chat_context.get_summary(db_session, ws_body["id"], ws_body["entity_id"])
    search = await chat_context.workspace_search(
        db_session,
        ws_body["id"],
        ws_body["entity_id"],
        category="tasks",
        status="running",
    )

    assert "Active tasks:" in summary
    assert f"task_id={task_id}" in summary
    assert f"task_id={task_id}" in search
    assert "runtime_context" in search
    assert "owner=designer" in search


@pytest.mark.asyncio
async def test_chat_runtime_context_ignores_workspace_id_on_plain_channel_conversation(
    client: AsyncClient,
    db_session,
):
    from packages.core.ai.runtime.profiles import (
        LEGACY_WORKSPACE_TOOL_PROFILE as TOOL_PROFILE_WORKSPACE_AGENT,
        RuntimeProfile,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services.workspace_runtime import (
        load_conversation_runtime_context,
        resolve_workspace_runtime,
    )

    headers = await _register(client, "ws_chat_scope_guard")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Scope Guard"})
    ws_body = ws.json()
    conv_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="External channel history",
            channel="webchat",
            scope="channel",
        )
    )
    await db_session.commit()

    runtime_context = await load_conversation_runtime_context(
        db_session,
        conversation_id=conv_id,
        entity_id=ws_body["entity_id"],
    )

    assert runtime_context["workspace_id"] is None
    assert runtime_context["thread_ref_kind"] is None
    assert runtime_context["thread_ref_id"] is None

    unscoped_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=ws_body["entity_id"],
        conversation_id=conv_id,
    )
    assert unscoped_runtime.workspace_id is None
    assert unscoped_runtime.runtime_profile is None

    explicit_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=ws_body["entity_id"],
        conversation_id=conv_id,
        workspace_id=ws_body["id"],
    )
    assert explicit_runtime.workspace_id == ws_body["id"]
    assert explicit_runtime.runtime_profile == RuntimeProfile.WORKSPACE_OPERATOR.value
    assert explicit_runtime.legacy_runtime_profile == TOOL_PROFILE_WORKSPACE_AGENT


@pytest.mark.asyncio
async def test_chat_runtime_context_resolves_workspace_task_thread(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Task
    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context
    from packages.core.services.workspace_runtime import load_conversation_runtime_context

    headers = await _register(client, "ws_chat_task_ctx")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Context"})
    ws_body = ws.json()
    task_id = generate_ulid()
    conv_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Prepare lease packet",
            description="Use the applicant's stated move-in date.",
            status="pending",
            details={
                "runtime_context": {
                    "required_refs": ["doc_lease_terms"],
                    "rules": [{"description": "Draft only until manager approval"}],
                },
            },
        )
    )
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Prepare lease packet",
            channel="workspace",
            scope="workspace_thread",
            thread_ref_kind="task",
            thread_ref_id=task_id,
        )
    )
    await db_session.commit()

    runtime = await load_conversation_runtime_context(
        db_session,
        conversation_id=conv_id,
        entity_id=ws_body["entity_id"],
    )

    assert runtime["workspace_id"] == ws_body["id"]
    assert runtime["task_id"] == task_id
    assert runtime["thread_ref_kind"] == "task"
    assert "Prepare lease packet" in runtime["extra_context"]
    assert "Draft only until manager approval" in runtime["extra_context"]

    _prompt, _tools, _history, ctx = await resolve_runtime_chat_context(
        db_session,
        "继续这个 task",
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        conversation_id=conv_id,
    )

    assert ctx.task_id == task_id
    assert ctx.thread_ref_kind == "task"
    assert ctx.extra_context
    assert "Prepare lease packet" in ctx.extra_context


@pytest.mark.asyncio
async def test_workspace_chat_uses_workspace_agent_tool_profile(client: AsyncClient, db_session):
    from packages.core.ai.runtime.profiles import (
        LEGACY_WORKSPACE_TOOL_PROFILE as TOOL_PROFILE_WORKSPACE_AGENT,
        RuntimeProfile,
    )
    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context

    headers = await _register(client, "ws_chat_tool_profile")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Tool Profile"})
    ws_body = ws.json()

    _prompt, tools, _history, ctx = await resolve_runtime_chat_context(
        db_session,
        "帮我看看workspace状态",
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
    )
    eager_names = {tool["function"]["name"] for tool in tools}

    assert ctx.runtime_profile == RuntimeProfile.WORKSPACE_OPERATOR.value
    assert ctx.legacy_runtime_profile == TOOL_PROFILE_WORKSPACE_AGENT
    assert {"search_tools", "workspace_agent", "workspace_resolve_hitl", "workspace_search", "rag"} <= eager_names
    assert "bash" in eager_names
    assert "bash" in ctx.allowed_tool_names
    assert "browse_web" in ctx.allowed_tool_names
    assert any(name.startswith("mcp__") for name in ctx.allowed_tool_names)


@pytest.mark.asyncio
async def test_workspace_chat_master_surface_includes_mcp(client: AsyncClient, db_session):
    from packages.core.ai.runtime.profiles import (
        LEGACY_WORKSPACE_TOOL_PROFILE as TOOL_PROFILE_WORKSPACE_AGENT,
        RuntimeProfile,
    )
    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context

    headers = await _register(client, "ws_chat_active_intent_mcp")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Scoped MCP"})
    ws_body = ws.json()

    _prompt, _tools, _history, ctx = await resolve_runtime_chat_context(
        db_session,
        "帮我发布小红书帖子",
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
    )

    assert ctx.runtime_profile == RuntimeProfile.WORKSPACE_OPERATOR.value
    assert ctx.legacy_runtime_profile == TOOL_PROFILE_WORKSPACE_AGENT
    assert any(name.startswith("mcp__") for name in ctx.allowed_tool_names)


@pytest.mark.asyncio
async def test_workspace_chat_context_includes_task_runtime_context(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Task
    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context

    headers = await _register(client, "ws_chat_task_runtime")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Runtime Chat"})
    ws_body = ws.json()
    task_id = generate_ulid()
    conv_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Create manga scene pack",
            status="pending",
            details={
                "runtime_context": {
                    "rules": [{"description": "Keep generated assets in one folder"}],
                },
            },
        )
    )
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Create manga scene pack",
            channel="workspace",
            scope="workspace_thread",
            thread_ref_kind="task",
            thread_ref_id=task_id,
        )
    )
    await db_session.commit()

    _prompt, _tools, _history, ctx = await resolve_runtime_chat_context(
        db_session,
        "继续",
        entity_id=ws_body["entity_id"],
        conversation_id=conv_id,
    )

    assert "Create manga scene pack" in ctx.extra_context
    assert "Keep generated assets in one folder" in ctx.extra_context


@pytest.mark.asyncio
async def test_workspace_runtime_resolves_explicit_task_without_conversation(client: AsyncClient, db_session):
    from packages.core.ai.runtime.profiles import (
        LEGACY_WORKSPACE_TOOL_PROFILE as TOOL_PROFILE_WORKSPACE_AGENT,
        RuntimeProfile,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    headers = await _register(client, "ws_runtime_explicit_task")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Runtime Explicit"})
    ws_body = ws.json()
    task_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Prepare prospect follow-up",
            description="Use only approved pricing copy.",
            status="pending",
            details={
                "runtime_context": {
                    "rules": [{"description": "Do not send without manager review"}],
                },
            },
        )
    )
    await db_session.commit()

    runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        task_id=task_id,
    )

    assert runtime.runtime_profile == RuntimeProfile.WORKSPACE_OPERATOR.value
    assert runtime.legacy_tool_profile == TOOL_PROFILE_WORKSPACE_AGENT
    assert runtime.legacy_runtime_profile == TOOL_PROFILE_WORKSPACE_AGENT
    assert runtime.task_id == task_id
    assert runtime.thread_ref_kind == "task"
    assert "Prepare prospect follow-up" in runtime.extra_context
    assert "Do not send without manager review" in runtime.extra_context


@pytest.mark.asyncio
async def test_workspace_runtime_includes_task_service_agent_tool_scope(client: AsyncClient, db_session):
    import json

    from packages.core.ai.runtime.profiles import (
        LEGACY_WORKSPACE_TOOL_PROFILE as TOOL_PROFILE_WORKSPACE_AGENT,
        RuntimeProfile,
    )
    from packages.core.ai.tool_pool import tool_pool
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    headers = await _register(client, "ws_runtime_service_tools")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Runtime Service Tools"})
    ws_body = ws.json()
    agent_id = generate_ulid()
    sub_id = generate_ulid()
    tool_id = generate_ulid()
    task_id = generate_ulid()
    tool_name = "test_leasing_unit_search"

    db_session.add(
        Agent(
            id=agent_id,
            entity_id=ws_body["entity_id"],
            name="Leasing Consultant",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=sub_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_id,
            workspace_id=ws_body["id"],
            service_key="leasing_consultant",
            status="active",
        )
    )
    db_session.add(
        ToolDefinition(
            id=tool_id,
            name=tool_name,
            display_name="Test Leasing Unit Search",
            status="active",
        )
    )
    db_session.add(AgentToolBinding(agent_id=agent_id, tool_id=tool_id))
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Prepare matching units",
            status="pending",
            details={},
            owner_service_key="leasing_consultant",
            delegate_service_keys=["leasing_consultant"],
        )
    )
    await db_session.commit()

    if not tool_pool.tool_count:
        tool_pool.initialize()
    previous_tool = tool_pool._tools.get(tool_name)
    tool_pool.register(
        tool_name,
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Find matching test leasing units.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        lambda **_kwargs: "{}",
        deferred=True,
    )

    try:
        runtime = await resolve_workspace_runtime(
            db_session,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            task_id=task_id,
        )
        from packages.core.ai.runtime.tool_registry import runtime_registered_tool_surface_from_schemas

        surface = runtime_registered_tool_surface_from_schemas(
            tool_pool.registered_tool_schemas(),
            bound_tool_names=runtime.bound_tool_names,
            is_master=runtime.is_master,
            mcp_allowed_names=runtime.mcp_allowed_names,
            legacy_tool_profile=runtime.legacy_tool_profile,
        )
        allowed = set(surface.visible_tool_names)
        search_result = json.loads(
            await tool_pool.execute(
                "search_tools",
                {"query": "leasing unit", "max_results": 5},
                legacy_tool_profile=runtime.legacy_tool_profile,
                allowed_tool_names=allowed,
            )
        )
    finally:
        if previous_tool is not None:
            tool_pool._tools[tool_name] = previous_tool
        else:
            tool_pool._tools.pop(tool_name, None)

    assert runtime.runtime_profile == RuntimeProfile.WORKSPACE_OPERATOR.value
    assert runtime.legacy_tool_profile == TOOL_PROFILE_WORKSPACE_AGENT
    assert runtime.is_master is True
    assert runtime.service_agent_ids == [agent_id]
    assert runtime.bound_tool_names is not None
    assert tool_name in runtime.bound_tool_names
    assert tool_name in allowed
    assert tool_name in set(search_result.get("loaded_tools") or [])
    assert "bash" in allowed


@pytest.mark.asyncio
async def test_task_comment_schedules_workspace_agent_processing(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Task
    from apps.api.routers import tasks as tasks_router
    from sqlalchemy import select

    calls: list[dict] = []
    started = asyncio.Event()

    async def fake_process_workspace_task_comment(**kwargs):
        calls.append(kwargs)
        started.set()

    monkeypatch.setattr(
        tasks_router,
        "process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )

    headers = await _register(client, "ws_task_comment_agent")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Comment Agent"})
    ws_body = ws.json()
    task_id = generate_ulid()
    agent_ulid = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Find homes for prospect",
            description="Initial leasing task",
            status="pending",
            details={},
            agent_id=agent_ulid,
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/logs",
        headers=headers,
        json={
            "content": "新增规则：任何房源推荐发给客户前都必须先让我审核。",
            "log_type": "comment",
        },
    )

    assert resp.status_code == 201
    await asyncio.wait_for(started.wait(), timeout=1)
    assert calls
    assert calls[0]["task_id"] == task_id
    assert calls[0]["entity_id"] == ws_body["entity_id"]
    assert calls[0]["comment"].startswith("新增规则")
    assert calls[0]["log_id"] == resp.json()["id"]
    assert calls[0]["responding_agent_id"] == agent_ulid
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.task_id == task_id,
                RuntimeEvidence.evidence_type == "task_comment",
            )
        )
    ).scalar_one()
    assert evidence.workspace_id == ws_body["id"]
    assert evidence.details["task_log_id"] == resp.json()["id"]
    assert evidence.details["comment"].startswith("新增规则")


@pytest.mark.asyncio
async def test_attachment_only_task_comment_does_not_schedule_workspace_agent(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from apps.api.routers import tasks as tasks_router

    calls: list[dict] = []

    async def fake_process_workspace_task_comment(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        tasks_router,
        "process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )

    headers = await _register(client, "ws_task_attachment_only")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Attachment Only"})
    ws_body = ws.json()
    task_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Collect docs",
            status="pending",
            details={},
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/logs",
        headers=headers,
        json={
            "content": "Attached 1 file",
            "log_type": "comment",
            "attachments": [{"filename": "lease.pdf", "url": "/tmp/lease.pdf"}],
        },
    )

    assert resp.status_code == 201
    assert calls == []


@pytest.mark.asyncio
async def test_channel_gateway_workspace_subscription_uses_legacy_tool_profile(monkeypatch):
    from types import SimpleNamespace

    from packages.core.ai.runtime.profiles import (
        LEGACY_WORKSPACE_TOOL_PROFILE as TOOL_PROFILE_WORKSPACE_AGENT,
    )
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.services.agent_subscription_service import ResolvedSubscription
    from packages.core.services.channel_agent_runtime import run_channel_agent_turn
    from packages.core.services import channel_agent_runtime
    from packages.core.services import workspace_runtime

    captured: dict = {}

    class DummySession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def fake_resolve_workspace_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            legacy_tool_profile=TOOL_PROFILE_WORKSPACE_AGENT,
            workspace_id="workspace-runtime-channel",
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=None,
            is_master=True,
            mcp_allowed_names=None,
            extra_context="",
        )

    async def fake_prepare_prompt_appendix_for_turn(_db, **kwargs):
        captured["appendix_kwargs"] = kwargs
        return SimpleNamespace(
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=SimpleNamespace(),
        )

    async def fake_agentic_loop(**kwargs):
        captured["loop_kwargs"] = kwargs
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(channel_agent_runtime, "async_session", lambda: DummySession())
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_resolve_workspace_runtime)
    monkeypatch.setattr(channel_agent_runtime, "resolve_channel_base_prompt", AsyncMock(return_value="base"))
    monkeypatch.setattr(
        channel_agent_runtime,
        "runtime_prepare_prompt_appendix_for_turn",
        fake_prepare_prompt_appendix_for_turn,
    )
    monkeypatch.setattr(channel_agent_runtime, "runtime_execute_channel_agent_loop", fake_agentic_loop)

    result = await run_channel_agent_turn(
        entity_id="entity-runtime-channel",
        agent_id=MANOR_AGENT_ID,
        user_id="user-runtime-channel",
        conversation_id="conv-runtime-channel",
        current_message="请按workspace规则处理",
        history=[],
        subscription=ResolvedSubscription(
            id="sub-runtime-channel",
            agent_id=MANOR_AGENT_ID,
            workspace_id="workspace-runtime-channel",
            custom_prompt=None,
        ),
    )

    assert result and result.content == "ok"
    assert captured["appendix_kwargs"]["legacy_runtime_profile"] == TOOL_PROFILE_WORKSPACE_AGENT
    assert captured["loop_kwargs"]["legacy_tool_profile"] == TOOL_PROFILE_WORKSPACE_AGENT
    assert captured["loop_kwargs"]["allowed_tool_names"] == set()


@pytest.mark.asyncio
async def test_workspace_chat_context_surfaces_knowledge_policy_and_document_matches(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.workspace import Workspace
    from packages.core.workspace_chat import context as chat_context

    headers = await _register(client, "ws_context_knowledge")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Knowledge Runtime"})
    ws_body = ws.json()
    group_id = generate_ulid()
    doc_id = generate_ulid()

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    workspace.operating_model = {
        "knowledge": {
            "auto_search": True,
            "retrieval_mode": "strict",
            "strict_mode": True,
            "citation_required": True,
            "default_group_ids": [group_id],
            "group_purposes": {group_id: "Pricing answers and escalation guidance"},
        }
    }
    db_session.add(
        DocumentGroup(
            id=group_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            name="Support FAQ",
            settings={"purpose": "Pricing answers and escalation guidance"},
        )
    )
    db_session.add(
        Document(
            id=doc_id,
            entity_id=ws_body["entity_id"],
            name="Pricing Escalation.md",
            fs_path="support/pricing.md",
            file_type="md",
            mime_type="text/markdown",
            vector_status="ready",
            source="upload",
        )
    )
    db_session.add(DocumentGroupMember(document_id=doc_id, group_id=group_id))
    await db_session.commit()

    chat_context.invalidate(ws_body["id"])
    summary = await chat_context.get_summary(db_session, ws_body["id"], ws_body["entity_id"])
    search = await chat_context.workspace_search(
        db_session,
        ws_body["id"],
        ws_body["entity_id"],
        query="pricing",
        category="knowledge",
    )

    assert "Knowledge runtime: before answering or executing document-dependent requests" in summary
    assert "Knowledge strict mode" in summary
    assert "Pricing Escalation.md" in search
    assert "Use rag(workspace_id=...)" in search


@pytest.mark.asyncio
async def test_workspace_agent_creates_workspace_task_with_runtime_context(client: AsyncClient, db_session):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.task import Task

    headers = await _register(client, "ws_agent_task")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Task Runtime"})
    ws_body = ws.json()

    result = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEAGENTTASK000",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEAGENTTASK000",
            action="create_task",
            params={
                "title": "Prepare lease consultant options",
                "description": "Find units that match the client's needs.",
                "runtime_instructions": "Use the leasing FAQ before suggesting units.",
                "required_refs": ["doc:leasing-faq"],
                "rules": [
                    {
                        "description": "Do not send external messages for this task without user approval.",
                        "rule_type": "approval_required",
                        "action_patterns": ["external_message.send"],
                    }
                ],
                "knowledge_query": "leasing FAQ and available unit matching guidance",
            },
        )
    )

    assert result["created"] is True
    task = (await db_session.execute(select(Task).where(Task.id == result["task"]["id"]))).scalar_one()
    runtime = task.details["runtime_context"]
    assert task.workspace_id == ws_body["id"]
    assert runtime["instructions"] == "Use the leasing FAQ before suggesting units."
    assert runtime["required_refs"] == ["doc:leasing-faq"]
    assert runtime["knowledge_query"] == "leasing FAQ and available unit matching guidance"
    assert runtime["rules"][0]["action_patterns"] == ["external_message.send"]


@pytest.mark.asyncio
async def test_public_webchat_broad_agent_binding_can_create_customer_ticket(client: AsyncClient, db_session):
    import json

    from sqlalchemy import select

    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChannelRuntimeContext,
        ChatSurface,
        runtime_execute_registered_tool,
        runtime_prepare_agent_tool_surface_for_turn,
    )
    from packages.core.ai.tools.workspace_agent_tools import _workspace_create_task_handler
    from packages.core.models.task import Task

    headers = await _register(client, "ws_public_ticket")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Public Ticket Runtime"})
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Ticket Concierge"})
    ws_body = ws.json()
    agent_body = agent.json()

    request = AIRuntimeRequest(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        entity_id=ws_body["entity_id"],
        agent_id=agent_body["id"],
        workspace_id=ws_body["id"],
        conversation_id="CONVPUBLICTICKET00000001",
        input_preview="Room 206 toilet is broken. Please create a maintenance ticket.",
        channel_context=ChannelRuntimeContext(
            channel_type="webchat",
            source_id="public-session-206",
            display_name="Alex Guest",
            role="external",
            conversation_id="CONVPUBLICTICKET00000001",
            channel_contact_id="CONTACTPUBLICTICKET0001",
        ),
    )
    surface = runtime_prepare_agent_tool_surface_for_turn(
        request,
        agent_id=agent_body["id"],
        bound_tool_names={"rag", "workspace_agent", "workspace_operation", "manor"},
    )

    assert surface.allowed_tool_names == {"rag", "workspace_create_task"}
    assert "workspace_agent" not in surface.allowed_tool_names

    raw = await runtime_execute_registered_tool(
        tool_name="workspace_create_task",
        arguments={
            "title": "Repair Room 206 toilet",
            "description": "Guest Alex reports the toilet in room 206 is broken.",
            "priority": 2,
        },
        handler_resolver=lambda name: _workspace_create_task_handler if name == "workspace_create_task" else None,
        entity_id=ws_body["entity_id"],
        user_id=None,
        agent_id=agent_body["id"],
        workspace_id=ws_body["id"],
        conversation_id="CONVPUBLICTICKET00000001",
        allowed_tool_names=surface.allowed_tool_names,
        runtime_envelope=surface.envelope,
    )
    result = json.loads(raw)

    assert result["created"] is True
    task = (await db_session.execute(select(Task).where(Task.id == result["task"]["id"]))).scalar_one()
    assert task.workspace_id == ws_body["id"]
    assert task.conversation_id == "CONVPUBLICTICKET00000001"
    assert task.creator_id == agent_body["id"]
    customer_context = task.details["customer_context"]
    assert customer_context == {
        "source": "public_customer_chat",
        "channel_type": "webchat",
        "source_id": "public-session-206",
        "channel_contact_id": "CONTACTPUBLICTICKET0001",
        "conversation_id": "CONVPUBLICTICKET00000001",
        "display_name": "Alex Guest",
        "role": "external",
        "is_verified": False,
    }


@pytest.mark.asyncio
async def test_workspace_agent_updates_existing_task_runtime_context(client: AsyncClient, db_session):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.task import Task, TaskLog

    headers = await _register(client, "ws_agent_task_runtime_update")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Runtime Update"})
    ws_body = ws.json()
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Prepare customer housing options",
            "workspace_id": ws_body["id"],
            "details": {
                "runtime_context": {
                    "instructions": "Use the existing leasing preferences.",
                    "required_refs": ["doc:lease-intake"],
                    "rules": [
                        {
                            "rule_key": "approval_before_send",
                            "description": "Ask before sending listings to the customer.",
                            "rule_type": "approval_required",
                            "action_patterns": ["external_message.send"],
                        }
                    ],
                    "knowledge_query": "leasing preferences",
                },
            },
        },
    )
    assert create.status_code == 201
    task_id = create.json()["id"]

    result = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USER_RUNTIME_UPDATE",
            workspace_id=ws_body["id"],
            conversation_id="CONV_RUNTIME_UPDATE",
            action="update_task_runtime",
            params={
                "task_id": task_id,
                "runtime_instructions": "Also prioritize listings under $2,500 and near transit.",
                "required_refs": ["doc:lease-intake", "doc:transit-map"],
                "knowledge_query": "budget and transit constraints",
                "rules": [
                    {
                        "description": "Do not promise availability without checking the PMS.",
                        "rule_type": "deny",
                        "action_patterns": ["external_message.send"],
                    }
                ],
            },
        )
    )

    assert result["updated"] is True
    db_session.expire_all()
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    runtime = task.details["runtime_context"]
    assert runtime["instructions"] == (
        "Use the existing leasing preferences.\nAlso prioritize listings under $2,500 and near transit."
    )
    assert runtime["required_refs"] == ["doc:lease-intake", "doc:transit-map"]
    assert runtime["knowledge_query"] == "leasing preferences\nbudget and transit constraints"
    assert [rule["rule_key"] for rule in runtime["rules"]][0] == "approval_before_send"
    assert runtime["rules"][1]["description"] == "Do not promise availability without checking the PMS."
    assert runtime["rules"][1]["rule_type"] == "deny"
    assert runtime["rules"][1]["action_patterns"] == ["external_message.send"]
    assert runtime["captured_from"] == {
        "conversation_id": "CONV_RUNTIME_UPDATE",
        "user_id": "USER_RUNTIME_UPDATE",
    }
    logs = (
        (
            await db_session.execute(
                select(TaskLog)
                .where(TaskLog.task_id == task_id, TaskLog.log_type == "runtime_context")
                .order_by(TaskLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert logs
    assert logs[-1].meta["runtime_context"] == runtime


@pytest.mark.asyncio
async def test_workspace_agent_start_dispatches_planner_and_resolves_single_owner(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))

    headers = await _register(client, "ws_agent_start")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Start Runtime"})
    ws_body = ws.json()
    agent_id = generate_ulid()
    sub_id = generate_ulid()
    db_session.add(
        Agent(
            id=agent_id,
            entity_id=ws_body["entity_id"],
            name="Leasing Consultant",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=sub_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_id,
            workspace_id=ws_body["id"],
            service_key="leasing_consultant",
            status="active",
        )
    )
    await db_session.commit()

    result = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEAGENTSTART00",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEAGENTSTART00",
            action="create_task",
            params={
                "title": "Prepare matching units for Taylor",
                "description": "Use the client's stated budget and move-in date.",
                "start": True,
            },
        )
    )

    task_id = result["task"]["id"]
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()

    assert result["created"] is True
    assert result["dispatched"] is True
    assert dispatched == [task_id]
    assert task.status == "in_progress"
    assert task.owner_service_key == "leasing_consultant"
    assert task.owner_subscription_id == sub_id
    assert task.delegate_service_keys == ["leasing_consultant"]


@pytest.mark.asyncio
async def test_workspace_agent_delegates_to_service_bound_agent(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import json
    from types import SimpleNamespace

    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription

    headers = await _register(client, "ws_agent_delegate")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Delegate Runtime"})
    ws_body = ws.json()
    agent_id = generate_ulid()
    sub_id = generate_ulid()
    db_session.add(
        Agent(
            id=agent_id,
            entity_id=ws_body["entity_id"],
            name="Social Publisher",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=sub_id,
            entity_id=ws_body["entity_id"],
            agent_id=agent_id,
            workspace_id=ws_body["id"],
            service_key="social_publisher",
            custom_prompt="Use the brand voice before posting.",
            status="active",
        )
    )
    await db_session.commit()

    seen: dict[str, object] = {}

    async def fake_build_agent_context(db, **kwargs):
        seen["context_kwargs"] = kwargs
        return SimpleNamespace(
            runtime_envelope=None,
            system_prompt="Social system prompt",
            tools=[{"type": "function", "function": {"name": "mcp__twitter_x__post_tweet"}}],
            allowed_tool_names={"mcp__twitter_x__post_tweet"},
            task_id=None,
            legacy_runtime_profile="workspace_agent",
            model="gpt-test",
            llm_metadata=None,
        )

    async def fake_execute_chat_agent_loop(**kwargs):
        seen["loop_kwargs"] = kwargs
        return SimpleNamespace(
            content="Tweet drafted and queued.",
            rounds=2,
            tool_calls_made=["mcp__twitter_x__post_tweet"],
            usage={"total_tokens": 42},
            stop_reason="completed",
            error=None,
        )

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr(
        "packages.core.ai.runtime.harness.runtime_execute_chat_agent_loop",
        fake_execute_chat_agent_loop,
    )

    result = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEAGENTDELEGATE",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEAGENTDELEGATE",
            action="delegate_service",
            params={
                "service_key": "social_publisher",
                "prompt": "Publish the launch update on X.",
                "max_rounds": 5,
            },
        )
    )

    assert result["delegated"] is True
    assert result["service"]["agent_subscription_id"] == sub_id
    assert result["service"]["agent_id"] == agent_id
    assert result["content"] == "Tweet drafted and queued."
    assert result["tool_calls_made"] == ["mcp__twitter_x__post_tweet"]
    assert seen["context_kwargs"]["agent_id"] == agent_id
    assert seen["context_kwargs"]["workspace_id"] == ws_body["id"]
    assert "Use the brand voice before posting." in seen["context_kwargs"]["extra_system_prompt"]
    assert seen["loop_kwargs"]["agent_id"] == agent_id
    assert seen["loop_kwargs"]["allowed_tool_names"] == {"mcp__twitter_x__post_tweet"}
    assert seen["loop_kwargs"]["max_rounds"] == 5

    missing = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEAGENTDELEGATE",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEAGENTDELEGATE",
            action="delegate_service",
            params={
                "service_key": "unknown_service",
                "prompt": "Do it",
            },
        )
    )
    assert missing["error"] == "workspace_service_agent_not_found"
    assert missing["available_services"][0]["service_key"] == "social_publisher"


@pytest.mark.asyncio
async def test_workspace_agent_create_task_respects_dependency_outputs_before_dispatch(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))

    headers = await _register(client, "ws_agent_dependency")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Dependency Runtime"})
    ws_body = ws.json()
    agent_id = generate_ulid()
    db_session.add(
        Agent(
            id=agent_id,
            entity_id=ws_body["entity_id"],
            name="Content Ops",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            agent_id=agent_id,
            workspace_id=ws_body["id"],
            service_key="content_ops",
            status="active",
        )
    )
    completed_dep_id = generate_ulid()
    waiting_dep_id = generate_ulid()
    db_session.add_all(
        [
            Task(
                id=completed_dep_id,
                entity_id=ws_body["entity_id"],
                workspace_id=ws_body["id"],
                title="Completed competitor scan",
                status="completed",
                actual_output={
                    "summary": "Found five strong social content signals.",
                    "files": [{"name": "signals.md", "path": "workspace/social/signals.md"}],
                },
            ),
            Task(
                id=waiting_dep_id,
                entity_id=ws_body["entity_id"],
                workspace_id=ws_body["id"],
                title="Running product shortlist",
                status="in_progress",
            ),
        ]
    )
    await db_session.commit()

    ready = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEDEP000000",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEDEP000000",
            action="create_task",
            params={
                "title": "Draft X posts from completed scan",
                "owner_service_key": "content_ops",
                "depends_on_task_ids": [completed_dep_id],
                "start": True,
            },
        )
    )
    blocked = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEDEP000000",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEDEP000000",
            action="create_task",
            params={
                "title": "Draft X posts after running shortlist",
                "owner_service_key": "content_ops",
                "depends_on_task_ids": [waiting_dep_id],
                "start": True,
            },
        )
    )

    ready_task = (await db_session.execute(select(Task).where(Task.id == ready["task"]["id"]))).scalar_one()
    blocked_task = (await db_session.execute(select(Task).where(Task.id == blocked["task"]["id"]))).scalar_one()

    assert ready["created"] is True
    assert ready["dispatched"] is True
    assert ready["dispatch_blocked_by_dependencies"] is False
    assert ready_task.status == "in_progress"
    assert ready_task.details["dependency_status"] == "completed"
    assert ready_task.details["dep_outputs"][0]["files"][0]["path"] == "workspace/social/signals.md"

    assert blocked["created"] is True
    assert blocked["dispatched"] is False
    assert blocked["dispatch_blocked_by_dependencies"] is True
    assert blocked_task.status == "pending"
    assert blocked_task.details["dependency_status"] == "waiting"
    assert dispatched == [ready_task.id]


@pytest.mark.asyncio
async def test_plan_finalize_updates_waiting_task_after_successful_replan(db_session, monkeypatch):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    async def _completed_supervisor(_db, _plan, _status):
        return "completed"

    monkeypatch.setattr(PlanExecutor, "_supervise_outcome", staticmethod(_completed_supervisor))

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Next wave: prepare social draft pack",
            status="waiting_on_customer",
            actual_output={
                "plan_id": generate_ulid(),
                "plan_status": "failed",
                "summary": "Old failed replan output should be replaced.",
            },
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    db_session.add(plan)
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="draft_pack",
            kind="llm",
            step_status="done",
            result={
                "text": "Draft pack ready for review.",
                "artifact_materialized": True,
                "files": [
                    {
                        "name": "draft-pack.md",
                        "type": "file",
                        "fs_path": "Workspaces/Social Ops/Artifacts/draft-pack.md",
                    }
                ],
            },
        )
    )
    await db_session.commit()

    event = await PlanExecutor._finalize(db_session, plan, "completed")
    await db_session.flush()

    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "completed"
    assert task.actual_output["plan_id"] == plan_id
    assert task.actual_output["plan_status"] == "completed"
    assert task.actual_output["files"] == [
        {
            "type": "file",
            "step": "draft_pack",
            "source": "fs_path",
            "name": "draft-pack.md",
            "fs_path": "Workspaces/Social Ops/Artifacts/draft-pack.md",
        }
    ]
    assert event["event_type"] == "task.succeeded"
    assert event["payload"]["task_status"] == "completed"


@pytest.mark.asyncio
async def test_task_detail_reconciles_stale_output_from_latest_completed_plan(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    headers = await _register(client, "task_detail_reconcile")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Reconcile"})
    ws_body = ws.json()
    entity_id = ws_body["entity_id"]
    workspace_id = ws_body["id"]
    task_id = generate_ulid()
    old_plan_id = generate_ulid()
    new_plan_id = generate_ulid()

    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Stale waiting task should reconcile",
            status="waiting_on_customer",
            actual_output={
                "plan_id": old_plan_id,
                "plan_status": "failed",
                "steps": [{"key": "assemble", "status": "failed"}],
            },
        )
    )
    db_session.add(
        ExecutionPlan(
            id=new_plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="completed",
            plan_dag={},
        )
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=new_plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="assemble",
            kind="subagent",
            step_status="done",
            result={
                "summary": "Draft pack assembled.",
                "artifact_materialized": True,
                "files": [
                    {
                        "name": "draft-pack.md",
                        "type": "file",
                        "fs_path": "workspace/social_ops/draft-pack.md",
                    }
                ],
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["actual_output"]["plan_id"] == new_plan_id
    assert body["actual_output"]["plan_status"] == "completed"
    assert body["actual_output"]["files"][0]["fs_path"] == "workspace/social_ops/draft-pack.md"
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "completed"
    assert task.actual_output["plan_id"] == new_plan_id


@pytest.mark.asyncio
async def test_task_detail_keeps_open_supervisor_input_request_waiting(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.services.task_service import add_task_log

    headers = await _register(client, "task_detail_open_supervisor_hitl")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task HITL Reconcile"})
    ws_body = ws.json()
    entity_id = ws_body["entity_id"]
    workspace_id = ws_body["id"]
    task_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Completed plan still needs the artifact",
            status="waiting_on_customer",
            actual_output={
                "plan_id": plan_id,
                "plan_status": "completed",
                "supervisor_verdict": "needs_human",
                "needs_input": True,
                "steps": [{"key": "compile", "status": "done", "result_summary": "Draft text only"}],
                "files": None,
            },
        )
    )
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="completed",
            plan_dag={},
        )
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="compile",
            kind="subagent",
            step_status="done",
            result={"summary": "Draft text only", "artifact_materialized": False},
        )
    )
    await db_session.flush()
    await add_task_log(
        db_session,
        task_id,
        "ai_hitl_requested",
        "The plan ran into issues and needs your input.",
        created_by="AI Supervisor",
        metadata={"verdict": "needs_human", "plan_id": plan_id, "artifact_required": True},
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "waiting_on_customer"
    assert body["actual_output"]["supervisor_verdict"] == "needs_human"
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "waiting_on_customer"


@pytest.mark.asyncio
async def test_plan_finalize_asks_supervisor_before_marking_text_output_complete(db_session, monkeypatch):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task, TaskLog
    from packages.core.plans.executor import PlanExecutor

    supervisor_prompts: list[str] = []

    async def fake_chat_completion(messages, **kwargs):
        supervisor_prompts.append(messages[0]["content"])
        return "needs_human", {}

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fake_chat_completion)

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Schedule the 7 drafted posts across the first publishing week",
            status="in_progress",
            priority=3,
            task_type="general",
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    db_session.add(plan)
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="build_schedule",
            kind="subagent",
            step_status="done",
            result={
                "text": (
                    "The execution reached the scheduling step, but the source "
                    "draft material was not available in the workspace run context. "
                    "The schedule was not produced."
                )
            },
        )
    )
    await db_session.commit()

    event = await PlanExecutor._finalize(db_session, plan, "completed")
    await db_session.flush()

    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    logs = list(
        (
            await db_session.execute(
                select(TaskLog).where(TaskLog.task_id == task_id, TaskLog.log_type == "ai_hitl_requested")
            )
        )
        .scalars()
        .all()
    )
    assert task.status == "waiting_on_customer"
    assert task.actual_output["supervisor_verdict"] == "needs_human"
    assert task.actual_output["needs_input"] is True
    assert supervisor_prompts
    assert "Before the parent task status is changed" in supervisor_prompts[-1]
    assert logs and logs[-1].meta["structured_blocker"] is False
    assert event["event_type"] == "task.hitl_requested"


@pytest.mark.asyncio
async def test_plan_finalize_holds_structured_blocker_before_status_update(db_session, monkeypatch):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task, TaskLog
    from packages.core.plans.executor import PlanExecutor

    async def fake_chat_completion(*_args, **_kwargs):
        raise AssertionError("structured blockers should not need supervisor LLM")

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fake_chat_completion)

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Collect source CSV before drafting report",
            status="in_progress",
            priority=3,
            task_type="general",
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    db_session.add(plan)
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="collect_source",
            kind="subagent",
            step_status="done",
            result={
                "status": "needs_input",
                "pending_action": {
                    "kind": "needs_input",
                    "prompt": "Attach the source CSV.",
                },
            },
        )
    )
    await db_session.commit()

    event = await PlanExecutor._finalize(db_session, plan, "completed")
    await db_session.flush()

    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    logs = list(
        (
            await db_session.execute(
                select(TaskLog).where(TaskLog.task_id == task_id, TaskLog.log_type == "ai_hitl_requested")
            )
        )
        .scalars()
        .all()
    )
    assert task.status == "waiting_on_customer"
    assert task.actual_output["supervisor_verdict"] == "needs_human"
    assert logs and logs[-1].meta["structured_blocker"] is True
    assert event["event_type"] == "task.hitl_requested"


def test_task_output_artifact_refs_preserve_document_id_for_viewer_links():
    from packages.core.plans.executor import _artifact_refs_from_result

    refs = _artifact_refs_from_result(
        {
            "artifact_materialized": True,
            "files": [
                {
                    "name": "Maya Chen Follow-up Draft.md",
                    "type": "file",
                    "fs_path": "workspace/artifacts/maya-chen-follow-up.md",
                    "document_id": "01DOCVIEWABLE000000000000",
                }
            ],
        },
        step_key="draft_follow_up",
    )

    assert refs == [
        {
            "type": "file",
            "step": "draft_follow_up",
            "source": "fs_path",
            "name": "Maya Chen Follow-up Draft.md",
            "fs_path": "workspace/artifacts/maya-chen-follow-up.md",
            "document_id": "01DOCVIEWABLE000000000000",
        }
    ]


@pytest.mark.asyncio
async def test_task_detail_reconciles_duplicate_file_refs(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    headers = await _register(client, "task_detail_dedupe_files")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task File Dedupe"})
    ws_body = ws.json()
    entity_id = ws_body["entity_id"]
    workspace_id = ws_body["id"]
    task_id = generate_ulid()
    plan_id = generate_ulid()
    file_path = "workspace/social_ops/x_drafts_next_week.md"

    duplicate_ref = {
        "type": "file",
        "step": "draft",
        "source": "fs_path",
        "fs_path": file_path,
    }
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Completed task with duplicate file refs",
            status="completed",
            actual_output={
                "plan_id": plan_id,
                "plan_status": "completed",
                "files": [duplicate_ref, dict(duplicate_ref)],
            },
        )
    )
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="completed",
            plan_dag={},
        )
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="draft",
            kind="subagent",
            step_status="done",
            result={
                "summary": "Draft pack ready.",
                "artifact_materialized": True,
                "fs_path": file_path,
                "files": [
                    {
                        "name": "x_drafts_next_week.md",
                        "type": "file",
                        "fs_path": file_path,
                    }
                ],
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["actual_output"]["files"] == [duplicate_ref]
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.actual_output["files"] == [duplicate_ref]


@pytest.mark.asyncio
async def test_goal_progress_reconciles_stale_linked_task_from_completed_plan(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.goal import Goal, GoalTaskLink
    from packages.core.models.task import Task

    headers = await _register(client, "goal_reconcile")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Goal Reconcile"})
    ws_body = ws.json()
    entity_id = ws_body["entity_id"]
    workspace_id = ws_body["id"]
    goal_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add(
        Goal(
            id=goal_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Prepare reviewed social draft pack",
            metric_key="draft_pack_ready",
            target_value=1,
            status="active",
        )
    )
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Draft pack task stuck on old output",
            status="waiting_on_customer",
            actual_output={
                "plan_id": generate_ulid(),
                "plan_status": "failed",
            },
        )
    )
    db_session.add(GoalTaskLink(goal_id=goal_id, task_id=task_id, contribution="direct"))
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="completed",
            plan_dag={},
        )
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="assemble",
            kind="subagent",
            step_status="done",
            result={
                "artifact_materialized": True,
                "files": [
                    {
                        "name": "draft-pack.md",
                        "type": "file",
                        "fs_path": "workspace/social_ops/draft-pack.md",
                    }
                ],
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/goals?workspace_id={workspace_id}", headers=headers)

    assert resp.status_code == 200
    goal = next(item for item in resp.json() if item["id"] == goal_id)
    assert goal["task_status_counts"] == {"completed": 1}
    assert goal["task_progress_fraction"] == 1
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "completed"
    assert task.actual_output["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_workspace_agent_rejects_unknown_owner_or_delegate_before_dispatch(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))

    headers = await _register(client, "ws_agent_owner_safe")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Owner Safety"})
    ws_body = ws.json()
    agent_id = generate_ulid()
    db_session.add(
        Agent(
            id=agent_id,
            entity_id=ws_body["entity_id"],
            name="Leasing Consultant",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=ws_body["entity_id"],
            agent_id=agent_id,
            workspace_id=ws_body["id"],
            service_key="leasing_consultant",
            status="active",
        )
    )
    await db_session.commit()

    invalid_owner = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEOWNER00000",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEOWNER00000",
            action="create_task",
            params={
                "title": "Prepare matching units",
                "owner_service_key": "missing_service",
                "start": True,
            },
        )
    )
    invalid_delegate = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id="USERWORKSPACEOWNER00000",
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEOWNER00000",
            action="create_task",
            params={
                "title": "Prepare matching units",
                "owner_service_key": "leasing_consultant",
                "delegate_service_keys": ["missing_delegate"],
                "start": True,
            },
        )
    )

    tasks = (await db_session.execute(select(Task).where(Task.workspace_id == ws_body["id"]))).scalars().all()

    assert invalid_owner["error"] == "owner_service_not_found"
    assert invalid_owner["available_service_keys"] == ["leasing_consultant"]
    assert invalid_delegate["error"] == "delegate_service_not_found"
    assert invalid_delegate["missing_delegate_service_keys"] == ["missing_delegate"]
    assert dispatched == []
    assert tasks == []


@pytest.mark.asyncio
async def test_planner_context_filters_subscriptions_by_task_entity(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.plans.planner import _gather_context

    entity_id = generate_ulid()
    foreign_entity_id = generate_ulid()
    workspace_id = generate_ulid()
    local_agent_id = generate_ulid()
    foreign_agent_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Entity-scoped planning",
    )
    db_session.add_all(
        [
            task,
            Agent(id=local_agent_id, entity_id=entity_id, name="Local Agent", status="active"),
            Agent(id=foreign_agent_id, entity_id=foreign_entity_id, name="Foreign Agent", status="active"),
            AgentSubscription(
                id=generate_ulid(),
                entity_id=entity_id,
                agent_id=local_agent_id,
                workspace_id=workspace_id,
                service_key="local_service",
                status="active",
            ),
            AgentSubscription(
                id=generate_ulid(),
                entity_id=foreign_entity_id,
                agent_id=foreign_agent_id,
                workspace_id=workspace_id,
                service_key="foreign_service",
                status="active",
            ),
        ]
    )
    await db_session.flush()

    ctx = await _gather_context(db_session, task)

    assert ctx.allowed_service_keys == {"local_service"}
    assert [sub.service_key for sub in ctx.subscriptions] == ["local_service"]
    assert set(ctx.agents_by_id) == {local_agent_id}


@pytest.mark.asyncio
async def test_workspace_agent_add_rule_updates_operating_model_and_governance(client: AsyncClient, db_session):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_operation_service import resolve_workspace_operation_review_message

    headers = await _register(client, "ws_agent_rule")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Rule Runtime"})
    ws_body = ws.json()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=ws_body["entity_id"],
            user_id=user_id,
            workspace_id=ws_body["id"],
            title="Workspace rule review",
            channel="workspace",
            scope="workspace_main",
        )
    )
    await db_session.commit()

    result = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id=user_id,
            workspace_id=ws_body["id"],
            conversation_id=conversation_id,
            action="add_rule",
            params={"description": "发 post 必须先给用户审核内容，得到用户同意才能发布。"},
        )
    )

    assert result["__hitl__"] is True
    assert result["hitl"]["action"] == "workspace.operation.apply"
    assert result["operation"]["kind"] == "workspace_operation_review"
    assert result["operation"]["draft_id"] == result["approval_token"]

    replacement = await resolve_workspace_operation_review_message(
        db_session,
        conversation_id=conversation_id,
        entity_id=ws_body["entity_id"],
        user_id=user_id,
        hitl_id=result["approval_token"],
        action="approve",
    )
    assert replacement and replacement.startswith("[Workspace operation approved]")
    await db_session.commit()
    db_session.expire_all()

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_body["id"]))
    ).scalar_one()

    rule = workspace.operating_model["rules"][0]
    assert rule["runtime_enforced"] is True
    assert rule["rule_type"] == "approval_required"
    assert "social_post.publish" in rule["action_patterns"]
    assert "social_post.publish" in policy.policy["hitl_required_actions"]


@pytest.mark.asyncio
async def test_workspace_operation_create_draft_returns_review_card_for_goal_changes(
    client: AsyncClient,
    db_session,
):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_operation_service import resolve_workspace_operation_review_message

    headers = await _register(client, "ws_agent_goal_review")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Goal Review Runtime"})
    ws_body = ws.json()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=ws_body["entity_id"],
            user_id=user_id,
            workspace_id=ws_body["id"],
            title="Workspace goal review",
            channel="workspace",
            scope="workspace_main",
        )
    )
    await db_session.commit()

    result = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id=user_id,
            workspace_id=ws_body["id"],
            conversation_id=conversation_id,
            action="operation",
            params={
                "action": "create_draft",
                "source_event_id": "test.goal.add",
                "patches": [
                    {
                        "op": "goal.add",
                        "payload": {
                            "goal": {
                                "goal_key": "lease_conversions",
                                "title": "Increase lease conversions",
                                "description": "Raise application-to-lease conversion for qualified leads.",
                                "metric_key": "lease_conversion_rate",
                                "target_value": 0.22,
                                "unit": "ratio",
                                "priority": 2,
                            },
                        },
                    }
                ],
            },
        )
    )

    assert result["__hitl__"] is True
    assert result["operation"]["kind"] == "workspace_operation_review"
    assert result["operation"]["draft_id"] == result["approval_token"]
    assert result["hitl"]["action"] == "workspace.operation.apply"
    assert result["draft"]["status"] == "open"
    assert "goals" in result["operation"]["changed_keys"]

    replacement = await resolve_workspace_operation_review_message(
        db_session,
        conversation_id=conversation_id,
        entity_id=ws_body["entity_id"],
        user_id=user_id,
        hitl_id=result["approval_token"],
        action="approve",
    )
    assert replacement and replacement.startswith("[Workspace operation approved]")
    await db_session.commit()
    db_session.expire_all()

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    goal = (
        await db_session.execute(
            select(Goal).where(
                Goal.workspace_id == ws_body["id"],
                Goal.metric_key == "lease_conversion_rate",
            )
        )
    ).scalar_one()
    assert workspace.operation_revision == 1
    assert workspace.operating_model["goals"][0]["goal_key"] == "lease_conversions"
    assert goal.title == "Increase lease conversions"
    assert float(goal.target_value) == pytest.approx(0.22)


@pytest.mark.asyncio
async def test_non_streaming_workspace_chat_persists_operation_review_card(
    client: AsyncClient,
    monkeypatch,
):
    import importlib
    import json
    from types import SimpleNamespace

    agentic_loop_module = importlib.import_module("packages.core.ai.agentic_loop")

    async def fake_agentic_loop(*_args, **kwargs):
        hitl_payload = {
            "__hitl__": True,
            "approval_token": "draft_nonstream_test",
            "hitl": {
                "id": "draft_nonstream_test",
                "type": "approval",
                "prompt": "Apply these workspace runtime changes?",
                "action": "workspace.operation.apply",
                "tool": "workspace_operation",
                "content": {"patches": [{"op": "goal.add"}]},
                "options": ["approve", "reject"],
            },
            "operation": {
                "kind": "workspace_operation_review",
                "draft_id": "draft_nonstream_test",
                "workspace_id": "ws_placeholder",
                "changed_keys": ["goals"],
                "summary": "Review workspace runtime changes: goals.",
                "patches": [{"op": "goal.add"}],
            },
        }
        kwargs["on_tool_start"]("workspace_operation", {"action": "create_draft"})
        kwargs["on_tool_end"](
            "workspace_operation",
            json.dumps(hitl_payload),
            duration_ms=1,
            args={"action": "create_draft"},
        )
        return SimpleNamespace(
            content="Draft ready for review.",
            usage={},
            tool_calls_made=["workspace_operation"],
            rounds=1,
            stop_reason="completed",
            error=None,
            error_detail=None,
            messages=[],
        )

    monkeypatch.setattr(agentic_loop_module, "agentic_loop", fake_agentic_loop)

    headers = await _register(client, "ws_nonstream_hitl")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Non Stream HITL"})
    ws_body = ws.json()

    resp = await client.post(
        "/api/v1/chat/message",
        headers=headers,
        json={
            "message": "新增 workspace 目标，先让我确认。",
            "workspace_id": ws_body["id"],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["tool_calls_made"] == ["workspace_operation"]

    messages = await client.get(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages?limit=20",
        headers=headers,
    )
    rows = messages.json()
    card = next(row for row in rows if row["author_kind"] == "agent")
    assert card["message_kind"] == "hitl_request"
    assert card["pending_action"]["kind"] == "workspace_operation_review"
    assert card["pending_action"]["draft_id"] == "draft_nonstream_test"
    assert card["pending_action"]["operation"]["changed_keys"] == ["goals"]


@pytest.mark.asyncio
async def test_workspace_chat_operation_review_card_applies_draft(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workspace import Workspace

    headers = await _register(client, "ws_chat_op_review")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Operation Review Chat"})
    ws_body = ws.json()

    draft = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "rule.add",
                    "payload": {
                        "rule": {
                            "rule_key": "review_posts_from_card",
                            "description": "Post publishing must be reviewed by the user first.",
                            "rule_type": "approval_required",
                            "action_patterns": ["social_post.publish"],
                        },
                    },
                }
            ],
        },
    )
    assert draft.status_code == 200
    draft_id = draft.json()["id"]
    conv_id = generate_ulid()
    message_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Workspace operation review",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Review workspace operation",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={
                "kind": "workspace_operation_review",
                "draft_id": draft_id,
                "action": "workspace.operation.apply",
                "tool": "workspace_operation",
                "operation": {"kind": "workspace_operation_review", "draft_id": draft_id},
                "options": ["approve", "reject"],
            },
        )
    )
    await db_session.commit()

    resolved = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )

    assert resolved.status_code == 200
    assert resolved.json()["resolution"]["choice"] == "approve"
    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_body["id"]))
    ).scalar_one()
    assert workspace.operation_revision == 1
    assert "social_post.publish" in policy.policy["hitl_required_actions"]


@pytest.mark.asyncio
async def test_workspace_agent_context_resolves_duplicate_operation_hitl_cards(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select
    from apps.api.routers import workspace_chat as workspace_chat_router
    from packages.core.ai.tools import workspace_agent_tools
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Message
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_runtime import resolve_workspace_runtime
    from packages.core.workspace_chat import service as workspace_chat_service

    headers = await _register(client, "ws_chat_text_confirm")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Text Confirm Review"})
    ws_body = ws.json()

    draft = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/operation/drafts",
        headers=headers,
        json={
            "patches": [
                {
                    "op": "rule.add",
                    "payload": {
                        "rule": {
                            "rule_key": "review_posts_text_confirm",
                            "description": "Post publishing must be reviewed by the user first.",
                            "rule_type": "approval_required",
                            "action_patterns": ["social_post.publish"],
                        },
                    },
                }
            ],
        },
    )
    assert draft.status_code == 200
    draft_id = draft.json()["id"]

    conv = await workspace_chat_service.ensure_main_conversation(
        db_session,
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
    )
    conv_id = conv.id
    message_ids = [generate_ulid(), generate_ulid()]
    for message_id in message_ids:
        db_session.add(
            Message(
                id=message_id,
                conversation_id=conv_id,
                role="assistant",
                content="Review workspace operation",
                author_kind="agent",
                message_kind="hitl_request",
                pending_action={
                    "kind": "workspace_operation_review",
                    "draft_id": draft_id,
                    "action": "workspace.operation.apply",
                    "tool": "workspace_operation",
                    "operation": {"kind": "workspace_operation_review", "draft_id": draft_id},
                    "options": ["approve", "reject"],
                },
            )
        )
    await db_session.commit()

    monkeypatch.setattr(
        workspace_chat_router,
        "_schedule_workspace_chat_processing",
        lambda **_: None,
    )

    plain_text = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages",
        headers=headers,
        json={"body": "确认部署"},
    )
    assert plain_text.status_code == 201

    db_session.expire_all()
    rows = (await db_session.execute(select(Message).where(Message.id.in_(message_ids)))).scalars().all()
    assert len(rows) == 2
    assert all(row.resolved_at is None for row in rows)

    envelope = await resolve_workspace_runtime(
        db_session,
        entity_id=ws_body["entity_id"],
        user_id=me["id"],
        conversation_id=conv_id,
        workspace_id=ws_body["id"],
    )
    assert "Open Workspace HITL Requests" in (envelope.extra_context or "")
    assert message_ids[-1] in (envelope.extra_context or "")
    assert draft_id in (envelope.extra_context or "")

    resolved_payload = json.loads(
        await workspace_agent_tools._workspace_resolve_hitl_handler(
            entity_id=ws_body["entity_id"],
            user_id=me["id"],
            workspace_id=ws_body["id"],
            conversation_id=conv_id,
            message_id=message_ids[-1],
            action="approve",
        )
    )
    assert resolved_payload["resolved"] is True
    assert set(resolved_payload["message_ids"]) == set(message_ids)

    db_session.expire_all()
    rows = (await db_session.execute(select(Message).where(Message.id.in_(message_ids)))).scalars().all()
    assert len(rows) == 2
    assert all(row.resolved_at is not None for row in rows)
    assert {row.resolution["choice"] for row in rows} == {"approve"}
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    assert workspace.operation_revision == 1
    refreshed_envelope = await resolve_workspace_runtime(
        db_session,
        entity_id=ws_body["entity_id"],
        user_id=me["id"],
        conversation_id=conv_id,
        workspace_id=ws_body["id"],
    )
    assert "Open Workspace HITL Requests" not in (refreshed_envelope.extra_context or "")

    messages = await client.get(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages?limit=20",
        headers=headers,
    )
    assert messages.status_code == 200
    open_pending = [row for row in messages.json() if row.get("pending_action") and not row.get("resolved_at")]
    assert open_pending == []


@pytest.mark.asyncio
async def test_workspace_chat_feedback_resolution_posts_feedback_message(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message, Task

    headers = await _register(client, "ws_chat_feedback_label")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Feedback Label"})
    ws_body = ws.json()

    conv_id = generate_ulid()
    message_id = generate_ulid()
    review_id = "rv_test_feedback"
    task_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Proposal feedback",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Review these proposals",
            author_kind="agent",
            message_kind="proposal",
            pending_action={
                "kind": "approve_proposals",
                "review_id": review_id,
                "task_ids": [task_id],
                "task_titles": ["Broaden the lead list"],
                "options": ["approve_all", "reject_all"],
            },
        )
    )
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Broaden the lead list",
            status="proposed",
            details={"strategist_review_id": review_id},
        )
    )
    await db_session.commit()

    resolved = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={"choice": "feedback", "note": "Make the proposal narrower."},
    )

    assert resolved.status_code == 200
    assert resolved.json()["resolution"]["choice"] == "feedback"
    rows = (
        (
            await db_session.execute(
                select(Message)
                .where(Message.conversation_id == conv_id, Message.role == "system")
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert rows[-1].content == "✓ Feedback sent — Make the proposal narrower."
    db_session.expire_all()
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "cancelled"
    assert task.details["rejection_reason"] == "Feedback requested: Make the proposal narrower."
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.workspace_id == ws_body["id"],
                RuntimeEvidence.evidence_type == "user_feedback",
            )
        )
    ).scalar_one()
    assert evidence.source == "workspace_chat"
    assert evidence.message_id == message_id
    assert evidence.details["pending_action_kind"] == "approve_proposals"
    assert evidence.details["choice"] == "feedback"
    assert evidence.details["note"] == "Make the proposal narrower."
    assert evidence.details["review_id"] == review_id
    from packages.core.workspace_chat import context as chat_context

    search = await chat_context.workspace_search(
        db_session,
        ws_body["id"],
        ws_body["entity_id"],
        category="runtime",
        query="narrower",
    )
    assert "## Recent Runtime Evidence" in search
    assert "Make the proposal narrower" in search


@pytest.mark.asyncio
async def test_workspace_chat_always_approve_proposals_persists_workspace_preference(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.workspace import Workspace, WorkspaceActivity

    headers = await _register(client, "ws_chat_always_approve_proposals")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Always Approve Proposals"})
    ws_body = ws.json()

    conv_id = generate_ulid()
    message_id = generate_ulid()
    review_id = "rv_test_always_approve"
    task_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Proposal approval",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Review these proposals",
            author_kind="agent",
            message_kind="proposal",
            pending_action={
                "kind": "approve_proposals",
                "review_id": review_id,
                "task_ids": [task_id],
                "task_titles": ["Prepare weekly batch"],
                "options": ["approve_all", "reject_all"],
            },
        )
    )
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Prepare weekly batch",
            status="proposed",
            details={"strategist_review_id": review_id},
        )
    )
    await db_session.commit()

    resolved = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={"choice": "always_approve"},
    )

    assert resolved.status_code == 200
    body = resolved.json()
    assert body["resolution"]["choice"] == "always_approve"
    assert "Future workspace proposals" in body["resolution"]["note"]

    db_session.expire_all()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    assert workspace.settings["strategist"]["auto_approve_proposals"] is True
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.status == "in_progress"

    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.workspace_id == ws_body["id"],
                RuntimeEvidence.evidence_type == "proposal_decision",
            )
        )
    ).scalar_one()
    assert evidence.details["choice"] == "always_approve"

    activity_events = list(
        (
            await db_session.execute(
                select(WorkspaceActivity.event_type).where(
                    WorkspaceActivity.workspace_id == ws_body["id"],
                )
            )
        )
        .scalars()
        .all()
    )
    assert "strategist_proposal.auto_approval_enabled" in activity_events
    assert "strategist_proposal.approved" in activity_events


@pytest.mark.asyncio
async def test_plan_completed_notifications_include_task_ref(monkeypatch):
    from packages.core.workspace_chat import notifiers

    posted: list[dict] = []

    async def fake_safe_post(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(notifiers, "_safe_post", fake_safe_post)

    await notifiers.notify_plan_completed(
        entity_id="entity_1",
        workspace_id="workspace_1",
        plan_id="plan_1",
        task_id="task_1",
        task_title="Follow up with lead",
        duration_seconds=12.0,
        cost_usd=None,
        steps=[],
    )

    assert len(posted) == 2
    assert posted[0]["refs"] == [
        {"type": "plan", "id": "plan_1"},
        {"type": "task", "id": "task_1"},
    ]
    assert posted[1]["refs"] == [
        {"type": "plan", "id": "plan_1"},
        {"type": "task", "id": "task_1"},
    ]


@pytest.mark.asyncio
async def test_workspace_chat_task_thread_messages_include_task_ref(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message, Task

    headers = await _register(client, "ws_chat_task_thread_links")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Thread Links"})
    ws_body = ws.json()

    task_id = generate_ulid()
    conv_id = generate_ulid()
    message_id = generate_ulid()
    existing_ref_message_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Inspect lead audit",
            status="in_progress",
        )
    )
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Inspect lead audit",
            channel="workspace",
            scope="workspace_thread",
            thread_ref_kind="task",
            thread_ref_id=task_id,
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="user",
            content="Please check this task.",
            author_kind="user",
            message_kind="text",
        )
    )
    db_session.add(
        Message(
            id=existing_ref_message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Step failed and needs the task link to explain itself.",
            author_kind="agent",
            message_kind="step_event",
            refs=[{"type": "task", "id": task_id}],
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages",
        headers=headers,
        params={"thread_ref_kind": "task", "thread_ref_id": task_id},
    )

    assert resp.status_code == 200
    row = next(item for item in resp.json() if item["id"] == message_id)
    task_ref = next(ref for ref in row["refs"] if ref["type"] == "task" and ref["id"] == task_id)
    assert task_ref["title"] == "Inspect lead audit"
    assert task_ref["status"] == "in_progress"
    existing_ref_row = next(item for item in resp.json() if item["id"] == existing_ref_message_id)
    existing_task_ref = next(ref for ref in existing_ref_row["refs"] if ref["type"] == "task" and ref["id"] == task_id)
    assert existing_task_ref["title"] == "Inspect lead audit"
    assert existing_task_ref["status"] == "in_progress"


@pytest.mark.asyncio
async def test_workspace_chat_task_completion_feedback_records_message_meta_and_evidence(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Conversation, Message, Task

    headers = await _register(client, "ws_chat_completion_feedback")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Completion Feedback"})
    ws_body = ws.json()

    conv_id = generate_ulid()
    message_id = generate_ulid()
    plan_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Plan updates",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Task(
            id=task_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Follow up with lead",
            status="completed",
        )
    )
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            task_id=task_id,
            status="completed",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="✅ **Task complete — Follow up with lead**",
            author_kind="agent",
            message_kind="agent_update",
            refs=[{"type": "plan", "id": plan_id}],
        )
    )
    await db_session.commit()

    listed = await client.get(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages",
        headers=headers,
    )
    listed_message = next(row for row in listed.json() if row["id"] == message_id)
    task_ref = next(ref for ref in listed_message["refs"] if ref["type"] == "task" and ref["id"] == task_id)
    assert task_ref["title"] == "Follow up with lead"
    assert task_ref["status"] == "completed"

    resp = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages/{message_id}/feedback",
        headers=headers,
        json={"rating": "up"},
    )

    assert resp.status_code == 200
    meta = resp.json()["meta"]
    assert meta["latest_task_completion_feedback"]["rating"] == "up"
    assert list(meta["task_completion_feedback"].values()) == ["up"]

    db_session.expire_all()
    msg = (await db_session.execute(select(Message).where(Message.id == message_id))).scalar_one()
    assert list(msg.meta["task_completion_feedback"].values()) == ["up"]
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.workspace_id == ws_body["id"],
                RuntimeEvidence.evidence_type == "task_completion_feedback",
            )
        )
    ).scalar_one()
    assert evidence.source == "workspace_chat"
    assert evidence.message_id == message_id
    assert evidence.task_id == task_id
    assert evidence.details["rating"] == "up"
    assert evidence.details["plan_id"] == plan_id
    assert evidence.metrics["helpful"] == 1


@pytest.mark.asyncio
async def test_workspace_chat_approve_selected_keeps_dependency_closure(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message, Task

    headers = await _register(client, "ws_chat_approve_selected_deps")
    ws = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Approve Selected Dependencies"},
    )
    ws_body = ws.json()

    conv_id = generate_ulid()
    message_id = generate_ulid()
    review_id = "rv_test_approve_selected_deps"
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    task_c_id = generate_ulid()

    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Proposal approval",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Review these proposals",
            author_kind="agent",
            message_kind="proposal",
            pending_action={
                "kind": "approve_proposals",
                "review_id": review_id,
                "task_ids": [task_a_id, task_b_id, task_c_id],
                "task_titles": [
                    "Prepare source artifact",
                    "Use source artifact",
                    "Optional unrelated task",
                ],
                "options": ["approve_selected", "reject_all"],
            },
        )
    )
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=ws_body["entity_id"],
                workspace_id=ws_body["id"],
                title="Prepare source artifact",
                status="proposed",
                details={"strategist_review_id": review_id, "strategist_task_key": "source"},
            ),
            Task(
                id=task_b_id,
                entity_id=ws_body["entity_id"],
                workspace_id=ws_body["id"],
                title="Use source artifact",
                status="proposed",
                details={
                    "strategist_review_id": review_id,
                    "strategist_task_key": "downstream",
                    "depends_on_task_ids": [task_a_id],
                },
            ),
            Task(
                id=task_c_id,
                entity_id=ws_body["entity_id"],
                workspace_id=ws_body["id"],
                title="Optional unrelated task",
                status="proposed",
                details={"strategist_review_id": review_id, "strategist_task_key": "optional"},
            ),
        ]
    )
    await db_session.commit()

    resolved = await client.post(
        f"/api/v1/workspaces/{ws_body['id']}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={
            "choice": "approve_selected",
            "payload": {"selected_task_ids": [task_b_id]},
        },
    )

    assert resolved.status_code == 200
    db_session.expire_all()
    tasks = list(
        (await db_session.execute(select(Task).where(Task.id.in_([task_a_id, task_b_id, task_c_id])))).scalars().all()
    )
    by_id = {task.id: task for task in tasks}

    assert by_id[task_a_id].status == "in_progress"
    assert by_id[task_b_id].status == "pending"
    assert by_id[task_b_id].details["dependency_status"] == "waiting"
    assert by_id[task_c_id].status == "cancelled"
    assert by_id[task_c_id].details["rejection_reason"] == "Not selected by user"


@pytest.mark.asyncio
async def test_workspace_agent_can_manage_workspace_knowledge(client: AsyncClient, db_session):
    import json
    from sqlalchemy import select
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.workspace import Workspace

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "ws_agent_knowledge",
            "email": "ws_agent_knowledge@test.com",
            "password": "pass123",
            "entity_name": "ws_agent_knowledge Corp",
        },
    )
    assert registered.status_code == 200
    auth_body = registered.json()
    headers = {"Authorization": f"Bearer {auth_body['access_token']}"}
    actor_user_id = auth_body["user_id"]
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Knowledge Runtime"})
    ws_body = ws.json()
    doc_id = generate_ulid()
    db_session.add(
        Document(
            id=doc_id,
            entity_id=ws_body["entity_id"],
            name="customer-faq.md",
            file_type="md",
            mime_type="text/markdown",
            vector_status="ready",
            source="upload",
        )
    )
    await db_session.commit()

    created = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id=actor_user_id,
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEKNOWLEDGE000",
            action="create_knowledge_folder",
            params={
                "name": "Customer FAQ",
                "purpose": "Answer customer questions from approved FAQs.",
                "use_by_default": True,
            },
        )
    )
    assert created["created"] is True
    group_id = created["group"]["id"]

    added = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id=actor_user_id,
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEKNOWLEDGE000",
            action="add_knowledge_documents",
            params={"document_ids": [doc_id], "group_id": group_id},
        )
    )
    assert added["updated"] is True
    assert added["added"][0]["id"] == doc_id

    group = (await db_session.execute(select(DocumentGroup).where(DocumentGroup.id == group_id))).scalar_one()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == ws_body["id"]))).scalar_one()
    member = (
        await db_session.execute(
            select(DocumentGroupMember).where(
                DocumentGroupMember.document_id == doc_id,
                DocumentGroupMember.group_id == group_id,
            )
        )
    ).scalar_one_or_none()
    assert group.workspace_id == ws_body["id"]
    assert group.settings["kind"] == "knowledge_net"
    assert member is not None
    assert group_id in workspace.operating_model["knowledge"]["default_group_ids"]
    assert (
        workspace.operating_model["knowledge"]["group_purposes"][group_id]
        == "Answer customer questions from approved FAQs."
    )

    listed = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id=actor_user_id,
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEKNOWLEDGE000",
            action="list_knowledge",
            params={},
        )
    )
    listed_group = next(item for item in listed["groups"] if item["id"] == group_id)
    assert listed_group["documents"][0]["id"] == doc_id

    removed = json.loads(
        await _workspace_agent_handler(
            entity_id=ws_body["entity_id"],
            user_id=actor_user_id,
            workspace_id=ws_body["id"],
            conversation_id="CONVWORKSPACEKNOWLEDGE000",
            action="remove_knowledge_document",
            params={"document_id": doc_id, "group_id": group_id},
        )
    )
    assert removed["removed"] == 1
    remaining = (
        await db_session.execute(
            select(DocumentGroupMember).where(
                DocumentGroupMember.document_id == doc_id,
                DocumentGroupMember.group_id == group_id,
            )
        )
    ).scalar_one_or_none()
    assert remaining is None
    assert (await db_session.get(Document, doc_id)) is not None


@pytest.mark.asyncio
async def test_workspace_list_stats_filter_cross_entity_dirty_rows(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.goal import Goal
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.workspace import AgentSubscription

    headers = await _register(client, "ws_list_stats_scope")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Stats Scope"})
    ws_body = ws.json()
    foreign_entity_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add(
        Task(
            id=generate_ulid(),
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            title="Foreign task should not count",
            status="pending",
        )
    )
    db_session.add(
        Goal(
            id=generate_ulid(),
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            title="Foreign goal should not count",
            metric_key="foreign_metric",
            target_value=10,
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=foreign_entity_id,
            agent_id=generate_ulid(),
            workspace_id=ws_body["id"],
            service_key="foreign_service",
            status="active",
        )
    )
    conv_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            title="Foreign conversation",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=generate_ulid(),
            conversation_id=conv_id,
            role="assistant",
            content="Foreign HITL",
            pending_action={"kind": "human_input", "step_id": generate_ulid()},
        )
    )
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            task_id=generate_ulid(),
            status="running",
        )
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=foreign_entity_id,
            workspace_id=ws_body["id"],
            step_key="foreign_human",
            kind="human",
            step_status="waiting_human",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/workspaces", headers=headers)

    assert resp.status_code == 200
    stats = next(w["stats"] for w in resp.json() if w["id"] == ws_body["id"])
    assert stats["tasks"] == 0
    assert stats["tasks_active"] == 0
    assert stats["goals"] == 0
    assert stats["agents"] == 0
    assert stats["pending_actions"] == 0
    assert stats["chat_pending_actions"] == 0


@pytest.mark.asyncio
async def test_workspace_list_stats_counts_visible_workspace_chat_actions(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Conversation, Message

    headers = await _register(client, "ws_list_stats_visible_actions")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Visible Actions"})
    ws_body = ws.json()
    entity_id = ws_body["entity_id"]
    workspace_id = ws_body["id"]

    conv_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Pending actions",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="running",
        )
    )
    db_session.add(
        ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="needs_input",
            kind="tool",
            step_status="waiting_human",
        )
    )
    db_session.add_all(
        [
            Message(
                id=generate_ulid(),
                conversation_id=conv_id,
                role="assistant",
                content="Need form answers",
                pending_action={
                    "kind": "needs_input",
                    "step_id": step_id,
                    "plan_id": plan_id,
                    "options": ["provide_answers", "skip"],
                },
            ),
            Message(
                id=generate_ulid(),
                conversation_id=conv_id,
                role="assistant",
                content="Review workspace operation",
                pending_action={
                    "kind": "workspace_operation_review",
                    "draft_id": generate_ulid(),
                    "options": ["approve", "reject"],
                },
            ),
            Message(
                id=generate_ulid(),
                conversation_id=conv_id,
                role="assistant",
                content="Review proposals",
                pending_action={
                    "kind": "approve_proposals",
                    "review_id": "rv_stats",
                    "task_ids": [generate_ulid()],
                },
            ),
            Message(
                id=generate_ulid(),
                conversation_id=conv_id,
                role="assistant",
                content="Review failed",
                pending_action={"kind": "retry_strategist_review"},
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/v1/workspaces", headers=headers)

    assert resp.status_code == 200
    stats = next(w["stats"] for w in resp.json() if w["id"] == workspace_id)
    assert stats["chat_pending_actions"] == 2
    assert stats["pending_actions"] == 2
    assert stats["hitl_tasks"] == 1
    assert stats["proposal_actions"] == 1
    assert stats["failed_actions"] == 1


@pytest.mark.asyncio
async def test_workspace_dashboard_counts_only_workspace_documents(client: AsyncClient, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember

    headers = await _register(client, "ws_dashboard_docs_scope")
    ws_a = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Docs A"})
    ws_b = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Docs B"})
    ws_a_body = ws_a.json()
    ws_b_body = ws_b.json()
    entity_id = ws_a_body["entity_id"]

    group_a = generate_ulid()
    group_b = generate_ulid()
    generated_bucket = generate_ulid()
    docs_a = [generate_ulid(), generate_ulid()]
    doc_b = generate_ulid()
    generated_doc = generate_ulid()
    ungrouped_doc = generate_ulid()
    db_session.add_all(
        [
            DocumentGroup(id=group_a, entity_id=entity_id, workspace_id=ws_a_body["id"], name="A Knowledge"),
            DocumentGroup(id=group_b, entity_id=entity_id, workspace_id=ws_b_body["id"], name="B Knowledge"),
            DocumentGroup(
                id=generated_bucket,
                entity_id=entity_id,
                workspace_id=ws_a_body["id"],
                name="Workspace Files",
                settings={"workspace_file_bucket": True},
            ),
            Document(id=docs_a[0], entity_id=entity_id, name="a1.pdf", file_type="pdf", vector_status="ready"),
            Document(id=docs_a[1], entity_id=entity_id, name="a2.pdf", file_type="pdf", vector_status="ready"),
            Document(id=doc_b, entity_id=entity_id, name="b.pdf", file_type="pdf", vector_status="ready"),
            Document(id=generated_doc, entity_id=entity_id, name="generated.md", file_type="md", vector_status="ready"),
            Document(
                id=ungrouped_doc, entity_id=entity_id, name="entity-wide.pdf", file_type="pdf", vector_status="ready"
            ),
            DocumentGroupMember(document_id=docs_a[0], group_id=group_a),
            DocumentGroupMember(document_id=docs_a[1], group_id=group_a),
            DocumentGroupMember(document_id=doc_b, group_id=group_b),
            DocumentGroupMember(document_id=generated_doc, group_id=generated_bucket),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/workspaces/{ws_a_body['id']}/dashboard", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["documents"]["total"] == 2


@pytest.mark.asyncio
async def test_workspace_staff_assignment_upserts_existing_row(client: AsyncClient):
    headers = await _register(client, "ws_staff_upsert")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Staff Upsert"})
    staff = await client.post("/api/v1/staff", headers=headers, json={"name": "Workspace Reviewer"})
    ws_id = ws.json()["id"]
    staff_id = staff.json()["id"]

    first = await client.post(
        f"/api/v1/workspaces/{ws_id}/staff",
        headers=headers,
        json={"staff_id": staff_id, "role": "reviewer"},
    )
    second = await client.post(
        f"/api/v1/workspaces/{ws_id}/staff",
        headers=headers,
        json={"staff_id": staff_id, "role": "lead"},
    )
    listed = await client.get(f"/api/v1/workspaces/{ws_id}/staff", headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert listed.status_code == 200
    target_rows = [row for row in listed.json() if row["staff_id"] == staff_id]
    assert len(target_rows) == 1
    assert target_rows[0]["role"] == "lead"


@pytest.mark.asyncio
async def test_workspace_heartbeat_enable_disable_manages_runtime_schedules(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "ws_heartbeat_schedule")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Heartbeat Schedule"})
    ws_id = ws.json()["id"]

    enabled = await client.post(f"/api/v1/workspaces/{ws_id}/heartbeat/enable?cadence=daily", headers=headers)
    jobs = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id))).scalars().all()
    jobs_by_id = {j.job_id: j for j in jobs}
    status = await client.get(f"/api/v1/workspaces/{ws_id}/heartbeat/status", headers=headers)

    assert enabled.status_code == 200
    assert status.status_code == 200
    assert status.json()["enabled"] is True
    assert status.json()["heartbeat_enabled"] is True
    assert {f"sr:{ws_id}", f"oe:{ws_id}", f"cie:{ws_id}"} <= set(jobs_by_id)
    assert jobs_by_id[f"sr:{ws_id}"].enabled is True
    assert jobs_by_id[f"sr:{ws_id}"].execution_type == "strategist_review"
    assert jobs_by_id[f"sr:{ws_id}"].execution_target == {"workspace_id": ws_id}

    disabled = await client.post(f"/api/v1/workspaces/{ws_id}/heartbeat/disable", headers=headers)
    jobs_after_disable = (
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id))).scalars().all()
    )

    assert disabled.status_code == 200
    assert jobs_after_disable == []


@pytest.mark.asyncio
async def test_workspace_update_heartbeat_syncs_runtime_schedules(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob

    headers = await _register(client, "ws_update_heartbeat")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Heartbeat Update"})
    ws_id = ws.json()["id"]

    enabled = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=headers,
        json={"heartbeat_enabled": True, "heartbeat_cadence": "weekly"},
    )
    jobs = (
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == ws_id)))
        .scalars()
        .all()
    )

    assert enabled.status_code == 200
    assert {f"sr:{ws_id}", f"oe:{ws_id}", f"cie:{ws_id}"} <= set(jobs)

    disabled = await client.put(
        f"/api/v1/workspaces/{ws_id}",
        headers=headers,
        json={"heartbeat_enabled": False},
    )
    jobs_after_disable = (
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id))).scalars().all()
    )

    assert disabled.status_code == 200
    assert jobs_after_disable == []


@pytest.mark.asyncio
async def test_scheduler_skips_workspace_job_when_workspace_paused(client: AsyncClient, db_session):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.scheduler_tasks import _dispatch_job

    headers = await _register(client, "ws_scheduler_guard")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Scheduler Guard"})
    ws_body = ws.json()
    pause = await client.post(f"/api/v1/workspaces/{ws_body['id']}/pause", headers=headers)
    assert pause.status_code == 200

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"guard:{ws_body['id']}",
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        name="Paused workspace automation",
        job_type="interval",
        schedule_kind="every",
        every_seconds=60,
        execution_type="agent",
        payload_message="should not dispatch",
        enabled=True,
    )
    db_session.add(job)
    await db_session.flush()

    await _dispatch_job(db_session, job, datetime.now(timezone.utc))

    run = (await db_session.execute(select(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id))).scalar_one()
    assert run.status == "skipped"
    assert run.result == {"skipped": True, "reason": "workspace_paused"}
    assert job.last_status == "skipped"


@pytest.mark.asyncio
async def test_scheduler_disables_manual_goal_measurement_jobs(client: AsyncClient, db_session):
    from datetime import datetime, timezone
    from decimal import Decimal
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.scheduler_tasks import _dispatch_job

    headers = await _register(client, "ws_manual_goal_scheduler")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Manual Goal Scheduler"})
    ws_body = ws.json()
    goal = Goal(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Prepare reviewed X drafts",
        metric_key="x_drafts_ready",
        target_value=Decimal("3"),
        measurement_source={"provider": "manual", "params": {}},
        measurement_cadence="weekly",
        status="active",
    )
    db_session.add(goal)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"gm:{goal.id}",
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        name="Legacy manual goal measurement",
        job_type="interval",
        schedule_kind="every",
        every_seconds=60,
        execution_type="goal_measurement",
        execution_target={"goal_id": goal.id},
        goal_id=goal.id,
        enabled=True,
    )
    db_session.add(job)
    await db_session.flush()

    await _dispatch_job(db_session, job, datetime.now(timezone.utc))

    run = (await db_session.execute(select(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id))).scalar_one()
    assert run.status == "skipped"
    assert run.result == {"skipped": True, "reason": "manual_measurement_required"}
    assert job.last_status == "skipped"
    assert job.enabled is False


@pytest.mark.asyncio
async def test_scheduler_disables_achieved_goal_measurement_jobs(client: AsyncClient, db_session):
    from datetime import datetime, timezone
    from decimal import Decimal
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.scheduler_tasks import _dispatch_job

    headers = await _register(client, "ws_achieved_goal_scheduler")
    ws = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Achieved Goal Scheduler"})
    ws_body = ws.json()
    goal = Goal(
        id=generate_ulid(),
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        title="Prepare reviewed X drafts",
        metric_key="x_drafts_ready",
        target_value=Decimal("3"),
        current_value=Decimal("3"),
        measurement_source={"provider": "workspace_internal", "params": {"mode": "linked_task_impact"}},
        measurement_cadence="weekly",
        status="achieved",
        pace_status="achieved",
    )
    db_session.add(goal)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"gm:{goal.id}",
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
        name="Legacy achieved goal measurement",
        job_type="interval",
        schedule_kind="every",
        every_seconds=60,
        execution_type="goal_measurement",
        execution_target={"goal_id": goal.id},
        goal_id=goal.id,
        enabled=True,
    )
    db_session.add(job)
    await db_session.flush()

    await _dispatch_job(db_session, job, datetime.now(timezone.utc))

    run = (await db_session.execute(select(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id))).scalar_one()
    assert run.status == "skipped"
    assert run.result == {"skipped": True, "reason": "goal_achieved"}
    assert job.last_status == "skipped"
    assert job.enabled is False

    await db_session.commit()
    fresh_job = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == job.job_id))).scalar_one()
    assert fresh_job.enabled is False


def test_workspace_goal_target_parser_accepts_architect_shape():
    from packages.core.services.workspace_setup_service import _coerce_goal_number

    assert _coerce_goal_number("10,000") == 10000
    assert _coerce_goal_number("5%") == 5
    assert _coerce_goal_number({"bad": "shape"}) == 0


@pytest.mark.asyncio
async def test_custom_agent_provisioning_rejects_unknown_tools_and_cross_entity_skills(db_session):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import AgentToolBinding, ToolDefinition
    from packages.core.services.agent_provisioning_service import (
        CustomAgentSpec,
        provision_custom_agent,
    )

    entity_id = generate_ulid()
    other_entity_id = generate_ulid()
    known_tool_id = generate_ulid()
    entity_skill_id = generate_ulid()
    public_skill_id = generate_ulid()
    other_skill_id = generate_ulid()

    db_session.add(
        ToolDefinition(
            id=known_tool_id,
            name="known_entity_tool",
            display_name="Known Entity Tool",
            status="active",
        )
    )
    db_session.add(
        Skill(
            id=entity_skill_id,
            entity_id=entity_id,
            name="Entity Skill",
            slug="entity_skill",
            system_prompt="Use the entity-approved workflow.",
            is_public=False,
            status="active",
        )
    )
    db_session.add(
        Skill(
            id=public_skill_id,
            entity_id=other_entity_id,
            name="Public Skill",
            slug="public_skill",
            system_prompt="Use the public workflow.",
            is_public=True,
            status="active",
        )
    )
    db_session.add(
        Skill(
            id=other_skill_id,
            entity_id=other_entity_id,
            name="Other Private Skill",
            slug="other_private_skill",
            system_prompt="This private workflow belongs to another entity.",
            is_public=False,
            status="active",
        )
    )
    await db_session.flush()

    result = await provision_custom_agent(
        db_session,
        entity_id=entity_id,
        spec=CustomAgentSpec(
            agent_name="Scoped Agent",
            system_prompt="You are Scoped Agent. Work only with explicitly bound tools and skills.",
            tool_bindings=["known_entity_tool", "ghost_tool"],
            skill_bindings=["entity_skill", "public_skill", other_skill_id],
        ),
    )

    assert result.bound_tools == ["known_entity_tool"]
    assert "tool not found: ghost_tool" in result.warnings
    assert sorted(result.bound_skills) == ["entity_skill", "public_skill"]
    assert f"skill not found: {other_skill_id}" in result.warnings

    bound_tool_ids = set(
        (await db_session.execute(select(AgentToolBinding.tool_id).where(AgentToolBinding.agent_id == result.agent_id)))
        .scalars()
        .all()
    )
    assert bound_tool_ids == {known_tool_id}
    assert (
        await db_session.execute(select(ToolDefinition).where(ToolDefinition.name == "ghost_tool"))
    ).scalar_one_or_none() is None

    bound_skill_ids = set(
        (
            await db_session.execute(
                select(AgentSkillBinding.skill_id).where(AgentSkillBinding.agent_id == result.agent_id)
            )
        )
        .scalars()
        .all()
    )
    assert bound_skill_ids == {entity_skill_id, public_skill_id}


@pytest.mark.asyncio
async def test_workspace_architect_custom_agent_redesign_clears_stale_integration_flags(db_session):
    import copy
    import json

    from packages.core.ai.tools.workspace_arch_tools import _request_custom_agent
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    fields = copy.deepcopy(DEFAULT_FIELDS)
    fields["agent_mappings"] = [
        {
            "service_key": "wechat_growth",
            "strategy": "create_custom",
            "create_agent_draft": {
                "agent_name": "WeChat Growth Agent",
                # This mirrors the production stale-state bug: an earlier turn
                # already rewrote the agent without missing integrations, but the
                # workspace-wide warning list still contains old agent-design flags.
                "missing_integrations": [],
            },
        }
    ]
    fields["flagged_integrations"] = [
        {
            "provider": "openai",
            "purpose": "Old upstream model key baked into the agent design.",
            "required": True,
            "linked_service_keys": ["wechat_growth"],
            "source": "agent_design",
            "agent_name": "WeChat Growth Agent",
        },
        {
            "provider": "anthropic",
            "purpose": "Old upstream Claude key baked into the agent design.",
            "required": True,
            "linked_service_keys": ["wechat_growth"],
            # Legacy rows may not have source; still clear if they came from
            # the old custom-agent missing_integrations for this service.
            "agent_name": "WeChat Growth Agent",
        },
        {
            "provider": "wechat",
            "purpose": "WeChat customer updates still need a real account connection.",
            "required": True,
            "linked_service_keys": ["wechat_growth"],
            "source": "explicit",
        },
        {
            "provider": "stripe",
            "purpose": "Billing operations still need Stripe.",
            "required": True,
            "linked_service_keys": ["subscription_ops"],
            "source": "explicit",
        },
    ]
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=None,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(
        await _request_custom_agent(
            db_session,
            entity_id=entity_id,
            draft_id=draft.id,
            service_key="wechat_growth",
            agent_name="WeChat Growth Agent",
            system_prompt=(
                "You are WeChat Growth Agent. Operate WeChat growth workflows using "
                "the entity's approved platform capabilities. Stay in growth scope."
            ),
            tool_bindings=["workspace_agent"],
            missing_integrations=[],
        )
    )

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    flags = draft.fields["flagged_integrations"]
    assert {flag["provider"] for flag in flags} == {"wechat", "stripe"}
    assert all(flag.get("source") == "explicit" for flag in flags)


@pytest.mark.asyncio
async def test_workspace_architect_can_remove_explicit_integration_warning(db_session):
    import copy
    import json

    from packages.core.ai.tools.workspace_arch_tools import _remove
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    fields = copy.deepcopy(DEFAULT_FIELDS)
    fields["flagged_integrations"] = [
        {
            "provider": "openai",
            "purpose": "Operator removed this upstream model dependency.",
            "required": True,
            "source": "explicit",
        },
        {
            "provider": "wechat",
            "purpose": "WeChat customer updates still need a real account connection.",
            "required": True,
            "source": "explicit",
        },
    ]
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=None,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(
        await _remove(
            db_session,
            entity_id=entity_id,
            draft_id=draft.id,
            kind="integration",
            key="OpenAI",
        )
    )

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    assert [flag["provider"] for flag in draft.fields["flagged_integrations"]] == ["wechat"]


@pytest.mark.asyncio
async def test_workspace_architect_missing_integrations_use_supported_catalog_and_chrome(db_session):
    import copy
    import json

    from sqlalchemy import select
    from packages.core.ai.tools.workspace_arch_tools import _flag_missing_integration
    from packages.core.models.mcp import MCPServer
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    existing_chrome = (
        await db_session.execute(select(MCPServer).where(MCPServer.server_key == "chrome"))
    ).scalar_one_or_none()
    if existing_chrome is None:
        db_session.add(
            MCPServer(
                id=generate_ulid(),
                server_key="chrome",
                name="Chrome",
                description="Local Chrome browser",
                transport="builtin",
                endpoint="packages.core.ai.mcp.chrome",
                auth_type="cli_worker",
                status="active",
            )
        )

    entity_id = generate_ulid()
    fields = copy.deepcopy(DEFAULT_FIELDS)
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=None,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    unsupported = json.loads(
        await _flag_missing_integration(
            db_session,
            entity_id=entity_id,
            draft_id=draft.id,
            provider="openai",
            purpose="Model key should not be treated as an Integration card.",
        )
    )
    assert unsupported["ok"] is True
    assert unsupported["skipped"] is True

    browser_platform = json.loads(
        await _flag_missing_integration(
            db_session,
            entity_id=entity_id,
            draft_id=draft.id,
            provider="instagram",
            purpose="Use the Instagram web UI for review work.",
            linked_service_keys=["social_ops"],
        )
    )
    assert browser_platform["ok"] is True
    assert browser_platform["provider"] == "chrome"
    assert browser_platform["covered_provider"] == "instagram"

    await db_session.flush()
    await db_session.refresh(draft)
    assert draft.fields["flagged_integrations"] == [
        {
            "provider": "chrome",
            "purpose": "Use the Instagram web UI for review work.",
            "required": True,
            "linked_service_keys": ["social_ops"],
            "source": "explicit",
            "covered_provider": "instagram",
        }
    ]


@pytest.mark.asyncio
async def test_workspace_architect_channel_remove_cleans_runtime_references(db_session):
    import copy
    import json

    from packages.core.ai.tools.workspace_arch_tools import _remove
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    fields = copy.deepcopy(DEFAULT_FIELDS)
    fields.update(
        {
            "name": "Leasing Consultant",
            "kind": "project",
            "operating_context": "Apartment leasing operations.",
            "primary_work": "Qualify prospects, draft follow-up emails, and coordinate tours.",
            "channel_config": {
                "primary_external_channel": {"channel_type": "webchat", "purpose": "Inbound prospects"},
                "internal_channel": {"channel_type": "internal_chat", "purpose": "Team review for email drafts"},
                "secondary_external_channels": [
                    {"channel_type": "email", "purpose": "Outbound drafts"},
                ],
                "channels": [
                    {"role": "secondary_external", "channel_type": "email", "purpose": "Legacy email row"},
                ],
            },
            "rules": [
                {
                    "rule_key": "approval_before_external_message",
                    "description": "No external message, email, chat reply, or tour confirmation may be sent without approval.",
                    "rule_type": "approval_required",
                    "action_patterns": ["email.send", "external_message.send"],
                },
                {
                    "rule_key": "stale_lead_escalation",
                    "description": "The agent may draft a re-engagement email but may not send it without approval.",
                    "rule_type": "approval_required",
                    "action_patterns": ["email.send"],
                },
            ],
            "automations": [
                {
                    "automation_key": "new_lead_draft_response",
                    "description": "When a new inquiry is received via webchat or email, draft the initial email response.",
                    "trigger": "on_message_received",
                    "service_key": "followup_drafting",
                },
            ],
            "services": [
                {
                    "service_key": "followup_drafting",
                    "name": "Follow-Up Email Drafting",
                    "description": "Drafts personalised follow-up emails for each prospect stage.",
                    "autonomy_level": "supervised",
                    "owner_role": "leasing_consultant",
                },
            ],
            "goals": [
                {
                    "goal_key": "lead_response_time",
                    "title": "Lead Response Time",
                    "description": "Ensure every inquiry receives a drafted follow-up email quickly.",
                    "target": "2 hours",
                    "cadence": "daily",
                },
            ],
            "knowledge_attachments": [
                {
                    "name": "Leasing Playbook & Email Templates",
                    "purpose": "Brand voice and follow-up email templates.",
                    "mode": "create_new",
                    "linked_service_keys": ["followup_drafting"],
                },
                {
                    "name": "Leasing Playbook & Message Templates",
                    "purpose": "Brand voice and webchat message templates.",
                    "mode": "create_new",
                    "linked_service_keys": ["followup_drafting"],
                },
            ],
        }
    )
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=None,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(
        await _remove(
            db_session,
            entity_id=entity_id,
            draft_id=draft.id,
            kind="channel",
            key="email",
        )
    )

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    channel_config = draft.fields["channel_config"]
    assert channel_config["secondary_external_channels"] == []
    assert channel_config["channels"] == []
    assert "email" in draft.fields["_removed_channels"]
    assert draft.fields["primary_work"] == "Qualify prospects, draft follow-up messages, and coordinate tours."
    assert draft.fields["services"][0]["name"] == "Follow-Up Message Drafting"
    assert "emails" not in draft.fields["services"][0]["description"].lower()
    assert "follow-up message" in draft.fields["goals"][0]["description"].lower()
    assert [item["name"] for item in draft.fields["knowledge_attachments"]] == ["Leasing Playbook & Message Templates"]
    rendered = json.dumps(draft.fields, ensure_ascii=False).lower()
    assert "email.send" not in rendered
    assert "via webchat or email" not in rendered
    assert "re-engagement email" not in rendered
    assert "initial email response" not in rendered
    visible_fields = copy.deepcopy(draft.fields)
    visible_fields.pop("_removed_channels", None)
    assert "email" not in json.dumps(visible_fields, ensure_ascii=False).lower()


def test_workspace_draft_reconcile_applies_removed_channel_tombstones():
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_draft_service import reconcile_draft_fields

    draft = WorkspaceDraft(
        entity_id="01K00000000000000000000000",
        user_id=None,
        fields={
            "_removed_channels": ["email"],
            "primary_work": "Draft follow-up emails for approval.",
            "services": [
                {
                    "service_key": "followup_drafting",
                    "name": "Follow-Up Email Drafting",
                    "description": "Drafts email responses.",
                    "tool_bindings": ["email.send", "workspace_search"],
                }
            ],
        },
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )

    assert reconcile_draft_fields(draft) is True
    assert draft.fields["primary_work"] == "Draft follow-up messages for approval."
    assert draft.fields["services"][0]["name"] == "Follow-Up Message Drafting"
    assert draft.fields["services"][0]["tool_bindings"] == ["workspace_search"]
    visible_fields = dict(draft.fields)
    visible_fields.pop("_removed_channels", None)
    assert "email" not in json.dumps(visible_fields, ensure_ascii=False).lower()


@pytest.mark.asyncio
async def test_workspace_architect_replaces_same_signature_automation(db_session):
    import copy
    import json

    from packages.core.ai.tools.workspace_arch_tools import _propose_automation
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    fields = copy.deepcopy(DEFAULT_FIELDS)
    fields["automations"] = [
        {
            "automation_key": "new_lead_draft_response",
            "description": "Old draft response automation.",
            "trigger": "on_message_received",
            "service_key": "followup_drafting",
        },
        {
            "automation_key": "daily_pipeline_summary",
            "description": "Daily summary.",
            "trigger": "daily 09:00",
            "service_key": "pipeline_tracking",
        },
    ]
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=None,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(
        await _propose_automation(
            db_session,
            entity_id=entity_id,
            draft_id=draft.id,
            automation_key="new_inquiry_draft",
            description="Updated webchat inquiry automation.",
            trigger="on_message_received",
            service_key="followup_drafting",
        )
    )

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    keys = [item["automation_key"] for item in draft.fields["automations"]]
    assert keys == ["daily_pipeline_summary", "new_inquiry_draft"]


@pytest.mark.asyncio
async def test_workspace_draft_field_patch_reconciles_stale_integration_flags(client: AsyncClient, db_session):
    import copy

    from sqlalchemy import select
    from packages.core.models.user import User
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    username = "ws_draft_patch_reconcile"
    headers = await _register(client, username)
    user = (await db_session.execute(select(User).where(User.email == f"{username}@test.com"))).scalar_one()

    fields = copy.deepcopy(DEFAULT_FIELDS)
    fields["name"] = "Patch Reconcile"
    fields["kind"] = "operations"
    fields["operating_context"] = "Test workspace draft patches."
    fields["primary_work"] = "Check stale setup warnings."
    fields["services"] = [{"key": "growth", "service_key": "growth", "name": "Growth"}]
    fields["agent_mappings"] = [
        {
            "service_key": "growth",
            "strategy": "create_custom",
            "create_agent_draft": {
                "agent_name": "Growth Agent",
                "missing_integrations": [{"provider": "openai", "purpose": "Old key", "required": True}],
            },
        }
    ]
    fields["flagged_integrations"] = [
        {
            "provider": "openai",
            "purpose": "Old key",
            "required": True,
            "linked_service_keys": ["growth"],
            "source": "agent_design",
            "agent_name": "Growth Agent",
        }
    ]
    draft = WorkspaceDraft(
        entity_id=user.entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.commit()

    patched = await client.patch(
        f"/api/v1/workspace-drafts/{draft.id}/fields",
        headers=headers,
        json={
            "agent_mappings": [
                {
                    "service_key": "growth",
                    "strategy": "create_custom",
                    "create_agent_draft": {
                        "agent_name": "Growth Agent",
                        "missing_integrations": [],
                    },
                }
            ],
        },
    )

    assert patched.status_code == 200
    assert patched.json()["fields"]["flagged_integrations"] == []


@pytest.mark.asyncio
async def test_finalize_setup_materializes_architect_goal_target(db_session):
    from sqlalchemy import select
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    session = WorkspaceSetupSession(
        entity_id="ENTGOALTARGET000000000000",
        fields={
            "name": "Goal Target Workspace",
            "kind": "campaign",
            "operating_context": "A test campaign",
            "primary_work": "Track audience growth",
            "services": [],
            "agent_mappings": [],
            "goals": [
                {
                    "goal_key": "audience_growth",
                    "title": "Audience Growth",
                    "description": "Grow the audience to ten thousand people.",
                    "target": "10,000",
                    "cadence": "weekly",
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    goal = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace_id))).scalar_one()
    measurement_jobs = (
        (
            await db_session.execute(
                select(ScheduledJob).where(
                    ScheduledJob.workspace_id == workspace_id,
                    ScheduledJob.execution_type == "goal_measurement",
                )
            )
        )
        .scalars()
        .all()
    )

    assert float(goal.target_value) == 10000
    assert float(goal.baseline_value) == 0
    assert goal.measurement_source == {
        "provider": "workspace_internal",
        "params": {"mode": "linked_task_impact"},
    }
    assert goal.measurement_cadence == "weekly"
    assert len(measurement_jobs) == 1
    assert measurement_jobs[0].job_id == f"gm:{goal.id}"
    assert measurement_jobs[0].enabled is True
    assert measurement_jobs[0].every_seconds == 604800.0


@pytest.mark.asyncio
async def test_finalize_setup_materializes_draft_automations(db_session):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    session = WorkspaceSetupSession(
        entity_id="ENTAUTOMATION00000000000",
        fields={
            "name": "Automation Workspace",
            "kind": "ops",
            "operating_context": "A recurring operations workspace",
            "primary_work": "Run scheduled operating routines.",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "channel_config": {},
            "automations": [
                {
                    "automation_key": "weekly_digest",
                    "service_key": "ops",
                    "trigger": "Every Monday at 8am",
                    "description": "Prepare a weekly workspace digest.",
                }
            ],
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    job = (
        await db_session.execute(
            select(ScheduledJob).where(
                ScheduledJob.job_id == f"wa:{workspace_id}:weekly-digest",
            )
        )
    ).scalar_one()

    assert job.workspace_id == workspace_id
    assert job.execution_type == "agent"
    assert job.cron_expr == "0 8 * * 1"
    assert "Prepare a weekly workspace digest." in (job.payload_message or "")


@pytest.mark.asyncio
async def test_workspace_architect_sets_credit_budget(db_session):
    import json
    from packages.core.ai.tools.workspace_arch_tools import _set_budget
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft

    entity_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        fields={},
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    raw = await _set_budget(
        db_session,
        entity_id=entity_id,
        draft_id=draft.id,
        monthly_budget_credits=12000,
        auto_pause_on_budget=False,
        notes="Founder-requested cap",
    )
    data = json.loads(raw)
    await db_session.flush()
    await db_session.refresh(draft)

    assert data["ok"] is True
    assert draft.fields["budget_policy"] == {
        "monthly_budget_credits": 12000,
        "auto_pause_on_budget": False,
        "notes": "Founder-requested cap",
    }


@pytest.mark.asyncio
async def test_finalize_setup_applies_credit_budget(db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.services.credit_service import usd_to_credits
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    entity_id = generate_ulid()
    session = WorkspaceSetupSession(
        entity_id=entity_id,
        fields={
            "name": "Budgeted Workspace",
            "kind": "campaign",
            "operating_context": "A budget-controlled campaign",
            "primary_work": "Draft and route content without overspending.",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "channel_config": {},
            "budget_policy": {
                "monthly_budget_credits": 12000,
                "auto_pause_on_budget": False,
                "notes": "Stay within a founder-approved monthly cap.",
            },
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()

    assert usd_to_credits(float(workspace.monthly_budget_usd)) == 12000
    assert workspace.auto_pause_on_budget is False
    assert workspace.budget_alert_state == "normal"
    assert workspace.operating_model["budget_policy"] == {
        "monthly_budget_credits": 12000,
        "auto_pause_on_budget": False,
        "notes": "Stay within a founder-approved monthly cap.",
    }


@pytest.mark.asyncio
async def test_finalize_setup_create_custom_agent_gets_workspace_runtime_bindings(db_session):
    from sqlalchemy import select
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import AgentSubscription, AgentToolBinding, ToolDefinition, Workspace
    from packages.core.models.base import generate_ulid
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    entity_id = generate_ulid()
    skill_id = generate_ulid()
    db_session.add(
        Skill(
            id=skill_id,
            entity_id=entity_id,
            name="Customer Reply",
            slug="customer_reply",
            description="Draft customer-facing replies.",
            system_prompt="Write concise customer-facing replies.",
            status="active",
        )
    )
    await db_session.commit()

    session = WorkspaceSetupSession(
        entity_id=entity_id,
        fields={
            "name": "Custom Agent Workspace",
            "kind": "support_desk",
            "operating_context": "A support workspace",
            "primary_work": "Answer customer questions and create deliverables",
            "services": [{"service_key": "customer_support", "title": "Customer Support"}],
            "agent_mappings": [
                {
                    "service_key": "customer_support",
                    "strategy": "create_custom",
                    "create_agent_draft": {
                        "agent_name": "Customer Support Agent",
                        "system_prompt": (
                            "You are Customer Support Agent. Help with customer support "
                            "work across subscribed workspaces. Stay in support scope."
                        ),
                        "tool_bindings": ["web_search", "web_search"],
                        "skill_bindings": ["customer_reply"],
                    },
                }
            ],
            "goals": [],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()

    subscription = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == "customer_support",
            )
        )
    ).scalar_one()
    tool_names = set(
        (
            await db_session.execute(
                select(ToolDefinition.name)
                .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
                .where(AgentToolBinding.agent_id == subscription.agent_id)
            )
        )
        .scalars()
        .all()
    )
    skill_binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == subscription.agent_id,
                AgentSkillBinding.skill_id == skill_id,
                AgentSkillBinding.status == "active",
            )
        )
    ).scalar_one_or_none()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()

    assert {
        "web_search",
        "manor",
        "workspace_agent",
        "rag",
        "generate_file",
        "invoke_skill",
    }.issubset(tool_names)
    assert skill_binding is not None
    assert workspace.operating_model["agent_mappings"][0]["service_key"] == "customer_support"


@pytest.mark.asyncio
async def test_finalize_setup_creates_default_selected_knowledge(db_session):
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroup
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    session = WorkspaceSetupSession(
        entity_id="ENTKNOWLEDGE000000000000",
        fields={
            "name": "Knowledge Workspace",
            "kind": "support_desk",
            "operating_context": "A test support desk",
            "primary_work": "Answer product questions",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "knowledge_attachments": [
                {
                    "name": "Support FAQ",
                    "purpose": "Answers and escalation guidance",
                    "mode": "create_new",
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    group = (
        await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id))
    ).scalar_one()

    assert group.name == "Support FAQ"
    assert group.settings["purpose"] == "Answers and escalation guidance"
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    knowledge = workspace.operating_model["knowledge"]
    assert knowledge["default_group_ids"] == [group.id]
    assert knowledge["group_purposes"][group.id] == "Answers and escalation guidance"
    assert knowledge["auto_search"] is True
    assert knowledge["citation_required"] is True


@pytest.mark.asyncio
async def test_finalize_setup_clones_existing_knowledge_group_without_stealing_it(db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    entity_id = generate_ulid()
    source_group_id = generate_ulid()
    doc_id = generate_ulid()
    db_session.add(
        Document(
            id=doc_id,
            entity_id=entity_id,
            name="existing-faq.md",
            file_type="md",
            vector_status="ready",
            source="upload",
        )
    )
    db_session.add(
        DocumentGroup(
            id=source_group_id,
            entity_id=entity_id,
            name="Existing FAQ",
            settings={"purpose": "Original global FAQ", "user_manageable": True},
        )
    )
    db_session.add(DocumentGroupMember(document_id=doc_id, group_id=source_group_id))
    await db_session.commit()

    session = WorkspaceSetupSession(
        entity_id=entity_id,
        fields={
            "name": "Linked Knowledge Workspace",
            "kind": "support_desk",
            "operating_context": "A test support desk",
            "primary_work": "Answer product questions",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "knowledge_attachments": [
                {
                    "name": "Workspace FAQ",
                    "purpose": "Workspace-specific FAQ source",
                    "mode": "link_existing",
                    "existing_group_id": source_group_id,
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()

    source_group = (
        await db_session.execute(select(DocumentGroup).where(DocumentGroup.id == source_group_id))
    ).scalar_one()
    cloned_groups = (
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    cloned_group = next(
        group for group in cloned_groups if (group.settings or {}).get("source_existing_group_id") == source_group_id
    )
    cloned_member = (
        await db_session.execute(
            select(DocumentGroupMember).where(
                DocumentGroupMember.document_id == doc_id,
                DocumentGroupMember.group_id == cloned_group.id,
            )
        )
    ).scalar_one_or_none()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()

    assert source_group.workspace_id is None
    assert cloned_group.name == "Workspace FAQ"
    assert cloned_group.settings["kind"] == "knowledge_net"
    assert cloned_group.settings["purpose"] == "Workspace-specific FAQ source"
    assert cloned_member is not None
    assert workspace.operating_model["knowledge"]["default_group_ids"] == [cloned_group.id]


@pytest.mark.asyncio
async def test_finalize_setup_starter_doc_generation_is_explicit_for_template_clone(db_session, monkeypatch):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup
    from packages.core.tasks import ai_tasks

    dispatched: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks.generate_knowledge_content,
        "delay",
        lambda *args: dispatched.append(args),
    )

    entity_id = generate_ulid()
    template_group_id = generate_ulid()
    doc_id = generate_ulid()
    db_session.add(
        Document(
            id=doc_id,
            entity_id=entity_id,
            name="template-sop.md",
            file_type="md",
            vector_status="ready",
            source="upload",
        )
    )
    db_session.add(
        DocumentGroup(
            id=template_group_id,
            entity_id=entity_id,
            name="Template SOP",
            settings={"purpose": "Reusable SOP template"},
        )
    )
    db_session.add(DocumentGroupMember(document_id=doc_id, group_id=template_group_id))
    await db_session.commit()

    async def _finalize_clone(name: str, generate_starter_doc: bool | None = None) -> str:
        attachment = {
            "name": name,
            "purpose": "Workspace SOP",
            "mode": "clone_template",
            "template_group_id": template_group_id,
        }
        if generate_starter_doc is not None:
            attachment["generate_starter_doc"] = generate_starter_doc
        session = WorkspaceSetupSession(
            entity_id=entity_id,
            fields={
                "name": f"{name} Workspace",
                "kind": "support_desk",
                "operating_context": "A test support desk",
                "primary_work": "Answer product questions",
                "services": [],
                "agent_mappings": [],
                "goals": [],
                "knowledge_attachments": [attachment],
                "channel_config": {},
            },
            messages=[],
            ready=True,
            missing=[],
        )
        return await finalize_setup(session, db_session)

    first_workspace_id = await _finalize_clone("Template Clone")
    await db_session.commit()
    assert dispatched == []

    second_workspace_id = await _finalize_clone("Template Clone With Starter", True)
    await db_session.commit()
    assert len(dispatched) == 1
    assert dispatched[0][1] == second_workspace_id

    first_groups = (
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == first_workspace_id)))
        .scalars()
        .all()
    )
    assert first_groups[0].settings["generate_starter_doc"] is False
    second_groups = (
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == second_workspace_id)))
        .scalars()
        .all()
    )
    assert second_groups[0].settings["generate_starter_doc"] is True


@pytest.mark.asyncio
async def test_finalize_setup_does_not_create_unapproved_knowledge(db_session):
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroup
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    session = WorkspaceSetupSession(
        entity_id="ENTKNOWSKIP000000000000",
        fields={
            "name": "Knowledge Skip Workspace",
            "kind": "support_desk",
            "operating_context": "A test support desk",
            "primary_work": "Answer product questions",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "knowledge_attachments": [
                {
                    "name": "Unapproved FAQ",
                    "purpose": "Should not bind",
                    "mode": "create_new",
                    "approved": False,
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    groups = (
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()

    assert groups == []
    assert workspace.operating_model["knowledge"]["default_group_ids"] == []


@pytest.mark.asyncio
async def test_finalize_setup_materializes_runtime_governance_from_rules(db_session):
    from sqlalchemy import select
    from packages.core.models.governance import GovernancePolicy
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    session = WorkspaceSetupSession(
        entity_id="ENTGOVRULES000000000000",
        user_id="USERGOVRULES00000000000",
        fields={
            "name": "Governed Workspace",
            "kind": "campaign",
            "operating_context": "A social campaign",
            "primary_work": "Draft and publish social content",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "rules": [
                {
                    "rule_key": "review_social_posts",
                    "description": "发任何社媒 post 前，必须给用户审核完整内容，得到同意才能发布。",
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    policy = (
        await db_session.execute(select(GovernancePolicy).where(GovernancePolicy.workspace_id == workspace_id))
    ).scalar_one()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()

    assert policy.policy["hitl_required_actions"] == ["social_post.publish"]
    assert policy.updated_by == "USERGOVRULES00000000000"
    rule = workspace.operating_model["rules"][0]
    assert rule["runtime_enforced"] is True
    assert rule["rule_type"] == "approval_required"
    assert rule["action_patterns"] == ["social_post.publish"]

    from packages.core.workspace_chat import context as chat_context

    chat_context.invalidate(workspace_id)
    summary = await chat_context.get_summary(db_session, workspace_id, "ENTGOVRULES000000000000")
    assert "Runtime guardrails:" in summary
    assert "approval required for social_post.publish" in summary


@pytest.mark.asyncio
async def test_finalize_setup_does_not_link_cross_entity_existing_knowledge(db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import DocumentGroup
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    entity_a = generate_ulid()
    entity_b = generate_ulid()
    group_id = generate_ulid()
    db_session.add(
        DocumentGroup(
            id=group_id,
            entity_id=entity_b,
            name="Foreign Knowledge",
        )
    )
    await db_session.commit()

    session = WorkspaceSetupSession(
        entity_id=entity_a,
        fields={
            "name": "Knowledge Scope Workspace",
            "kind": "support_desk",
            "operating_context": "A test support desk",
            "primary_work": "Answer product questions",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "knowledge_attachments": [
                {
                    "name": "Foreign Knowledge",
                    "purpose": "Should not attach",
                    "mode": "link_existing",
                    "existing_group_id": group_id,
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    await finalize_setup(session, db_session)
    await db_session.commit()
    group = (await db_session.execute(select(DocumentGroup).where(DocumentGroup.id == group_id))).scalar_one()

    assert group.workspace_id is None


@pytest.mark.asyncio
async def test_finalize_setup_does_not_assign_cross_entity_staff(db_session):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.staff import Staff
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession, finalize_setup

    entity_a = generate_ulid()
    entity_b = generate_ulid()
    staff_id = generate_ulid()
    db_session.add(
        Staff(
            id=staff_id,
            entity_id=entity_b,
            name="Foreign Staff",
            status="active",
        )
    )
    await db_session.commit()

    session = WorkspaceSetupSession(
        entity_id=entity_a,
        fields={
            "name": "Staff Scope Workspace",
            "kind": "operations",
            "operating_context": "A test operations desk",
            "primary_work": "Coordinate support",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "staff_assignments": [
                {
                    "staff_id": staff_id,
                    "role": "reviewer",
                }
            ],
            "channel_config": {},
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    assignments = (
        (await db_session.execute(select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == workspace_id)))
        .scalars()
        .all()
    )

    assert assignments == []


def test_dedupe_rule_dicts_collapses_same_description_without_explicit_key():
    """B6: rules with identical descriptions but no rule_key used to slip
    through dedupe because the fallback key (rule_N) was always unique."""
    from packages.core.services.workspace_operation_service import _dedupe_rule_dicts

    rules = [
        {"description": "Avoid sharing customer PII in public channels."},
        {"description": "Avoid sharing customer PII in public channels."},
        {"rule_key": "tone", "description": "Stay polite."},
        {"rule_key": "tone", "description": "Stay polite, rephrased."},
        {"description": ""},
        {"description": ""},
    ]

    result = _dedupe_rule_dicts(rules)

    descriptions = [r.get("description") for r in result]
    keys = [r.get("rule_key") for r in result]
    # One PII rule, one tone rule (explicit key dedupe), two empty (no
    # dedupe possible without a description or key) — assigned fallback keys.
    assert descriptions.count("Avoid sharing customer PII in public channels.") == 1
    assert descriptions.count("Stay polite.") == 1
    assert keys.count("tone") == 1
    # Empty-description rules are NOT collapsed because there's nothing to
    # dedupe on; each one keeps a unique synthetic key so callers can edit
    # them individually rather than silently dropping data.
    assert descriptions.count("") == 2
    empty_keys = [r["rule_key"] for r in result if r["description"] == ""]
    assert len(empty_keys) == len(set(empty_keys))


@pytest.mark.asyncio
async def test_apply_operation_reuses_existing_auto_provisioned_agent(db_session):
    """B2: a second apply asking to provision a custom agent with the same
    name as one that already exists for the entity must reuse the row instead
    of creating a duplicate Agent."""
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.workspace_operation_service import (
        _sync_agent_mappings_from_operation_state,
    )

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Reuse Agent WS",
        kind="operations",
        operating_context="",
        primary_work="",
        operating_model={},
        status="active",
    )
    existing_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Leasing Concierge",
        source="auto_workspace_setup",
        status="active",
    )
    db_session.add_all([workspace, existing_agent])
    await db_session.flush()

    mappings = [
        {
            "service_key": "leasing",
            "strategy": "create_custom",
            "recommended_agent_name": "Leasing Concierge",
            "create_agent_draft": {"agent_name": "Leasing Concierge"},
        },
    ]
    result = await _sync_agent_mappings_from_operation_state(
        db_session,
        workspace,
        mappings,
        deactivate_missing=False,
    )

    assert result["custom_agents"] == 0
    assert result["reused_existing_agents"] == 1
    assert result["created"] == 1

    agent_rows = list(
        (
            await db_session.execute(
                select(Agent).where(
                    Agent.entity_id == entity_id,
                    Agent.name == "Leasing Concierge",
                    Agent.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(agent_rows) == 1
    assert agent_rows[0].id == existing_agent.id

    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.service_key == "leasing",
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()
    assert sub.agent_id == existing_agent.id


@pytest.mark.asyncio
async def test_workspace_rename_refreshes_subscription_framing(db_session):
    """B7: renaming the workspace (or changing primary_work /
    operating_context) must rewrite auto-generated subscription framings so
    the agent doesn't introduce itself with the old workspace name."""
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.entity_service import update_workspace

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Original Name",
        kind="operations",
        operating_context="serving early customers",
        primary_work="answer support tickets",
        operating_model={},
        status="active",
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Auto Agent",
        source="auto_workspace_setup",
        status="active",
    )
    db_session.add_all([workspace, agent])
    await db_session.flush()

    auto_framing_sub = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace.id,
        service_key="support",
        custom_prompt=(
            'You are subscribed to the "Original Name" workspace as the '
            '"support" service handler.\n\nWorkspace primary work: answer support tickets'
        ),
        status="active",
    )
    operator_written_sub = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace.id,
        service_key="ops",
        custom_prompt="Always reply in the user's language and keep tone neutral.",
        status="active",
    )
    db_session.add_all([auto_framing_sub, operator_written_sub])
    await db_session.flush()

    updated = await update_workspace(
        db_session,
        workspace.id,
        entity_id,
        name="New Name",
        primary_work="answer support tickets and onboard new users",
    )
    assert updated is not None

    refreshed_auto = (
        await db_session.execute(select(AgentSubscription).where(AgentSubscription.id == auto_framing_sub.id))
    ).scalar_one()
    refreshed_manual = (
        await db_session.execute(select(AgentSubscription).where(AgentSubscription.id == operator_written_sub.id))
    ).scalar_one()

    assert "New Name" in refreshed_auto.custom_prompt
    assert "Original Name" not in refreshed_auto.custom_prompt
    assert "onboard new users" in refreshed_auto.custom_prompt
    # Operator-written prompts must not be touched.
    assert refreshed_manual.custom_prompt == ("Always reply in the user's language and keep tone neutral.")
