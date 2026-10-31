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
            {"search_tools", "invoke_skill"} | envelope.discovered_tool_grants.tool_names
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
    assert _invoke_skill_schema_load_names({"status": "sandbox_ready", "loaded_tools": ["sandbox"]}) == []
    assert (
        _invoke_skill_schema_load_names(
            {
                "status": "completed",
                "sandbox_id": "sandbox-1",
                "loaded_tools": ["sandbox"],
            }
        )
        == []
    )


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
        seen_second_round = {str(tool.get("function", {}).get("name") or "") for tool in tools}
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
    with (
        patch(
            "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
            side_effect=fake_completion,
        ),
        patch(
            "packages.core.ai.runtime.tool_registry.runtime_tool_schema",
            side_effect=lambda name: sandbox_schema if name == "sandbox" else None,
        ),
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
async def test_delegated_sandbox_binding_needs_no_skill_handoff_on_internal_surfaces() -> None:
    """The Sandbox is a server-owned grant for every internal Agent.

    No Skill handoff or durable binding row is consulted: a session that
    would explode on use proves the decision is made without storage. The
    Sandbox handler still binds each sandbox_id to its owning agent/user.
    """
    from packages.core.ai.runtime.composite_tools import SANDBOX_LEGACY_TOOL_BY_ACTION
    from packages.core.ai.runtime.profiles import INTERNAL_CHAT_SURFACES
    from packages.core.services.runtime_authorization.binding_verifier import (
        CurrentRuntimeToolBindingVerifier,
    )
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationAccess,
        RuntimeAuthorizationRequest,
        RuntimeToolBindingSource,
    )

    verifier = CurrentRuntimeToolBindingVerifier()
    for surface in sorted(INTERNAL_CHAT_SURFACES, key=lambda item: item.value):
        for tool_name in ("sandbox", *SANDBOX_LEGACY_TOOL_BY_ACTION.values()):
            request = RuntimeAuthorizationRequest.create(
                entity_id="entity-1",
                user_id="user-1",
                workspace_id=None,
                action_key="sandbox.execute",
                capability_id="sandbox.execute",
                access=RuntimeAuthorizationAccess.USE,
                principal_kind="agent",
                principal_agent_id="agent-without-skills",
                principal_execution_user_id="user-1",
                tool_name=tool_name,
                bound_tool_names={"search_tools", tool_name},
                runtime_surface=surface,
            )
            decision = await verifier.verify(SimpleNamespace(), request)
            assert decision.source is RuntimeToolBindingSource.CONTEXTUAL, (surface, tool_name)


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
        await runtime_sandbox.runtime_init_sandbox_binding(
            runtime_sandbox.RuntimeSandboxScope("entity-1", "user-1", "conversation-1"),
            "sandbox-1",
            "skill-1",
            agent_id="agent-1",
        )


class _FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return None


def _office_skill_ready(monkeypatch, captured: dict):
    from packages.core import database
    from packages.core.ai.tools import generate_file_tool
    from packages.core.ai.tools.generate_file import office_common
    from packages.core.services import skill_service

    async def fake_scope(**kwargs):
        return kwargs.get("name") or "artifact.xlsx"

    async def fake_list_skills(_db, _entity_id):
        return []

    async def fake_invoke(_db, skill, _entity_id, _input_text, **kwargs):
        captured["skill"] = skill
        captured["kwargs"] = kwargs
        return {
            "skill": skill,
            "content": "✓ Sandbox ready for skill xlsx",
            "usage": {},
            "tools_used": [],
            "rounds": 0,
            "stop_reason": "sandbox_ready",
            "sandbox_id": "sandbox-9",
        }

    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(skill_service, "list_skills", fake_list_skills)
    monkeypatch.setattr(office_common, "runtime_invoke_skill", fake_invoke)
    # The generate_file_tool shim re-points common._scope_workspace_output_name
    # to its own module global on every call, so patch the shim's name.
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope)


