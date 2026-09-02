from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_sandbox_skill_ready_grants_only_the_composite_sandbox_tool(
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.ai.runtime import skills as runtime_skills
    from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrants
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import skill_service

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return None

    async def fake_get_skill(_db, _skill_id):
        return SimpleNamespace(
            id="skill_xlsx",
            slug="xlsx",
            name="xlsx",
            status="active",
            entity_id=None,
        )

    async def fake_runtime_invoke_skill(*_args, **_kwargs):
        return {
            "skill": "xlsx",
            "content": "sandbox instructions",
            "usage": {},
            "stop_reason": "sandbox_ready",
            "sandbox_id": "sandbox-1",
        }

    envelope = SimpleNamespace(
        surface=ChatSurface.AGENT_DM,
        profile=RuntimeProfile.AGENT_DELEGATE,
        allowed_tool_names=("search_tools", "invoke_skill"),
        blocked_tool_names=(),
        discovered_tool_grants=RuntimeDiscoveredToolGrants(),
        metadata={},
        effective_allowed_tool_names=lambda: (
            {"search_tools", "invoke_skill"}
            | envelope.discovered_tool_grants.tool_names
        ),
    )
    context = SimpleNamespace(
        active_user_message="生成一个销售表格",
        manual_skill_selected=False,
        agent_id="agent-1",
        workspace_id=None,
        conversation_id="conversation-1",
        task_id=None,
        tool_profile=None,
        allowed_tool_names={"search_tools", "invoke_skill"},
        runtime_envelope=envelope,
    )

    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(
        runtime_skills,
        "runtime_invoke_skill",
        fake_runtime_invoke_skill,
    )

    result = await runtime_skills.runtime_invoke_skill_action(
        entity_id="entity-1",
        user_id="user-1",
        skill="xlsx",
        runtime_context=context,
    )

    payload = json.loads(result)
    assert payload["status"] == "sandbox_ready"
    assert payload["sandbox_id"] == "sandbox-1"
    assert payload["loaded_tools"] == ["sandbox"]
    assert envelope.discovered_tool_grants.tool_names == {"sandbox"}
    assert "bash" not in envelope.effective_allowed_tool_names()
    assert "generate_file" not in envelope.effective_allowed_tool_names()


def test_agentic_loop_accepts_only_the_trusted_sandbox_handoff_schema() -> None:
    from packages.core.ai.agentic_loop import _invoke_skill_schema_load_names

    assert _invoke_skill_schema_load_names(
        {
            "status": "sandbox_ready",
            "sandbox_id": "sandbox-1",
            "loaded_tools": ["sandbox", "bash", "generate_file"],
        }
    ) == ["sandbox"]
    assert _invoke_skill_schema_load_names(
        {"status": "sandbox_ready", "loaded_tools": ["sandbox"]}
    ) == []
    assert _invoke_skill_schema_load_names(
        {
            "status": "completed",
            "sandbox_id": "sandbox-1",
            "loaded_tools": ["sandbox"],
        }
    ) == []


@pytest.mark.asyncio
async def test_agentic_loop_loads_sandbox_after_skill_handoff() -> None:
    from packages.core.ai.agentic_loop import agentic_loop

    invoke_schema = {
        "type": "function",
        "function": {
            "name": "invoke_skill",
            "description": "Invoke a Skill",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    sandbox_schema = {
        "type": "function",
        "function": {
            "name": "sandbox",
            "description": "Operate a Sandbox",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    seen_second_round: set[str] = set()

    async def fake_completion(messages, tools, **_kwargs):
        nonlocal seen_second_round
        if len(messages) <= 2:
            return (
                None,
                [{"id": "invoke-1", "name": "invoke_skill", "arguments": {}}],
                {"prompt": 1, "completion": 1, "total": 2},
            )
        seen_second_round = {
            str(tool.get("function", {}).get("name") or "")
            for tool in tools
        }
        return (
            "sandbox is callable",
            None,
            {"prompt": 1, "completion": 1, "total": 2},
        )

    handoff = json.dumps(
        {
            "status": "sandbox_ready",
            "sandbox_id": "sandbox-1",
            "loaded_tools": ["sandbox"],
            "content": "follow the packaged workflow",
        }
    )
    with patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        side_effect=fake_completion,
    ), patch(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema",
        side_effect=lambda name: sandbox_schema if name == "sandbox" else None,
    ):
        result = await agentic_loop(
            system_prompt="test",
            user_message="create a spreadsheet",
            tools=[invoke_schema],
            tool_executor=AsyncMock(return_value=handoff),
            # A real Agent resolver only knows the original static bindings.
            # The sandbox schema becomes visible after invoke_skill grants it.
            tool_schema_resolver=lambda _name: None,
        )

    assert result.content == "sandbox is callable"
    assert seen_second_round == {"invoke_skill", "sandbox"}


@pytest.mark.asyncio
async def test_delegated_sandbox_binding_rechecks_active_skill_context(
    monkeypatch,
) -> None:
    from packages.core.services.runtime_authorization.binding_verifier import (
        CurrentRuntimeToolBindingVerifier,
    )
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationAccess,
        RuntimeAuthorizationRequest,
        RuntimeToolBindingSource,
    )

    async def fake_load_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-1",
            "skill_id": "skill-xlsx",
            "entity_id": "entity-1",
            "user_id": "user-1",
            "agent_id": "agent-1",
        }

    async def fake_list_skills_for_agent(
        _db,
        _entity_id,
        _agent_id,
        **_kwargs,
    ):
        return [SimpleNamespace(id="skill-xlsx", slug="xlsx", name="xlsx")]

    monkeypatch.setattr(
        "packages.core.ai.runtime.sandbox.runtime_load_sandbox_context",
        fake_load_context,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_service.list_skills_for_agent",
        fake_list_skills_for_agent,
    )

    request = RuntimeAuthorizationRequest.create(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id=None,
        action_key="sandbox.execute",
        capability_id="sandbox.execute",
        access=RuntimeAuthorizationAccess.USE,
        principal_kind="agent",
        principal_agent_id="agent-1",
        principal_execution_user_id="user-1",
        tool_name="sandbox",
        bound_tool_names={"search_tools", "invoke_skill", "sandbox"},
        conversation_id="conversation-1",
        runtime_surface="agent_dm",
    )

    verifier = CurrentRuntimeToolBindingVerifier()
    decision = await verifier.verify(object(), request)

    assert decision.source is RuntimeToolBindingSource.CONTEXTUAL

    wrong_agent_request = RuntimeAuthorizationRequest.create(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id=None,
        action_key="sandbox.execute",
        capability_id="sandbox.execute",
        access=RuntimeAuthorizationAccess.USE,
        principal_kind="agent",
        principal_agent_id="agent-2",
        principal_execution_user_id="user-1",
        tool_name="sandbox",
        bound_tool_names={"sandbox"},
        conversation_id="conversation-1",
        runtime_surface="agent_dm",
    )
    assert not await verifier._has_active_sandbox_skill_binding(
        object(),
        wrong_agent_request,
    )


@pytest.mark.asyncio
async def test_delegated_sandbox_binding_rejects_same_slug_replacement(
    monkeypatch,
) -> None:
    from packages.core.services.runtime_authorization.binding_verifier import (
        CurrentRuntimeToolBindingVerifier,
    )
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationAccess,
        RuntimeAuthorizationRequest,
    )

    async def fake_load_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-1",
            "skill_id": "revoked-skill-id",
            "entity_id": "entity-1",
            "user_id": "user-1",
            "agent_id": "agent-1",
        }

    async def fake_list_skills_for_agent(
        _db,
        _entity_id,
        _agent_id,
        **_kwargs,
    ):
        return [SimpleNamespace(id="replacement-skill-id", slug="xlsx", name="xlsx")]

    monkeypatch.setattr(
        "packages.core.ai.runtime.sandbox.runtime_load_sandbox_context",
        fake_load_context,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_service.list_skills_for_agent",
        fake_list_skills_for_agent,
    )

    request = RuntimeAuthorizationRequest.create(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id=None,
        action_key="sandbox.execute",
        capability_id="sandbox.execute",
        access=RuntimeAuthorizationAccess.USE,
        principal_kind="agent",
        principal_agent_id="agent-1",
        principal_execution_user_id="user-1",
        tool_name="sandbox",
        bound_tool_names={"sandbox"},
        conversation_id="conversation-1",
        runtime_surface="agent_dm",
    )

    assert not await CurrentRuntimeToolBindingVerifier._has_active_sandbox_skill_binding(
        object(),
        request,
    )


@pytest.mark.asyncio
async def test_sandbox_context_initialization_requires_successful_cache_write(
    monkeypatch,
) -> None:
    from packages.core import cache as cache_module
    from packages.core.ai.runtime import sandbox as runtime_sandbox

    async def cache_miss(_key):
        return None

    async def failed_cache_write(*_args, **_kwargs):
        return False

    monkeypatch.setattr(cache_module.cache, "get", cache_miss)
    monkeypatch.setattr(cache_module.cache, "set", failed_cache_write)

    with pytest.raises(RuntimeError, match="could not be persisted"):
        await runtime_sandbox.runtime_init_sandbox_context(
            "conversation-1",
            "sandbox-1",
            "skill-1",
            entity_id="entity-1",
            user_id="user-1",
            agent_id="agent-1",
        )
