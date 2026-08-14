from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_personal_chat_lists_and_directly_starts_percent_flow(
    client: AsyncClient,
) -> None:
    registration = (await client.post("/api/v1/auth/register", json={
        "username": "personal_percent_flow",
        "email": "personal_percent_flow@test.com",
        "password": "pass123",
        "entity_name": "Personal Percent Flow",
    })).json()
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": "Launch Workspace",
    })).json()
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Publish launch brief",
        "variables": {"request": ""},
        "steps": [
            {
                "id": "trigger",
                "type": "trigger",
                "name": "Start",
                "config": {
                    "run_inputs": [
                        {"key": "request", "type": "string", "required": True},
                    ],
                },
                "next": ["end"],
            },
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace["id"],
        "trigger_type": "manual",
        "config": {
            "chat_entrypoint": {
                "enabled": True,
                "title": "Publish launch brief",
                "description": "Review the launch inputs before execution.",
            },
        },
    })).json()

    listed = await client.get("/api/v1/chat/flow-entrypoints", headers=headers)

    assert listed.status_code == 200, listed.text
    assert listed.json() == [{
        "binding_id": binding["id"],
        "workflow_id": workflow["id"],
        "workspace_id": workspace["id"],
        "workspace_name": "Launch Workspace",
        "title": "Publish launch brief",
        "description": "Review the launch inputs before execution.",
        "placeholder": "",
        "order": 100,
        "inputs": [{
            "key": "request",
            "label": "request",
            "type": "string",
            "required": True,
            "hidden": False,
            "placeholder": "",
            "default": None,
            "target": "request",
        }],
    }]

    started = await client.post(
        f"/api/v1/chat/flow-entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Prepare the August launch brief."},
    )

    assert started.status_code == 200, started.text
    assert "event: stream_start" in started.text
    assert "event: stream_end" in started.text

    conversations = (await client.get(
        "/api/v1/chat/conversations",
        headers=headers,
    )).json()
    assert len(conversations) == 1
    conversation_id = conversations[0]["id"]
    messages = (await client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        headers=headers,
    )).json()

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Prepare the August launch brief."
    assert messages[0]["meta"]["chat_mode"] == "flows"
    assert messages[1]["message_kind"] == "workflow_activity"
    assert messages[2]["pending_action"]["kind"] == "workflow_starter_input"
    assert messages[2]["pending_action"]["workflow_binding_id"] == binding["id"]

    runs = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()
    assert len(runs) == 1
    assert runs[0]["binding_id"] == binding["id"]
    assert runs[0]["status"] == "paused"
    assert runs[0]["trigger_source"] == "global_chat"