@pytest.mark.asyncio
async def test_generate_file_office_kind_hands_off_like_invoke_skill(monkeypatch) -> None:
    from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrants
    from packages.core.ai.tools import generate_file_tool

    captured: dict = {}
    _office_skill_ready(monkeypatch, captured)
    envelope = SimpleNamespace(discovered_tool_grants=RuntimeDiscoveredToolGrants())

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        kind="spreadsheet",
        name="crm.xlsx",
        prompt="Consolidate partner leads into a CRM workbook",
        _runtime_envelope_from_context=envelope,
        _agent_id_from_context="agent-1",
        _runtime_run_id_from_context="run-1",
        _runtime_tool_call_id_from_context="call-1",
        _runtime_tool_attempt_from_context=2,
    )

    payload = json.loads(result)
    assert payload["status"] == "sandbox_ready"
    assert payload["sandbox_id"] == "sandbox-9"
    assert payload["loaded_tools"] == ["sandbox"]
    assert payload["skill"] == "xlsx"
    assert "Sandbox ready" in payload["content"]
    assert envelope.discovered_tool_grants.tool_names == {"sandbox"}
    assert captured["skill"] == "xlsx"
    assert captured["kwargs"]["runtime_tool_context"] == {
        "_runtime_run_id_from_context": "run-1",
        "_runtime_tool_call_id_from_context": "call-1",
        "_runtime_tool_attempt_from_context": 2,
    }


@pytest.mark.asyncio
async def test_generate_file_office_kind_surfaces_structured_skill_errors(monkeypatch) -> None:
    from packages.core import database
    from packages.core.ai.tools import generate_file_tool
    from packages.core.ai.tools.generate_file import office_common
    from packages.core.services import skill_service

    async def fake_scope(**kwargs):
        return kwargs.get("name") or "artifact.docx"

    async def fake_list_skills(_db, _entity_id):
        return []

    async def fake_invoke(_db, skill, _entity_id, _input_text, **_kwargs):
        return {
            "skill": skill,
            "content": "Sandbox resources are busy. Please retry later.",
            "usage": {},
            "tools_used": [],
            "rounds": 0,
            "stop_reason": "error",
            "error": "Sandbox resources are busy. Please retry later.",
            "code": "capacity_full",
            "retryable": True,
        }

    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(skill_service, "list_skills", fake_list_skills)
    monkeypatch.setattr(office_common, "runtime_invoke_skill", fake_invoke)
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        kind="word_document",
        name="memo.docx",
        prompt="write the memo",
    )

    payload = json.loads(result)
    assert payload["error"].startswith("Sandbox resources are busy")
    assert payload["skill"] == "docx"
    assert payload["code"] == "capacity_full"
    assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_generate_file_office_kind_passes_suspension_through(monkeypatch) -> None:
    from packages.core import database
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.tools import generate_file_tool
    from packages.core.ai.tools.generate_file import office_common
    from packages.core.services import skill_service

    suspension = RuntimeToolSuspension(
        kind="waiting_resource",
        reservation_id="res-1",
        poll_after_seconds=5,
        deadline_at="2026-09-04T00:00:00+00:00",
    )

    async def fake_scope(**kwargs):
        return kwargs.get("name") or "artifact.pptx"

    async def fake_list_skills(_db, _entity_id):
        return []

    async def fake_invoke(_db, _skill, _entity_id, _input_text, **_kwargs):
        return suspension

    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(skill_service, "list_skills", fake_list_skills)
    monkeypatch.setattr(office_common, "runtime_invoke_skill", fake_invoke)
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope)

    result = await generate_file_tool._generate_file_handler(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        kind="presentation",
        name="deck.pptx",
        prompt="build the deck",
    )

    assert result is suspension


def test_sandbox_envelope_grant_expands_to_legacy_leaf_tools() -> None:
    from packages.core.ai.runtime.composite_tools import SANDBOX_LEGACY_TOOL_BY_ACTION
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.constants.runtime_principal import RuntimePrincipalKind

    envelope = RuntimeEnvelope(
        surface=ChatSurface.AGENT_DM,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.AGENT, agent_id="agent-1"),
        profile=RuntimeProfile.AGENT_DELEGATE,
        allowed_tool_names=("sandbox",),
    )
    effective = envelope.effective_allowed_tool_names()
    assert "sandbox" in effective
    for leaf in SANDBOX_LEGACY_TOOL_BY_ACTION.values():
        assert leaf in effective
    assert envelope.allowed_tool_names == ("sandbox",)

    blocked = RuntimeEnvelope(
        surface=ChatSurface.AGENT_DM,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.AGENT, agent_id="agent-1"),
        profile=RuntimeProfile.AGENT_DELEGATE,
        allowed_tool_names=("sandbox",),
        blocked_tool_names=("sandbox",),
    )
    assert blocked.effective_allowed_tool_names() == set()
