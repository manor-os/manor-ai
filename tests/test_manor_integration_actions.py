from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_manor_send_email_uses_selected_integration_account(
    db_session,
    monkeypatch,
):
    from packages.core.ai.runtime.manor_actions import runtime_manor_send_email
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.services import channel_service

    entity_id = "ent_selected_email"
    first_account_id = generate_ulid()
    selected_account_id = generate_ulid()
    first_config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_type="email",
        provider="smtp",
        name="Support",
        config={"integration_id": first_account_id},
        credentials={},
        status="active",
    )
    selected_config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_type="email",
        provider="smtp",
        name="Sales",
        config={"integration_id": selected_account_id},
        credentials={},
        status="active",
    )
    db_session.add_all([first_config, selected_config])
    await db_session.commit()

    captured = {}

    async def fake_send_message(_db, _entity_id, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="message-selected-account",
            status="sent",
            error_message=None,
        )

    monkeypatch.setattr(channel_service, "send_message", fake_send_message)
    result = json.loads(await runtime_manor_send_email(
        db_session,
        entity_id=entity_id,
        params={
            "integration_account_id": selected_account_id,
            "to": "customer@example.com",
            "subject": "From Sales",
            "content": "Hello",
        },
    ))

    assert captured["channel_config_id"] == selected_config.id
    assert result["integration_account_id"] == selected_account_id
    assert result["channel_config_id"] == selected_config.id


@pytest.mark.asyncio
async def test_manor_send_email_channel_account_fans_out_per_recipient(
    db_session,
    monkeypatch,
):
    """Regression: the connected-channel branch must fan out one
    single-recipient send per address — never a stringified list To (Gmail
    555 5.5.2) — and aggregate into the same partial-success shape as the
    platform-SMTP path."""
    from packages.core.ai.runtime.manor_actions import runtime_manor_send_email
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig
    from packages.core.services import channel_service

    entity_id = "ent_channel_fanout"
    config = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_type="email",
        provider="smtp",
        name="Support",
        config={"integration_id": generate_ulid()},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    calls: list[dict] = []

    async def fake_send_message(_db, _entity_id, **kwargs):
        calls.append(kwargs)
        # One address refused → the batch must still deliver the rest.
        bad = kwargs["to_address"] == "bad@corp.com"
        return SimpleNamespace(
            id=f"log-{len(calls)}",
            status="failed" if bad else "sent",
            error_message="555 5.5.2 Syntax error" if bad else None,
        )

    monkeypatch.setattr(channel_service, "send_message", fake_send_message)

    result = json.loads(await runtime_manor_send_email(
        db_session,
        entity_id=entity_id,
        params={
            "to": ["a@corp.com", "bad@corp.com", "b@corp.com"],
            "subject": "Team update",
            "content": "Hello",
        },
    ))

    # One call per recipient, each with a single clean to_address.
    assert [c["to_address"] for c in calls] == ["a@corp.com", "bad@corp.com", "b@corp.com"]
    for c in calls:
        assert isinstance(c["to_address"], str)
        assert "," not in c["to_address"]
        assert "[" not in c["to_address"]

    # Aggregated partial-success shape, symmetric with platform SMTP.
    assert result["delivery"] == "channel_account"
    assert result["sent"] is True
    assert result["status"] == "partial"
    assert result["total"] == 3
    assert result["delivered"] == 2
    assert result["failed"] == [{"to": "bad@corp.com", "error": "555 5.5.2 Syntax error"}]
    assert result["to"] == ["a@corp.com", "bad@corp.com", "b@corp.com"]
    assert len(result["message_log_ids"]) == 3


@pytest.mark.asyncio
async def test_manor_lists_ready_integrations_for_current_user(client):
    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration

    entity_id = "ent_ready_integrations"
    user_id = "user_ready_integrations"

    telegram_account_id = generate_ulid()
    async with db_module.async_session() as db:
        db.add(
            Integration(
                id=telegram_account_id,
                entity_id=entity_id,
                provider="telegram",
                status="active",
                config={},
                credentials={"bot_token": "test-token"},
            )
        )
        db.add(
            Integration(
                id=generate_ulid(),
                entity_id=entity_id,
                provider="discord",
                status="active",
                config={},
                credentials={},
            )
        )
        await db.commit()

    result = json.loads(
        await _dispatch_action(
            "list_ready_integrations",
            {},
            entity_id,
            user_id=user_id,
        )
    )

    ready_mcp = {item["server_key"]: item for item in result["mcp_servers"]}
    configured = {item["provider"]: item for item in result["configured_integrations"]}

    assert result["ready_only"] is True
    assert ready_mcp["telegram"]["agent_can_use"] is True
    assert ready_mcp["telegram"]["scope"] == "entity"
    assert ready_mcp["telegram"]["default_account_id"] == telegram_account_id
    assert ready_mcp["telegram"]["account_options"] == [
        {
            "id": telegram_account_id,
            "display_name": f"telegram account {telegram_account_id[-6:]}",
            "scope": "entity",
            "is_default": False,
        }
    ]
    assert "discord" not in ready_mcp
    assert configured["telegram"]["ready"] is True
    assert "discord" not in configured


