"""The worker subagent step must only forward kwargs the subagent loop accepts.

Regression for a production TypeError:
    runtime_execute_subagent_loop() got an unexpected keyword argument 'user_id'
which failed every subagent-kind plan step (3/3 retries). _exec_subagent passed
user_id=, but runtime_execute_subagent_loop has no such parameter.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import packages.core.workers.internal as internal
from packages.core.ai.runtime import harness


def test_exec_subagent_only_forwards_supported_loop_kwargs(monkeypatch):
    captured: dict = {}

    class _Stop(Exception):
        pass

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        raise _Stop()

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    fake_ctx = SimpleNamespace(
        system_prompt="sys",
        runtime_envelope=None,
        tools=[],
        legacy_runtime_profile="profile",
        allowed_tool_names=set(),
        model="model-x",
        llm_metadata=None,
    )

    async def fake_build_agent_context(*args, **kwargs):
        return fake_ctx

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)

    class _FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())

    step = {
        "params": {"prompt": "draft the blog"},
        "entity_id": "ent",
        "resolved_agent_id": "agent",
        "user_id": "user",
        "workspace_id": "ws",
        "conversation_id": "conv",
        "task_id": "task",
        "expected_output_schema": None,
    }

    with pytest.raises(_Stop):
        asyncio.run(internal._exec_subagent(step))

    accepted = set(inspect.signature(harness.runtime_execute_subagent_loop).parameters)
    unsupported = set(captured) - accepted
    assert not unsupported, f"_exec_subagent forwards kwargs the subagent loop rejects: {unsupported}"
