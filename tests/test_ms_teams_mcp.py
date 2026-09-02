"""Microsoft Teams Graph contract regressions."""
from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_create_chat_includes_the_delegated_user_as_a_member(monkeypatch):
    from packages.core.ai.mcp import ms_teams

    calls: list[dict] = []

    async def fake_api(token, method, path, body=None, params=None):
        calls.append({
            "token": token,
            "method": method,
            "path": path,
            "body": body,
            "params": params,
        })
        if path == "me":
            return json.dumps({"id": "user-initiator"})
        return json.dumps({"id": "chat-1"})

    monkeypatch.setattr(ms_teams, "_api", fake_api)

    result = await ms_teams._create_chat(
        "delegated-access-token",
        {"recipients": ["person@example.test"]},
    )

    assert json.loads(result) == {"id": "chat-1"}
    assert calls == [
        {
            "token": "delegated-access-token",
            "method": "GET",
            "path": "me",
            "body": None,
            "params": None,
        },
        {
            "token": "delegated-access-token",
            "method": "POST",
            "path": "chats",
            "body": {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": (
                            "https://graph.microsoft.com/v1.0/users('user-initiator')"
                        ),
                    },
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": (
                            "https://graph.microsoft.com/v1.0/users('person@example.test')"
                        ),
                    },
                ],
            },
            "params": None,
        },
    ]