@pytest.mark.asyncio
async def test_manor_search_finds_ready_integration_action():
    from packages.core.ai.tools.manor_tool import _manor_handler

    result = json.loads(
        await _manor_handler(
            entity_id="ent_ready_integrations",
            action="search",
            query="ready integrations mcp connected usable",
        )
    )
    actions = {item["action"] for item in result["matches"]}

    assert "list_ready_integrations" in actions


def test_manor_merge_action_params_keeps_params_explicit():
    from packages.core.ai.tools.manor_tool import _merge_action_params

    params = _merge_action_params(
        {
            "action": "assign_task",
            "task_id": "top-level-task",
            "workspace_id": "context-workspace",
            "_active_user_message_from_context": "assign this to Simon",
            "_tool_profile_from_context": "workspace_agent",
            "_allowed_tool_names_from_context": ["manor"],
            "params": {
                "task_id": "nested-task",
                "assignee_email": "simon.assignment@test.com",
            },
        }
    )

    assert params == {
        "task_id": "nested-task",
        "assignee_email": "simon.assignment@test.com",
    }


def test_manor_tool_schema_documents_task_status_values():
    from packages.core.ai.tools.manor_tool import _search_actions
    from packages.core.constants.task import TASK_STATUSES

    schema_text = json.dumps(_search_actions("task status", max_results=8), ensure_ascii=False)

    assert "status='pending'" in schema_text
    assert "do not pass status='todo'" in schema_text
    for status in TASK_STATUSES:
        assert status in schema_text


@pytest.mark.asyncio
async def test_manor_update_task_changes_general_fields_and_category(client, db_session):
    from sqlalchemy import select
    from packages.core.ai.tools.manor_tool import _manor_handler
    from packages.core.models.task import Task
    from packages.core.models.user import User

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "manor_general_task_update",
            "email": "manor_general_task_update@test.com",
            "password": "pass123",
            "entity_name": "General Task Update Corp",
        },
    )
    auth = {"Authorization": f"Bearer {register.json()['access_token']}"}
    entity_id = register.json()["entity_id"]
    user = (
        await db_session.execute(select(User).where(User.email == "manor_general_task_update@test.com"))
    ).scalar_one()

    category_resp = await client.post(
        "/api/v1/tasks/categories",
        headers=auth,
        json={
            "name": "Operations",
            "color": "#0f8f84",
            "sort_order": 1,
        },
    )
    assert category_resp.status_code == 201
    category_id = category_resp.json()["id"]

    categories = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="list_task_categories",
        )
    )
    assert any(category["id"] == category_id for category in categories["categories"])

    created = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={"title": "General update target", "description": "Original"},
        )
    )

    updated = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="update_task",
            params={
                "task_id": created["id"],
                "status": "in_progress",
                "priority": "high",
                "category": "Operations",
                "description": "Updated by the general task tool",
                "scheduled_at": "2026-05-22T10:00:00+00:00",
                "duration_minutes": 45,
            },
        )
    )

    assert updated["updated"] is True
    assert updated["status"] == "in_progress"
    assert updated["priority"] == 4
    assert updated["category_id"] == category_id
    assert set(updated["updated_fields"]) >= {"status", "priority", "category_id", "description", "details"}

    task = (await db_session.execute(select(Task).where(Task.id == created["id"]))).scalar_one()
    assert task.status == "in_progress"
    assert task.priority == 4
    assert task.category_id == category_id
    assert task.description == "Updated by the general task tool"
    assert task.details["scheduled_at"] == "2026-05-22T10:00:00+00:00"
    assert task.details["duration_minutes"] == 45


