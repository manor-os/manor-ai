from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.context import build_agent_context
from packages.core.ai.runtime import (
    ChatSurface,
    RuntimeEnvelope,
    RuntimePrincipal,
    RuntimePrincipalKind,
    RuntimeProfile,
)


def _authorization(*, task_id: str, workspace_id: str) -> dict:
    return {
        "version": 1,
        "authorization_id": f"proposal-item:{task_id}",
        "task_id": task_id,
        "workspace_id": workspace_id,
        "provider": "youtube",
        "action": "publish_video",
        "destination": "studio.youtube.com",
        "visibility": "public",
        "intended_channel": "paired_chrome_signed_in_channel",
        "max_executions": 1,
        "consumed_at": None,
    }


@pytest.mark.asyncio
async def test_build_agent_context_adds_worker_runtime_metadata_to_envelope(monkeypatch):
    authorization = _authorization(task_id="task", workspace_id="workspace")
    captured: dict = {}

    async def fake_resolve_model(*_args, **_kwargs):
        return "openai/gpt-5.5"

    async def fake_resolve_metadata(*_args, **_kwargs):
        return None

    async def fake_resolve_workspace_runtime(*_args, **_kwargs):
        captured["runtime_scope"] = _kwargs
        return SimpleNamespace(
            workspace_id="workspace",
            task_id="task",
            thread_ref_kind="task",
            thread_ref_id="task",
            tool_profile="workspace",
            bound_tool_names=(),
            is_master=True,
            mcp_allowed_names=(),
            extra_context="",
        )

    async def fake_assemble(_db, *, request, **_kwargs):
        captured["request"] = request
        envelope = RuntimeEnvelope(
            surface=ChatSurface.SCHEDULED_AGENT_RUN,
            principal=RuntimePrincipal(kind=RuntimePrincipalKind.SYSTEM_WORKER),
            profile=RuntimeProfile.BACKGROUND_WORKER,
            entity_id="entity",
            workspace_id="workspace",
            task_id="task",
            metadata=dict(request.metadata),
        )
        return SimpleNamespace(
            context=SimpleNamespace(agent=None),
            tool_schemas=[],
            tool_names=(),
            allowed_tool_names=(),
            envelope=envelope,
            prompt="system prompt",
        )

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(
        "packages.core.ai.context.runtime_assemble_prompt_for_turn",
        fake_assemble,
    )

    ctx = await build_agent_context(
        SimpleNamespace(),
        entity_id="entity",
        workspace_id="workspace",
        task_id="task",
        agent_subscription_id="resolved-subscription",
        runtime_metadata={"proposal_external_authorization": authorization, "agent_subscription_id": "stale-subscription"},
    )

    assert captured["request"].metadata["proposal_external_authorization"] == authorization
    assert ctx.runtime_envelope.metadata["proposal_external_authorization"] == authorization
    assert captured["runtime_scope"]["agent_subscription_id"] == "resolved-subscription"
    assert ctx.runtime_envelope.metadata["agent_subscription_id"] == "resolved-subscription"
