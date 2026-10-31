from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_workspace_delegation_uses_durable_per_call_billing_with_attempt_nonce(
    monkeypatch,
):
    from packages.core.ai import context as context_module
    from packages.core.ai.runtime import billing as billing_module
    from packages.core.ai.runtime import harness as harness_module
    from packages.core.ai.runtime import workspace_delegation_actions as actions
    from packages.core import database as database_module
    from packages.core.services import usage_service

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_resolve_service(*_args, **_kwargs):
        subscription = SimpleNamespace(
            id="subscription-1",
            agent_id="agent-1",
            service_key="research",
            name="Research",
            custom_prompt=None,
        )
        agent = SimpleNamespace(id="agent-1", name="Research Agent")
        return subscription, agent, [], None

    async def fake_build_context(*_args, **_kwargs):
        return SimpleNamespace(
            runtime_envelope=None,
            system_prompt="system",
            tools=[],
            allowed_tool_names=[],
            model="gpt-5.5",
            temperature=0.2,
            max_tokens=200,
            task_id="task-1",
            tool_profile=None,
            llm_metadata=None,
            byok=False,
        )

    prefixes: list[str] = []
    reserved_callbacks: list[object] = []
    settled_callbacks: list[object] = []

    def fake_billing_callbacks(**kwargs):
        prefixes.append(kwargs["source_prefix"])

        async def reserve(*_args, **_kwargs):
            return None

        async def settle(*_args, **_kwargs):
            return None

        reserved_callbacks.append(reserve)
        settled_callbacks.append(settle)
        return reserve, settle

    loop_contexts: list[tuple[bool, bool]] = []

    async def fake_loop(**kwargs):
        current = billing_module.runtime_current_billing_context()
        loop_contexts.append((bool(current.suppress), bool(current.durable_per_call)))
        call_index = len(loop_contexts) - 1
        assert kwargs["on_llm_call_before"] is reserved_callbacks[call_index]
        assert kwargs["on_llm_usage_settled"] is settled_callbacks[call_index]
        return SimpleNamespace(
            content='{"answer":"done"}',
            messages=[],
            usage={"prompt": 10, "completion": 2},
            rounds=1,
            tool_calls_made=[],
            stop_reason="completed",
            error=None,
            error_detail=None,
            control={},
        )

    monkeypatch.setattr(database_module, "async_session", fake_session)

    async def owner_pays(_db, *, entity_id, workspace_id):
        return entity_id  # the fake session has no delivery rows to look up

    monkeypatch.setattr("packages.core.services.billing_payer.resolve_run_payer", owner_pays)
    monkeypatch.setattr(actions, "_resolve_workspace_service_agent", fake_resolve_service)
    monkeypatch.setattr(context_module, "build_agent_context", fake_build_context)
    monkeypatch.setattr(
        usage_service,
        "build_durable_llm_billing_callbacks",
        fake_billing_callbacks,
    )
    monkeypatch.setattr(harness_module, "runtime_execute_chat_agent_loop", fake_loop)

    params = {
        "service_key": "research",
        "prompt": "Investigate",
        "delegation_idempotency_key": "coordinator-child-v1:stable-child",
    }
    first = json.loads(await actions.runtime_workspace_delegate_service_action(
        entity_id="entity-1",
        workspace_id="workspace-1",
        user_id="user-1",
        params=params,
        task_id="task-1",
        step_id="step-1",
    ))
    second = json.loads(await actions.runtime_workspace_delegate_service_action(
        entity_id="entity-1",
        workspace_id="workspace-1",
        user_id="user-1",
        params=params,
        task_id="task-1",
        step_id="step-1",
    ))

    stable_prefix = "delegation:coordinator-child-v1:stable-child:"
    assert first["status"] == second["status"] == "completed"
    assert len(prefixes) == 2
    assert all(prefix.startswith(stable_prefix) for prefix in prefixes)
    assert prefixes[0] != prefixes[1]
    assert loop_contexts == [(True, True), (True, True)]
    assert billing_module.runtime_current_billing_context() is None
