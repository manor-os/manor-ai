"""E2E tests: dashboard analytics — stats, task trends, usage trends, activity."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.ai.runtime.dashboard_submission import (
    runtime_record_dashboard_submission,
)
from packages.core.ai.tools.code_tool import _code_handler
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Task
from packages.core.models.usage import TokenUsageLog
from packages.core.services.conversation_messages import add_message
from packages.core.services.dashboard_agent import DashboardAgentTurnResult


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


def _generated_code(
    *,
    source: str,
    key: str,
    params: dict | None = None,
) -> dict:
    return {
        "version": 1,
        "runtime": "sandboxed_html",
        "html": '<div data-items></div>',
        "css": "[data-items]{display:grid;gap:8px;color:var(--module-text);border-color:var(--module-border)}",
        "javascript": (
            "window.renderDashboardModule = function(data) { "
            "document.querySelector('[data-items]').textContent = "
            f"JSON.stringify(data.{key} || []); }};"
        ),
        "data_requests": [
            {
                "key": key,
                "source": source,
                "params": params or {},
            }
        ],
    }


async def _dashboard_agent_result(
    db,
    user,
    submission: dict | None,
    *,
    assistant_message: str = "Dashboard preview is ready.",
    tool_calls: list[str] | None = None,
) -> DashboardAgentTurnResult:
    conversation_id = generate_ulid()
    db.add(
        Conversation(
            id=conversation_id,
            entity_id=user.entity_id,
            user_id=user.id,
            title="Dashboard module test",
            channel="web",
            status="active",
            scope="channel",
            meta={"surface": "dashboard_module"},
        )
    )
    await db.flush()
    return DashboardAgentTurnResult(
        conversation_id=conversation_id,
        assistant_message=assistant_message,
        submission=submission,
        tool_calls=tool_calls or [],
        hitl_requests=[],
    )


def test_dashboard_create_deduplicates_an_equivalent_module_title():
    from apps.api.routers.dashboard import _merge_dashboard_layout_suggestion

    existing_conversation_id = generate_ulid()
    replacement_conversation_id = generate_ulid()
    current = {
        "version": 2,
        "widgets": [{"id": "daily_brief", "visible": True}],
        "modules": [
            {
                "id": "module_existing_news",
                "title": "Daily AI News",
                "description": "Old news module",
                "visible": True,
                "size": "compact",
                "conversation_id": existing_conversation_id,
                "code": _generated_code(source="news", key="old_news"),
            }
        ],
    }

    merged = _merge_dashboard_layout_suggestion(
        {
            "widgets": current["widgets"],
            "module_changes": [
                {
                    "action": "create",
                    "title": "daily ai news",
                    "description": "Fresh news module",
                    "visible": True,
                    "size": "wide",
                    "code": _generated_code(source="news", key="fresh_news"),
                }
            ],
        },
        current,
        conversation_id=replacement_conversation_id,
    )

    assert len(merged["modules"]) == 1
    assert merged["modules"][0]["id"] == "module_existing_news"
    assert merged["modules"][0]["description"] == "Fresh news module"
    assert merged["modules"][0]["size"] == "wide"
    assert merged["modules"][0]["conversation_id"] == replacement_conversation_id
    assert merged["modules"][0]["code"]["data_requests"][0]["key"] == "fresh_news"


@pytest.mark.asyncio
async def test_dashboard_layout_defaults_and_persists(client: AsyncClient):
    headers = await _auth(client, "dashlayout")

    default_response = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert default_response.status_code == 200
    defaults = default_response.json()
    assert defaults["version"] == 2
    assert defaults["modules"] == []
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
async def test_generated_dashboard_modules_are_private_to_each_user(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token

    owner_headers = await _auth(client, "dashprivateowner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    teammate = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="dash-private-teammate@test.com",
        password_hash="unused",
        role="member",
        status="active",
        display_name="Dashboard Teammate",
    )
    db_session.add(teammate)
    await db_session.commit()
    teammate_headers = {
        "Authorization": (
            f"Bearer {create_access_token(teammate.id, teammate.entity_id, teammate.role)}"
        )
    }

    defaults = (await client.get("/api/v1/dashboard/layout", headers=owner_headers)).json()
    owner_module = {
        "id": "module_private_owner",
        "title": "Private owner module",
        "visible": True,
        "size": "wide",
        "code": _generated_code(source="tasks", key="owner_tasks"),
    }
    saved = await client.put(
        "/api/v1/dashboard/layout",
        headers=owner_headers,
        json={"widgets": defaults["widgets"], "modules": [owner_module]},
    )
    assert saved.status_code == 200

    teammate_layout = await client.get(
        "/api/v1/dashboard/layout",
        headers=teammate_headers,
    )
    assert teammate_layout.status_code == 200
    assert teammate_layout.json()["modules"] == []

    owner_reloaded = await client.get("/api/v1/dashboard/layout", headers=owner_headers)
    assert [module["id"] for module in owner_reloaded.json()["modules"]] == [
        "module_private_owner"
    ]


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
async def test_dashboard_layout_rejects_unsafe_generated_code(client: AsyncClient):
    headers = await _auth(client, "dashlayoutunsafe")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()
    unsafe_code = _generated_code(source="news", key="headlines")
    unsafe_code["javascript"] = (
        "window.renderDashboardModule = function(data) { "
        "fetch('https://example.com/collect'); };"
    )

    response = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={
            "widgets": defaults["widgets"],
            "modules": [
                {
                    "id": "module_unsafe_code",
                    "title": "Unsafe module",
                    "visible": True,
                    "size": "wide",
                    "code": unsafe_code,
                }
            ],
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_layout_ai_suggestion_updates_preview_only(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": "total_tasks", "visible": True},
                    {"id": "workspaces", "visible": True},
                    {"id": "task_trend", "visible": True},
                    {"id": "daily_brief", "visible": False},
                    {"id": "time_saved", "visible": False},
                    {"id": "tasks_running", "visible": False},
                    {"id": "activity", "visible": False},
                ],
                "module_changes": [],
            },
        )

    monkeypatch.setattr(
        dashboard_router,
        "run_dashboard_agent_turn",
        fake_agent,
    )
    headers = await _auth(client, "dashlayoutai")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    response = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={
            "prompt": "Only show tasks, workspaces, and trend",
            "widgets": defaults["widgets"],
        },
    )
    assert response.status_code == 200
    suggestion = response.json()["widgets"]
    assert [widget["id"] for widget in suggestion[:3]] == [
        "total_tasks",
        "workspaces",
        "task_trend",
    ]
    assert [widget["id"] for widget in suggestion if widget["visible"]] == [
        "total_tasks",
        "workspaces",
        "task_trend",
    ]

    persisted = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert persisted.json() == defaults

    blank = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={"prompt": "   ", "widgets": defaults["widgets"]},
    )
    assert blank.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_ai_generated_module_previews_then_persists(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": widget_id, "visible": True}
                    for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
                ],
                "module_changes": [
                    {
                        "action": "create",
                        "title": "Failed tasks by workspace",
                        "description": "Failed tasks created in the last 14 days",
                        "size": "wide",
                        "code": _generated_code(
                            source="tasks",
                            key="failed_tasks",
                            params={"statuses": ["failed"], "days": 14, "limit": 8},
                        ),
                    }
                ],
            },
        )

    monkeypatch.setattr(
        dashboard_router,
        "run_dashboard_agent_turn",
        fake_agent,
    )
    headers = await _auth(client, "dashmoduleai")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    preview_response = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={
            "prompt": "Show failed tasks grouped by workspace for the last 14 days",
            "widgets": defaults["widgets"],
            "modules": defaults["modules"],
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["version"] == 2
    assert preview["preview_created"] is True
    assert preview["conversation_id"] == preview["modules"][0]["conversation_id"]
    assert len(preview["modules"]) == 1
    assert preview["modules"][0]["id"].startswith("module_")
    assert preview["modules"][0]["code"]["runtime"] == "sandboxed_html"
    assert preview["modules"][0]["code"]["data_requests"] == [
        {
            "key": "failed_tasks",
            "source": "tasks",
            "params": {"statuses": ["failed"], "days": 14, "limit": 8},
        }
    ]

    persisted_before_save = await client.get(
        "/api/v1/dashboard/layout",
        headers=headers,
    )
    assert persisted_before_save.json()["modules"] == []

    save_response = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={"widgets": preview["widgets"], "modules": preview["modules"]},
    )
    assert save_response.status_code == 200

    persisted_after_save = await client.get(
        "/api/v1/dashboard/layout",
        headers=headers,
    )
    assert persisted_after_save.json()["widgets"] == preview["widgets"]
    assert persisted_after_save.json()["modules"] == preview["modules"]


@pytest.mark.asyncio
async def test_dashboard_ai_conversation_updates_only_the_target_module(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    captured: dict = {}
    updated_code = _generated_code(
        source="tasks",
        key="edited_tasks",
        params={"statuses": ["failed"], "limit": 4},
    )

    async def fake_agent(db, *, user, message, system_prompt, **_kwargs):
        captured["message"] = message
        captured["system_prompt"] = system_prompt
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": widget_id, "visible": False}
                    for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
                ],
                "module_changes": [
                    {
                        "action": "update",
                        "id": "module_edit_target",
                        "title": "Failed tasks, compact",
                        "description": "Shows four failed tasks",
                        "size": "compact",
                        "code": updated_code,
                    }
                ],
                "assistant_message": "Made the module compact and limited it to four failed tasks.",
            },
            assistant_message=(
                "Made the module compact and limited it to four failed tasks."
            ),
        )

    monkeypatch.setattr(
        dashboard_router,
        "run_dashboard_agent_turn",
        fake_agent,
    )
    headers = await _auth(client, "dashmoduleconversation")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()
    original_target = {
        "id": "module_edit_target",
        "title": "All tasks",
        "visible": True,
        "size": "wide",
        "code": _generated_code(source="tasks", key="all_tasks"),
    }
    untouched_module = {
        "id": "module_untouched",
        "title": "Workspace list",
        "visible": True,
        "size": "wide",
        "code": _generated_code(source="workspaces", key="workspace_items"),
    }

    response = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={
            "prompt": "Make it compact and show only four failures",
            "widgets": defaults["widgets"],
            "modules": [original_target, untouched_module],
            "target_module_id": "module_edit_target",
            "conversation": [
                {"role": "user", "content": "Use fewer rows"},
                {"role": "assistant", "content": "I can make it more compact."},
            ],
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["assistant_message"].startswith("Made the module compact")
    assert result["changed_module_id"] == "module_edit_target"
    assert result["widgets"] == defaults["widgets"]
    assert result["modules"][0]["title"] == "Failed tasks, compact"
    assert result["modules"][0]["code"] == updated_code
    assert result["modules"][1]["id"] == untouched_module["id"]
    assert result["modules"][1]["code"] == untouched_module["code"]
    assert captured["message"] == "Make it compact and show only four failures"
    assert "module_edit_target" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_dashboard_ai_news_module_previews_then_persists(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": widget_id, "visible": True}
                    for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
                ],
                "module_changes": [
                    {
                        "action": "create",
                        "title": "每日新闻",
                        "description": "最近 24 小时的最新报道",
                        "size": "wide",
                        "code": _generated_code(
                            source="news",
                            key="headlines",
                            params={"query": None, "days": 1, "limit": 8},
                        ),
                    }
                ],
            },
        )

    monkeypatch.setattr(
        dashboard_router,
        "run_dashboard_agent_turn",
        fake_agent,
    )
    headers = await _auth(client, "dashnewsmodule")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    preview_response = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={
            "prompt": "加每日新闻",
            "widgets": defaults["widgets"],
            "modules": defaults["modules"],
        },
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert len(preview["modules"]) == 1
    assert preview["modules"][0]["title"] == "每日新闻"
    assert preview["modules"][0]["code"]["runtime"] == "sandboxed_html"
    assert "renderDashboardModule" in preview["modules"][0]["code"]["javascript"]
    assert preview["modules"][0]["code"]["data_requests"] == [
        {
            "key": "headlines",
            "source": "news",
            "params": {"query": None, "days": 1, "limit": 8},
        }
    ]

    saved = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={"widgets": preview["widgets"], "modules": preview["modules"]},
    )
    assert saved.status_code == 200
    reloaded = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert reloaded.json()["widgets"] == preview["widgets"]
    assert reloaded.json()["modules"] == preview["modules"]


@pytest.mark.asyncio
async def test_dashboard_ai_stock_module_uses_live_quote_source(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": widget_id, "visible": True}
                    for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
                ],
                "module_changes": [
                    {
                        "action": "create",
                        "title": "Live stock prices",
                        "description": "NVIDIA and SanDisk, refreshed every 15 seconds",
                        "size": "wide",
                        "code": _generated_code(
                            source="stocks",
                            key="quotes",
                            params={
                                "symbols": ["NVDA", "SNDK"],
                                "refresh_seconds": 15,
                            },
                        ),
                    }
                ],
            },
        )

    monkeypatch.setattr(
        dashboard_router,
        "run_dashboard_agent_turn",
        fake_agent,
    )
    headers = await _auth(client, "dashstockmodule")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    response = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={
            "prompt": "show nvidia and sandix stock real time price",
            "widgets": defaults["widgets"],
            "modules": defaults["modules"],
        },
    )

    assert response.status_code == 200
    module = response.json()["modules"][0]
    assert module["title"] == "Live stock prices"
    assert module["code"]["data_requests"] == [
        {
            "key": "quotes",
            "source": "stocks",
            "params": {"symbols": ["NVDA", "SNDK"], "refresh_seconds": 15},
        }
    ]


@pytest.mark.asyncio
async def test_dashboard_generation_uses_persistent_manor_agent_conversation(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import dashboard_agent as dashboard_agent_service

    captured: dict = {}
    generated = {
        "widgets": [
            {"id": widget_id, "visible": True}
            for widget_id in (
                "daily_brief",
                "time_saved",
                "total_tasks",
                "tasks_running",
                "activity",
                "workspaces",
                "task_trend",
            )
        ],
        "module_changes": [
            {
                "action": "create",
                "title": "Connected research",
                "size": "wide",
                "code": _generated_code(source="news", key="research"),
            }
        ],
    }

    async def fake_runtime(message, conversation_id, **kwargs):
        captured.update(
            {
                "message": message,
                "conversation_id": conversation_id,
                "runtime_metadata": kwargs["runtime_metadata"],
                "manual_skill_refs": kwargs["manual_skill_refs"],
            }
        )
        validation = json.loads(
            await _code_handler(
                action="dashboard_module_validate",
                params={"code": generated["module_changes"][0]["code"]},
                _manual_skill_slugs_from_context=["dashboard-module-builder"],
            )
        )
        assert validation["platform_ready"] is True
        runtime_record_dashboard_submission(generated)
        await add_message(
            kwargs["db"],
            conversation_id,
            role="assistant",
            content="I researched the source and generated the module.",
            tool_calls=[
                {"name": "web_search", "arguments": {"query": "latest AI"}},
                {"name": "code", "arguments": {"action": "dashboard_module_validate"}},
                {"name": "dashboard_submit_module", "arguments": {}},
            ],
        )
        await kwargs["db"].commit()
        return {
            "conversation_id": conversation_id,
            "content": "I researched the source and generated the module.",
            "tool_calls_made": ["web_search", "code", "dashboard_submit_module"],
            "hitl_requests": [],
        }

    monkeypatch.setattr(
        dashboard_agent_service,
        "runtime_run_chat_turn",
        fake_runtime,
    )
    monkeypatch.setattr(
        dashboard_agent_service,
        "dashboard_blocked_tool_names",
        lambda: (),
    )
    headers = await _auth(client, "dashagentconversation")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    preview_response = await client.post(
        "/api/v1/dashboard/layout/suggest",
        headers=headers,
        json={
            "prompt": "Research the latest AI news and build a module",
            "widgets": defaults["widgets"],
            "modules": [],
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["preview_created"] is True
    assert preview["tool_calls"] == ["web_search", "code"]
    assert preview["conversation_id"] == captured["conversation_id"]
    assert captured["runtime_metadata"]["extra_tool_names"] == [
        "dashboard_submit_module"
    ]
    selected_skill = captured["manual_skill_refs"][0]
    assert selected_skill["display_name"] == "Dashboard Module Builder"
    assert len(selected_skill["id"]) == 26
    assert "slug" not in selected_skill
    assert "Research the latest AI news" in selected_skill["input"]
    assert '"current_modules":[]' in selected_skill["input"]

    module = preview["modules"][0]
    saved = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={"widgets": preview["widgets"], "modules": [module]},
    )
    assert saved.status_code == 200
    history = await client.get(
        f"/api/v1/dashboard/modules/{module['id']}/conversation",
        headers=headers,
    )
    assert history.status_code == 200
    assert [item["role"] for item in history.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert history.json()["messages"][1]["tool_calls"] == ["web_search", "code"]


@pytest.mark.asyncio
async def test_dashboard_tool_data_broker_is_read_only_and_cached(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    cache_data: dict = {}
    executions: list[dict] = []

    async def fake_cache_get(key):
        return cache_data.get(key)

    async def fake_cache_set(key, value, ttl):
        cache_data[key] = value
        cache_data[f"{key}:ttl"] = ttl

    async def fake_execute(name, args, **kwargs):
        executions.append({"name": name, "args": args, "kwargs": kwargs})
        return json.dumps({"items": [{"title": "Result"}]})

    monkeypatch.setattr(dashboard_router.cache, "get", fake_cache_get)
    monkeypatch.setattr(dashboard_router.cache, "set", fake_cache_set)
    monkeypatch.setattr(
        dashboard_router,
        "runtime_tool_schema",
        lambda _name: {"type": "function"},
    )
    monkeypatch.setattr(dashboard_router, "runtime_execute_tool", fake_execute)
    headers = await _auth(client, "dashtoolbroker")
    request = {
        "tool_name": "web_search",
        "arguments": {"query": "AI reliability"},
        "refresh_seconds": 120,
    }

    first = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=headers,
        json=request,
    )
    second = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=headers,
        json=request,
    )
    assert first.status_code == 200
    assert first.json()["result"]["items"][0]["title"] == "Result"
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert len(executions) == 1
    assert executions[0]["kwargs"]["allowed_tool_names"] == {"web_search"}

    email_read = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=headers,
        json={
            "tool_name": "mcp__gmail__list_messages",
            "arguments": {
                "query": "is:unread newer_than:7d",
                "max_results": 8,
                "include_details": True,
            },
            "refresh_seconds": 120,
        },
    )
    assert email_read.status_code == 200
    assert len(executions) == 2
    assert executions[1]["name"] == "mcp__gmail__list_messages"
    assert executions[1]["kwargs"]["allowed_tool_names"] == {"mcp__gmail__list_messages"}

    blocked = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=headers,
        json={
            "tool_name": "mcp__gmail__send_email",
            "arguments": {"to": "somebody@example.com", "body": "No"},
        },
    )
    assert blocked.status_code == 403
    assert len(executions) == 2


@pytest.mark.asyncio
async def test_dashboard_http_data_broker_fetches_and_caches_public_json(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    cache_data: dict = {}
    fetched_urls: list[str] = []

    async def fake_cache_get(key):
        return cache_data.get(key)

    async def fake_cache_set(key, value, ttl):
        cache_data[key] = value
        cache_data[f"{key}:ttl"] = ttl

    async def fake_get_json(url):
        fetched_urls.append(url)
        return {"current": {"temperature": 21}, "forecast": [{"high": 26}]}

    monkeypatch.setattr(dashboard_router.cache, "get", fake_cache_get)
    monkeypatch.setattr(dashboard_router.cache, "set", fake_cache_set)
    monkeypatch.setattr(dashboard_router, "get_dashboard_http_json", fake_get_json)
    headers = await _auth(client, "dashhttpbroker")
    request = {
        "url": "https://api.example.com/v1/live?location=seattle",
        "refresh_seconds": 180,
    }

    first = await client.post(
        "/api/v1/dashboard/http-data",
        headers=headers,
        json=request,
    )
    second = await client.post(
        "/api/v1/dashboard/http-data",
        headers=headers,
        json=request,
    )

    assert first.status_code == 200
    assert first.json()["result"]["current"]["temperature"] == 21
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert fetched_urls == [request["url"]]

    blocked = await client.post(
        "/api/v1/dashboard/http-data",
        headers=headers,
        json={"url": "https://127.0.0.1/private", "refresh_seconds": 180},
    )
    assert blocked.status_code == 422
    assert fetched_urls == [request["url"]]


@pytest.mark.asyncio
async def test_dashboard_module_tool_requests_reject_mutations(client: AsyncClient):
    headers = await _auth(client, "dashtoolrequest")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()
    safe_code = _generated_code(source="news", key="connected_data")
    safe_code["data_requests"] = [
        {
            "key": "connected_data",
            "source": "tool",
            "params": {},
            "tool_name": "web_search",
            "tool_arguments": {"query": "AI reliability"},
            "refresh_seconds": 300,
        }
    ]
    safe = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={
            "widgets": defaults["widgets"],
            "modules": [
                {
                    "id": "module_safe_tool",
                    "title": "Safe tool data",
                    "visible": True,
                    "size": "wide",
                    "code": safe_code,
                }
            ],
        },
    )
    assert safe.status_code == 200

    unsafe_code = {**safe_code}
    unsafe_code["data_requests"] = [
        {
            "key": "connected_data",
            "source": "tool",
            "params": {},
            "tool_name": "mcp__gmail__send_email",
            "tool_arguments": {"to": "somebody@example.com"},
            "refresh_seconds": 300,
        }
    ]
    unsafe = await client.put(
        "/api/v1/dashboard/layout",
        headers=headers,
        json={
            "widgets": defaults["widgets"],
            "modules": [
                {
                    "id": "module_unsafe_tool",
                    "title": "Unsafe tool data",
                    "visible": True,
                    "size": "wide",
                    "code": unsafe_code,
                }
            ],
        },
    )
    assert unsafe.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_module_conversation_is_private_and_deleted_with_module(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token

    owner_headers = await _auth(client, "dashconversationowner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            title="Private dashboard conversation",
            channel="web",
            status="active",
            scope="channel",
            meta={"surface": "dashboard_module"},
        )
    )
    await add_message(
        db_session,
        conversation_id,
        role="user",
        content="Show my private data",
    )
    teammate = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="dashboard-conversation-teammate@test.com",
        password_hash="unused",
        role="member",
        status="active",
        display_name="Dashboard Conversation Teammate",
    )
    db_session.add(teammate)
    await db_session.commit()
    teammate_headers = {
        "Authorization": (
            f"Bearer {create_access_token(teammate.id, teammate.entity_id, teammate.role)}"
        )
    }
    defaults = (await client.get("/api/v1/dashboard/layout", headers=owner_headers)).json()
    module = {
        "id": "module_private_conversation",
        "title": "Private conversation",
        "visible": True,
        "size": "wide",
        "conversation_id": conversation_id,
        "code": _generated_code(source="tasks", key="private_tasks"),
    }
    saved = await client.put(
        "/api/v1/dashboard/layout",
        headers=owner_headers,
        json={"widgets": defaults["widgets"], "modules": [module]},
    )
    assert saved.status_code == 200

    owner_history = await client.get(
        "/api/v1/dashboard/modules/module_private_conversation/conversation",
        headers=owner_headers,
    )
    teammate_history = await client.get(
        "/api/v1/dashboard/modules/module_private_conversation/conversation",
        headers=teammate_headers,
    )
    assert owner_history.status_code == 200
    assert owner_history.json()["messages"][0]["content"] == "Show my private data"
    assert teammate_history.status_code == 404

    removed = await client.put(
        "/api/v1/dashboard/layout",
        headers=owner_headers,
        json={"widgets": defaults["widgets"], "modules": []},
    )
    assert removed.status_code == 200
    deleted = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    assert deleted is None


@pytest.mark.asyncio
async def test_dashboard_stocks_uses_requested_symbols(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    captured: dict = {}

    async def fake_stock_quotes(**kwargs):
        captured.update(kwargs)
        return [
            {
                "symbol": "NVDA",
                "price": 183.72,
                "change": 2.41,
                "change_percent": 1.33,
                "currency": "USD",
                "updated_at": "2026-07-12T09:00:00+00:00",
                "status": "ok",
                "provider": "Finnhub",
            }
        ]

    monkeypatch.setattr(dashboard_router, "get_dashboard_stock_quotes", fake_stock_quotes)
    headers = await _auth(client, "dashstockfeed")

    response = await client.get(
        "/api/v1/dashboard/stocks?symbols=NVDA,SNDK",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()[0]["symbol"] == "NVDA"
    assert captured == {"symbols": ["NVDA", "SNDK"]}


@pytest.mark.asyncio
async def test_dashboard_news_uses_user_locale_and_requested_filters(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    captured: dict = {}

    async def fake_news(**kwargs):
        captured.update(kwargs)
        return [
            {
                "id": "article-1",
                "title": "Agent reliability improves",
                "url": "https://example.com/article-1",
                "source": "Example News",
                "published_at": "2026-07-12T08:00:00+00:00",
                "language": "English",
            }
        ]

    monkeypatch.setattr(dashboard_router, "get_dashboard_news", fake_news)
    headers = await _auth(client, "dashnewsfeed")

    response = await client.get(
        "/api/v1/dashboard/news?query=AI%20agents&days=2&limit=3",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Agent reliability improves"
    assert captured == {
        "query": "AI agents",
        "days": 2,
        "limit": 3,
        "locale": "en",
    }


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

    future_since = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    resp = await client.get(
        "/api/v1/dashboard/recent-activity",
        headers=headers,
        params={"limit": 10, "since": future_since},
    )
    assert resp.status_code == 200
    assert resp.json() == []


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


# ── Async generation jobs ──


async def _poll_dashboard_job(
    client: AsyncClient,
    headers: dict,
    job_id: str,
    *,
    attempts: int = 200,
) -> dict:
    import asyncio

    for _ in range(attempts):
        resp = await client.get(
            f"/api/v1/dashboard/layout/suggest/jobs/{job_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("Dashboard generation job did not finish in time")


@pytest.mark.asyncio
async def test_dashboard_generation_job_succeeds_and_returns_preview(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": widget_id, "visible": True}
                    for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
                ],
                "module_changes": [
                    {
                        "action": "create",
                        "title": "Failed tasks",
                        "description": "Recent failed tasks",
                        "size": "wide",
                        "code": _generated_code(
                            source="tasks",
                            key="failed_tasks",
                            params={"statuses": ["failed"], "limit": 8},
                        ),
                    }
                ],
            },
        )

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", fake_agent)
    headers = await _auth(client, "dashjobok")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    start = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json={
            "prompt": "Show recent failed tasks",
            "widgets": defaults["widgets"],
            "modules": defaults["modules"],
        },
    )
    assert start.status_code == 200
    job = start.json()
    assert job["status"] == "running"
    assert job["job_id"].startswith("dashjob_")

    finished = await _poll_dashboard_job(client, headers, job["job_id"])
    assert finished["status"] == "succeeded"
    result = finished["result"]
    assert result["preview_created"] is True
    assert result["conversation_id"]
    titles = [module["title"] for module in result["modules"]]
    assert "Failed tasks" in titles

    blank = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json={"prompt": "   ", "widgets": defaults["widgets"]},
    )
    assert blank.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_generation_job_reports_agent_failure(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def failing_agent(db, *, user, **_kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", failing_agent)
    headers = await _auth(client, "dashjobfail")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    start = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json={"prompt": "Break please", "widgets": defaults["widgets"]},
    )
    assert start.status_code == 200

    finished = await _poll_dashboard_job(client, headers, start.json()["job_id"])
    assert finished["status"] == "failed"
    assert finished["error_code"] == "agent_error"
    assert finished["error"] == "Could not interpret dashboard request"
    assert finished.get("result") in (None,)


@pytest.mark.asyncio
async def test_dashboard_generation_job_cancel_stops_the_run(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio

    from apps.api.routers import dashboard as dashboard_router

    started = asyncio.Event()

    async def slow_agent(db, *, user, **_kwargs):
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("cancelled agent should never complete")

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", slow_agent)
    headers = await _auth(client, "dashjobcancel")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    start = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json={"prompt": "Take forever", "widgets": defaults["widgets"]},
    )
    assert start.status_code == 200
    job_id = start.json()["job_id"]
    await asyncio.wait_for(started.wait(), timeout=5)

    cancelled = await client.post(
        f"/api/v1/dashboard/layout/suggest/jobs/{job_id}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    body = cancelled.json()
    if body["status"] == "running":
        body = await _poll_dashboard_job(client, headers, job_id)
    assert body["status"] == "cancelled"
    assert body["error_code"] == "cancelled"


@pytest.mark.asyncio
async def test_dashboard_generation_job_is_private_to_its_user(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(db, user, None)

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", fake_agent)
    owner_headers = await _auth(client, "dashjobowner")
    defaults = (
        await client.get("/api/v1/dashboard/layout", headers=owner_headers)
    ).json()

    start = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=owner_headers,
        json={"prompt": "Anything", "widgets": defaults["widgets"]},
    )
    job_id = start.json()["job_id"]

    intruder_headers = await _auth(client, "dashjobintruder")
    peek = await client.get(
        f"/api/v1/dashboard/layout/suggest/jobs/{job_id}",
        headers=intruder_headers,
    )
    assert peek.status_code == 404
    cancel = await client.post(
        f"/api/v1/dashboard/layout/suggest/jobs/{job_id}/cancel",
        headers=intruder_headers,
    )
    assert cancel.status_code == 404

    finished = await _poll_dashboard_job(client, owner_headers, job_id)
    assert finished["status"] == "succeeded"
    assert finished["result"]["preview_created"] is False


@pytest.mark.asyncio
async def test_dashboard_generation_jobs_run_concurrently_with_deltas(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two dashboard-level generations run in parallel; each returns its own
    module as a delta so the client can merge them without clobbering."""
    import asyncio

    from apps.api.routers import dashboard as dashboard_router

    release = asyncio.Event()
    started_prompts: list[str] = []

    async def gated_agent(db, *, user, message, **_kwargs):
        started_prompts.append(message)
        await release.wait()
        title = f"Module for {message}"
        return await _dashboard_agent_result(
            db,
            user,
            {
                "widgets": [
                    {"id": widget_id, "visible": True}
                    for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
                ],
                "module_changes": [
                    {
                        "action": "create",
                        "title": title,
                        "description": "generated",
                        "size": "compact",
                        "code": _generated_code(
                            source="tasks",
                            key="items",
                            params={"limit": 5},
                        ),
                    }
                ],
            },
        )

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", gated_agent)
    headers = await _auth(client, "dashjobpair")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    job_ids = []
    for prompt in ("first news", "second stocks"):
        resp = await client.post(
            "/api/v1/dashboard/layout/suggest/jobs",
            headers=headers,
            json={
                "prompt": prompt,
                "widgets": defaults["widgets"],
                "modules": defaults["modules"],
            },
        )
        assert resp.status_code == 200
        job_ids.append(resp.json()["job_id"])

    for _ in range(200):
        if len(started_prompts) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(started_prompts) == 2, "both jobs should run concurrently"
    release.set()

    results = [
        await _poll_dashboard_job(client, headers, job_id) for job_id in job_ids
    ]
    assert all(item["status"] == "succeeded" for item in results)
    for item in results:
        changes = item["result"]["applied_module_changes"]
        assert len(changes) == 1
        assert changes[0]["action"] == "create"
        assert changes[0]["module"]["title"].startswith("Module for ")


