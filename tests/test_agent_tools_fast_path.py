"""GET /agents/{id}/tools must not build the actor capability catalog for agents
without any MCP surface (it dominated the endpoint's latency: 5-22s in prod)."""
from __future__ import annotations

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
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_agent_tools_skips_catalog_when_agent_has_no_mcp(client: AsyncClient, monkeypatch):
    from apps.api.routers import agents as agents_router
    from packages.core.database import async_session
    from packages.core.services.agent_service import create_tool_definition

    async def explode(*args, **kwargs):
        raise AssertionError("capability catalog must not be built for agents without MCP")

    monkeypatch.setattr(agents_router, "_actor_agent_capability_catalog", explode)

    headers = await _register(client, "agent_tools_fast_path")
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "No MCP"})
    assert agent.status_code in (200, 201), agent.text
    agent_id = agent.json()["id"]

    async with async_session() as db:
        tool = await create_tool_definition(db, name="fast_path_web_search", category="builtin")
        await db.commit()
        tool_id = tool.id

    bind = await client.post(f"/api/v1/agents/{agent_id}/tools", headers=headers, json={"tool_ids": [tool_id]})
    assert bind.status_code == 200, bind.text

    response = await client.get(f"/api/v1/agents/{agent_id}/tools", headers=headers)
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()] == ["fast_path_web_search"]
