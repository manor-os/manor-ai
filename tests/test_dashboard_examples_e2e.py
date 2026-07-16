from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from packages.core.ai.runtime.dashboard_module_validation import (
    validate_dashboard_module_code,
)
from packages.core.ai.runtime.dashboard_submission import (
    runtime_record_dashboard_submission,
)
from packages.core.ai.tools.code_tool import _code_handler
from packages.core.models.base import generate_ulid
from packages.core.services.conversation_messages import add_message
from packages.core.services.dashboard_agent import dashboard_tool_is_read_only
from tests.dashboard_example_scenarios import dashboard_example_scenarios


def test_realistic_dashboard_examples_use_distinct_visual_structures():
    signatures = {
        "attention_queue": ("attention-board", "attention-lane", "task-tile"),
        "workspace_risk": ("risk-chart", "risk-track", "risk-segment"),
        "automation_health": ("automation-table", "health-state", "switch"),
        "agent_directory": ("agent-grid", "agent-card", "agent-avatar"),
        "external_briefing": ("news-layout", "lead-story", "story-row"),
    }

    scenarios = dashboard_example_scenarios()
    assert {scenario["id"] for scenario in scenarios} == set(signatures)
    assert len({scenario["code"]["css"] for scenario in scenarios}) == len(scenarios)

    for scenario in scenarios:
        source = "\n".join(
            (
                scenario["code"]["html"],
                scenario["code"]["css"],
                scenario["code"]["javascript"],
            )
        )
        for signature in signatures[scenario["id"]]:
            assert signature in source


async def _auth(client: AsyncClient, username: str) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, payload


@pytest.mark.asyncio
async def test_realistic_dashboard_requests_preview_validate_and_save(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router
    from packages.core.services import dashboard_agent as dashboard_agent_service

    scenarios = dashboard_example_scenarios()
    by_prompt = {scenario["prompt"]: scenario for scenario in scenarios}

    async def fake_runtime(message, conversation_id, **kwargs):
        scenario = by_prompt[message]
        submission = {
            "widgets": [
                {"id": widget_id, "visible": True}
                for widget_id in dashboard_router.DASHBOARD_WIDGET_IDS
            ],
            "module_changes": [
                {
                    "action": "create",
                    "title": scenario["title"],
                    "size": scenario["size"],
                    "visible": True,
                    "code": scenario["code"],
                }
            ],
        }
        validation = json.loads(
            await _code_handler(
                action="dashboard_module_validate",
                params={"code": scenario["code"]},
                _manual_skill_slugs_from_context=["dashboard-module-builder"],
            )
        )
        assert validation["platform_ready"] is True
        runtime_record_dashboard_submission(submission)
        tool_calls = [
            *scenario["tool_calls"],
            "code",
            "dashboard_submit_module",
        ]
        await add_message(
            kwargs["db"],
            conversation_id,
            role="assistant",
            content=f"{scenario['title']} preview is ready.",
            tool_calls=[{"name": name, "arguments": {}} for name in tool_calls],
        )
        await kwargs["db"].commit()
        return {
            "conversation_id": conversation_id,
            "content": f"{scenario['title']} preview is ready.",
            "tool_calls_made": tool_calls,
            "hitl_requests": [],
        }

    monkeypatch.setattr(dashboard_agent_service, "runtime_run_chat_turn", fake_runtime)
    monkeypatch.setattr(dashboard_agent_service, "dashboard_blocked_tool_names", lambda: ())

    headers, _user = await _auth(client, "dashboard_examples")
    layout = (await client.get("/api/v1/dashboard/layout", headers=headers)).json()
    for scenario in scenarios:
        response = await client.post(
            "/api/v1/dashboard/layout/suggest",
            headers=headers,
            json={
                "prompt": scenario["prompt"],
                "widgets": layout["widgets"],
                "modules": layout["modules"],
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["preview_created"] is True
        assert preview["modules"][-1]["title"] == scenario["title"]
        assert preview["modules"][-1]["code"] == scenario["code"]
        assert validate_dashboard_module_code(scenario["code"])["platform_ready"] is True
        assert "code" in preview["tool_calls"]
        for tool_name in scenario["tool_calls"]:
            assert tool_name in preview["tool_calls"]
            assert dashboard_tool_is_read_only(tool_name) is True

        saved = await client.put(
            "/api/v1/dashboard/layout",
            headers=headers,
            json={"widgets": preview["widgets"], "modules": preview["modules"]},
        )
        assert saved.status_code == 200, saved.text
        layout = saved.json()

    assert [module["title"] for module in layout["modules"]] == [
        scenario["title"] for scenario in scenarios
    ]
    reloaded = await client.get("/api/v1/dashboard/layout", headers=headers)
    assert reloaded.json() == layout

    other_headers, _other_user = await _auth(client, "dashboard_examples_other")
    other_layout = await client.get("/api/v1/dashboard/layout", headers=other_headers)
    assert other_layout.json()["modules"] == []


@pytest.mark.asyncio
async def test_dashboard_read_tools_return_structured_entity_scoped_data(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import dashboard as dashboard_router

    cached: dict[str, object] = {}

    async def cache_get(key):
        return cached.get(key)

    async def cache_set(key, value, ttl):
        cached[key] = value
        cached[f"{key}:ttl"] = ttl

    monkeypatch.setattr(dashboard_router.cache, "get", cache_get)
    monkeypatch.setattr(dashboard_router.cache, "set", cache_set)

    headers, _user = await _auth(client, "dashboard_tool_examples")
    agent_response = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Customer Operations Agent",
            "description": "Handles customer operations follow-up.",
            "system_prompt": "Sensitive operating instructions must never appear in Dashboard tool data.",
            "category": "operations",
            "tags": ["customer"],
        },
    )
    assert agent_response.status_code == 201, agent_response.text

    job_response = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": f"dashboard-example-{generate_ulid().lower()}",
            "name": "Weekly customer health summary",
            "schedule_kind": "cron",
            "cron_expr": "0 9 * * 1",
            "timezone": "America/Los_Angeles",
            "payload_message": "Sensitive automation instructions must not be returned.",
        },
    )
    assert job_response.status_code == 201, job_response.text

    agents = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=headers,
        json={
            "tool_name": "query_entity_agents",
            "arguments": {"statuses": ["active"], "limit": 20},
            "refresh_seconds": 60,
        },
    )
    assert agents.status_code == 200, agents.text
    agent_items = agents.json()["result"]["agents"]
    assert [item["name"] for item in agent_items] == ["Customer Operations Agent"]
    assert agent_items[0]["category"] == "operations"
    assert "system_prompt" not in agent_items[0]

    automations = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=headers,
        json={
            "tool_name": "query_scheduled_jobs",
            "arguments": {"limit": 20},
            "refresh_seconds": 60,
        },
    )
    assert automations.status_code == 200, automations.text
    automation_items = automations.json()["result"]["automations"]
    assert [item["name"] for item in automation_items] == [
        "Weekly customer health summary"
    ]
    assert automation_items[0]["cron_expr"] == "0 9 * * 1"
    assert "payload_message" not in automation_items[0]

    other_headers, _other_user = await _auth(client, "dashboard_tool_examples_other")
    other_agents = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=other_headers,
        json={
            "tool_name": "query_entity_agents",
            "arguments": {"limit": 20},
        },
    )
    other_automations = await client.post(
        "/api/v1/dashboard/tool-data",
        headers=other_headers,
        json={
            "tool_name": "query_scheduled_jobs",
            "arguments": {"limit": 20},
        },
    )
    assert other_agents.json()["result"]["agents"] == []
    assert other_automations.json()["result"]["automations"] == []