@pytest.mark.asyncio
async def test_dashboard_generation_enforces_concurrency_and_target_mutex(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio

    from apps.api.routers import dashboard as dashboard_router

    release = asyncio.Event()

    async def gated_agent(db, *, user, **_kwargs):
        await release.wait()
        return await _dashboard_agent_result(db, user, None)

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", gated_agent)
    headers = await _auth(client, "dashjoblimit")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    module = {
        "id": "module_target_one",
        "title": "Target module",
        "description": None,
        "visible": True,
        "size": "compact",
        "conversation_id": None,
        "code": _generated_code(source="tasks", key="items"),
    }

    def payload(prompt: str, target: str | None = None) -> dict:
        body = {
            "prompt": prompt,
            "widgets": defaults["widgets"],
            "modules": [module],
        }
        if target:
            body["target_module_id"] = target
        return body

    first_edit = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json=payload("edit it", "module_target_one"),
    )
    assert first_edit.status_code == 200

    duplicate_edit = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json=payload("edit it again", "module_target_one"),
    )
    assert duplicate_edit.status_code == 409
    assert duplicate_edit.json()["detail"]["conflict"] == "target_busy"

    for prompt in ("create one", "create two"):
        resp = await client.post(
            "/api/v1/dashboard/layout/suggest/jobs",
            headers=headers,
            json=payload(prompt),
        )
        assert resp.status_code == 200

    over_limit = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json=payload("one too many"),
    )
    assert over_limit.status_code == 409
    assert over_limit.json()["detail"]["conflict"] == "concurrency_limit"

    release.set()
    # Drain the released jobs so no background task outlives the test engine.
    from packages.core.services import dashboard_generation as generation_module

    pending_tasks = [
        job.task
        for job in generation_module._jobs.values()
        if job.status == "running" and job.task is not None
    ]
    if pending_tasks:
        await asyncio.gather(*pending_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_dashboard_news_builds_gdelt_safe_queries(
    monkeypatch: pytest.MonkeyPatch,
):
    """GDELT rejects parentheses around non-OR queries with a plain-text 200,
    which silently emptied every plain-keyword news module."""
    from packages.core.services import dashboard_news

    captured: list[str] = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"articles": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            captured.append(params["query"])
            return FakeResponse()

    monkeypatch.setattr(dashboard_news.httpx, "AsyncClient", FakeClient)
    dashboard_news._news_cache.clear()

    await dashboard_news.get_dashboard_news(query="AI", days=1, limit=5, locale="zh")
    await dashboard_news.get_dashboard_news(
        query="AI OR robotics", days=1, limit=5, locale="en"
    )
    await dashboard_news.get_dashboard_news(query=None, days=1, limit=5, locale="en")

    assert captured == [
        "AI sourcelang:Chinese",
        "(AI OR robotics) sourcelang:English",
        "sourcelang:English",
    ]


@pytest.mark.asyncio
async def test_dashboard_news_falls_back_to_duckduckgo_when_gdelt_is_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import dashboard_news

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("GDELT returned plain text")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            return FakeResponse()

    async def fake_ddg(query, language, days, limit):
        assert query == "AI"
        assert language == "Chinese"
        return [
            {
                "id": "ddg-1",
                "title": "Fallback headline",
                "url": "https://example.com/fallback",
                "source": "Example",
                "published_at": None,
                "language": language,
            }
        ]

    monkeypatch.setattr(dashboard_news.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(dashboard_news, "_duckduckgo_news", fake_ddg)
    dashboard_news._news_cache.clear()

    items = await dashboard_news.get_dashboard_news(
        query="AI", days=1, limit=5, locale="zh"
    )
    assert [item["title"] for item in items] == ["Fallback headline"]


@pytest.mark.asyncio
async def test_dashboard_module_conversations_stay_out_of_chat_history(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    async def fake_agent(db, *, user, **_kwargs):
        return await _dashboard_agent_result(db, user, None)

    monkeypatch.setattr(dashboard_router, "run_dashboard_agent_turn", fake_agent)
    headers = await _auth(client, "dashconvhidden")
    defaults = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()

    start = await client.post(
        "/api/v1/dashboard/layout/suggest/jobs",
        headers=headers,
        json={"prompt": "Anything at all", "widgets": defaults["widgets"]},
    )
    finished = await _poll_dashboard_job(client, headers, start.json()["job_id"])
    module_conversation_id = finished["result"]["conversation_id"]
    assert module_conversation_id

    listing = await client.get("/api/v1/chat/conversations", headers=headers)
    assert listing.status_code == 200
    listed_ids = {conv["id"] for conv in listing.json()}
    assert module_conversation_id not in listed_ids
