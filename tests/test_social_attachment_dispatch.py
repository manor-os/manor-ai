"""Attachment uploads retain the normal MCP scope and publication gate."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.mcp import tiktok, youtube
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action
from packages.core.ai.tools import mcp_builtin
from packages.core.services.agent_permission_service import ToolAccessDecision


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
@pytest.mark.parametrize(
    "provider,tool,module,privacy",
    [
        ("youtube", "upload_video", youtube, {"privacy": "public"}),
        ("tiktok", "post_video", tiktok, {"privacy_level": "PUBLIC_TO_EVERYONE"}),
    ],
)
async def test_attachment_dispatch_isolates_and_clears_call_scope(
    monkeypatch, provider_fails, provider, tool, module, privacy
):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(transport="builtin"))

    monkeypatch.setattr("packages.core.database.async_session", Session)
    permission = AsyncMock(return_value=ToolAccessDecision(True, "connected", "user", "account-1"))
    monkeypatch.setattr("packages.core.services.agent_permission_service.can_use_integration", permission)
    monkeypatch.setattr(mcp_builtin, "_resolve_bearer_token", AsyncMock(return_value="test-token"))
    monkeypatch.setattr("packages.core.ai.mcp.file_registrar.register_generated_files", AsyncMock(return_value=0))
    barrier = asyncio.Event()
    observations = []

    async def provider_call(name, arguments, bearer_token):
        context = dict(module._call_ctx_var.get())
        observations.append(context)
        if len(observations) == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=2)
        assert module._call_ctx_var.get() == context
        assert name == tool
        assert arguments == {
            "video_url": "uploads/chat/approved.mp4",
            "title": "Approved tutorial",
            **privacy,
        }
        assert bearer_token == "test-token"
        if provider_fails:
            raise RuntimeError("synthetic upload failure")
        return {"content": [{"type": "text", "text": '{"id":"uploaded"}'}], "isError": False}

    monkeypatch.setattr(module, "call_tool", provider_call)
    handler = mcp_builtin._build_handler(provider, tool)

    async def invoke(index):
        result = json.loads(
            await handler(
                entity_id=f"entity-{index}",
                user_id=f"user-{index}",
                conversation_id=f"conversation-{index}",
                workspace_id=f"workspace-{index}",
                video_url="uploads/chat/approved.mp4",
                title="Approved tutorial",
                **privacy,
            )
        )
        assert module._call_ctx_var.get() == {}
        assert result == (
            {"server": provider, "tool": tool, "error": "call_failed", "detail": "synthetic upload failure"}
            if provider_fails
            else {"id": "uploaded"}
        )

    await asyncio.gather(invoke(1), invoke(2))
    assert sorted(observations, key=lambda value: value["user_id"]) == [
        {
            "user_id": f"user-{index}",
            "entity_id": f"entity-{index}",
            "conversation_id": f"conversation-{index}",
            "workspace_id": f"workspace-{index}",
            "runtime_validation_duration_ms": 0.0,
            "integration_account_id": "account-1",
        }
        for index in (1, 2)
    ]
    assert permission.await_count == 2
    assert all(call.kwargs["allow_env_fallback"] is False for call in permission.await_args_list)


@pytest.mark.parametrize("source", ["uploads/chat/approved.mp4", "/api/v1/fs/entity-1/uploads/chat/approved.mp4"])
@pytest.mark.parametrize(
    "tool,privacy",
    [
        ("mcp__youtube__upload_video", {"privacy": "public"}),
        ("mcp__tiktok__post_video", {"privacy_level": "PUBLIC_TO_EVERYONE"}),
    ],
)
def test_public_attachment_upload_retains_publication_approval(source, tool, privacy):
    action = classify_runtime_tool_action(
        tool,
        {"video_url": source, "title": "Approved tutorial", **privacy},
    )
    assert action is not None
    assert action.action_key == "social_post.publish"
    assert action.risk_level == "high"
    assert action.capability_id == "external.social"
