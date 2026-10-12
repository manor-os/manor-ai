from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "pass123",
        "entity_name": "Flow Template Corp",
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}





def test_standalone_service_agents_preserve_tools_prompt_and_explicit_overrides() -> None:
    from packages.core.services.workflow_template_service import (
        _standalone_service_agents,
        _with_standalone_service_agents,
    )

    payload = {
        "recipe": {"subscriptions": [{
            "service_key": "publisher", "agent_slug": "publisher-agent",
            "custom_prompt": "Use the approved voice.",
        }]},
        "embedded": {"agents": [{
            "slug": "publisher-agent", "system_prompt": "Publish safely.",
            "mcp_bindings": [{"allowed_tools": ["mcp__example__publish", "mcp__example__publish"]}],
            "tool_bindings": [{"tool_name": "submit_result"}],
        }]},
    }
    services = _standalone_service_agents(payload)
    values = {"steps": [
        {"id": "inherit", "config": {"service_key": "publisher"}},
        {"id": "agent", "config": {"service_key": "publisher", "agent_id": "explicit"}},
        {"id": "tools", "config": {"service_key": "publisher", "tools": ["explicit_tool"]}},
    ]}

    assert services == {"publisher": {
        "tools": ["mcp__example__publish", "submit_result"],
        "system_prompt": "Publish safely.\n\nUse the approved voice.",
    }}
    result = _with_standalone_service_agents(values, services)
    assert result["steps"][0]["config"] == {
        "service_key": "publisher",
        "standalone_tools": ["mcp__example__publish", "submit_result"],
        "standalone_system_prompt": "Publish safely.\n\nUse the approved voice.",
    }
    assert result["steps"][1]["config"] == {"service_key": "publisher", "agent_id": "explicit"}
    assert result["steps"][2]["config"] == {"service_key": "publisher", "tools": ["explicit_tool"]}