@pytest.mark.asyncio
async def test_manor_create_task_can_create_independent_task(client, db_session):
    from sqlalchemy import select
    from packages.core.ai.tools.manor_tool import _manor_handler
    from packages.core.models.task import Task
    from packages.core.models.user import User

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "manor_independent_task",
            "email": "manor_independent_task@test.com",
            "password": "pass123",
            "entity_name": "Independent Task Corp",
        },
    )
    entity_id = register.json()["entity_id"]
    user = (await db_session.execute(select(User).where(User.email == "manor_independent_task@test.com"))).scalar_one()

    result = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={
                "title": "Prepare an independent research task",
                "description": "This task is not part of a workspace.",
            },
        )
    )

    task = (await db_session.execute(select(Task).where(Task.id == result["id"]))).scalar_one()
    assert result["status"] == "created"
    assert task.workspace_id is None
    assert task.title == "Prepare an independent research task"


@pytest.mark.asyncio
async def test_manor_create_task_does_not_fail_on_freeform_category(client, db_session):
    from sqlalchemy import select
    from packages.core.ai.tools.manor_tool import _manor_handler
    from packages.core.models.task import Task
    from packages.core.models.user import User

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "manor_freeform_category",
            "email": "manor_freeform_category@test.com",
            "password": "pass123",
            "entity_name": "Freeform Category Corp",
        },
    )
    entity_id = register.json()["entity_id"]
    user = (await db_session.execute(select(User).where(User.email == "manor_freeform_category@test.com"))).scalar_one()

    result = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={
                "title": "Add Codex CLI support",
                "category": "feature",
            },
        )
    )

    task = (await db_session.execute(select(Task).where(Task.id == result["id"]))).scalar_one()
    assert result["status"] == "created"
    assert result["category_warning"]["error"] == "category_not_found"
    assert task.task_type == "feature"
    assert task.category_id is None


@pytest.mark.asyncio
async def test_manor_task_assignment_resolves_staff_name_and_email(client, db_session):
    from sqlalchemy import select
    from packages.core.ai.tools.manor_tool import _manor_handler
    from packages.core.models.task import Task
    from packages.core.models.user import User

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "manor_staff_assignment",
            "email": "manor_staff_assignment@test.com",
            "password": "pass123",
            "entity_name": "Staff Assignment Corp",
        },
    )
    auth = {"Authorization": f"Bearer {register.json()['access_token']}"}
    entity_id = register.json()["entity_id"]
    user = (await db_session.execute(select(User).where(User.email == "manor_staff_assignment@test.com"))).scalar_one()

    staff_resp = await client.post(
        "/api/v1/staff",
        headers=auth,
        json={
            "name": "Simon",
            "email": "simon.assignment@test.com",
        },
    )
    assert staff_resp.status_code == 201
    staff_id = staff_resp.json()["id"]

    created = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={
                "title": "Prepare Simon's briefing",
                "staff_name": "Simon",
            },
        )
    )
    task = (await db_session.execute(select(Task).where(Task.id == created["id"]))).scalar_one()
    assert created["assigned"] is True
    assert created["assignee_name"] == "Simon"
    assert created["staff_id"] == staff_id
    assert task.assignee_id == staff_id

    unassigned = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={"title": "Assign after creation"},
        )
    )
    assigned = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="assign_task",
            params={
                "task_id": unassigned["id"],
                "assignee_email": "simon.assignment@test.com",
            },
        )
    )
    assert assigned["assigned"] is True
    assert assigned["assignee_name"] == "Simon"
    assert assigned["staff_id"] == staff_id

    unassigned_direct = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={"title": "Assign from direct action args"},
        )
    )
    assigned_direct = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="assign_task",
            task_id=unassigned_direct["id"],
            assignee_email="simon.assignment@test.com",
        )
    )
    assert assigned_direct["assigned"] is True
    assert assigned_direct["assignee_name"] == "Simon"
    assert assigned_direct["staff_id"] == staff_id


@pytest.mark.asyncio
async def test_manor_assign_task_reports_required_assignee(client, db_session):
    from packages.core.ai.tools.manor_tool import _manor_handler
    from sqlalchemy import select
    from packages.core.models.user import User

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "manor_assignment_required",
            "email": "manor_assignment_required@test.com",
            "password": "pass123",
            "entity_name": "Assignment Required Corp",
        },
    )
    entity_id = register.json()["entity_id"]
    user = (
        await db_session.execute(select(User).where(User.email == "manor_assignment_required@test.com"))
    ).scalar_one()

    created = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="create_task",
            params={"title": "Needs an assignee"},
        )
    )

    result = json.loads(
        await _manor_handler(
            entity_id=entity_id,
            user_id=user.id,
            action="assign_task",
            params={"task_id": created["id"]},
        )
    )

    assert result["error"] == "assignee_required"
    assert any("assignee_id" in item for item in result["required_params"])
