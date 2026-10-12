"""A prompt skill's nested loop must never queue a suspending sandbox ticket.

Observed 2026-09-14 (e2e, durable external-runner): the model invoked the
``skill-creator`` prompt skill, whose nested loop then invoked the ``pdf``
sandbox skill with no warm container. Admission queued a durable reservation
for the *outer* ``invoke_skill`` call and returned a suspension, but a nested
loop has no checkpoint to park on: the suspension surfaced to the parent as an
empty tool result and the ticket stayed ``pending_checkpoint`` forever.

The nested loop marks every tool call it runs with the parent skill; the
marker travels through ``invoke_skill`` into the sandbox binding, and
admission refuses to reserve for it with an honest, non-retryable error.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_parent_skill_marker_is_a_runtime_tool_context_key() -> None:
    from packages.core.ai.runtime.tool_context import (
        RUNTIME_TOOL_CONTEXT_KEYS,
        runtime_tool_call_context_from_kwargs,
    )

    # The prompt-skill executor only forwards keys in this set to the tools
    # it runs; a marker outside it would never reach sandbox admission.
    assert "_runtime_parent_skill_from_context" in RUNTIME_TOOL_CONTEXT_KEYS
    context = runtime_tool_call_context_from_kwargs(
        {"_runtime_parent_skill_from_context": "skill-creator"},
    )
    assert context.parent_skill == "skill-creator"
    assert runtime_tool_call_context_from_kwargs({}).parent_skill is None


@pytest.mark.asyncio
async def test_invoke_skill_action_forwards_the_parent_skill_marker(monkeypatch) -> None:
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
        return SimpleNamespace(id="skill_pdf", slug="pdf", name="pdf", status="active", entity_id=None)

    captured: dict = {}

    async def fake_runtime_invoke_skill(*_args, **kwargs):
        captured.update(kwargs)
        return {"skill": "pdf", "content": "ok", "usage": {}, "stop_reason": "stop"}

    envelope = SimpleNamespace(
        surface=ChatSurface.AGENT_DM,
        profile=RuntimeProfile.AGENT_DELEGATE,
        allowed_tool_names=("search_tools", "invoke_skill"),
        blocked_tool_names=(),
        discovered_tool_grants=RuntimeDiscoveredToolGrants(),
        metadata={},
        effective_allowed_tool_names=lambda: {"search_tools", "invoke_skill"},
    )
    context = SimpleNamespace(
        active_user_message="make a pdf",
        manual_skill_selected=False,
        agent_id="agent-1",
        workspace_id=None,
        conversation_id="conversation-1",
        task_id=None,
        tool_profile=None,
        allowed_tool_names={"search_tools", "invoke_skill"},
        runtime_envelope=envelope,
        # The nested loop of ``skill-creator`` runs under the outer run's
        # identity and the outer ``invoke_skill`` call id.
        runtime_run_id="run-1",
        runtime_tool_call_id="outer-call-1",
        runtime_tool_attempt=1,
        parent_skill="skill-creator",
    )
    monkeypatch.setattr(database, "async_session", lambda: FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    await runtime_skills.runtime_invoke_skill_action(
        entity_id="entity-1", user_id="user-1", skill="pdf", runtime_context=context,
    )

    forwarded = captured["runtime_tool_context"]
    assert forwarded["_runtime_run_id_from_context"] == "run-1"
    assert forwarded["_runtime_tool_call_id_from_context"] == "outer-call-1"
    assert forwarded["_runtime_parent_skill_from_context"] == "skill-creator"


@pytest.mark.asyncio
async def test_prompt_skill_marks_its_nested_tool_calls_with_itself(monkeypatch) -> None:
    from packages.core.services import skill_service

    skill = SimpleNamespace(
        id="skill_creator",
        entity_id=None,
        status="active",
        slug="skill-creator",
        name="skill-creator",
        tools=["invoke_skill"],
        config={},
        system_prompt="Create skills.",
    )
    executor_call: dict = {}

    async def fake_get_skill(db, skill_id):
        return None

    async def fake_get_skill_by_slug(db, slug, entity_id):
        return skill

    async def fake_loop(**kwargs):
        return SimpleNamespace(
            content="done", usage={}, tool_calls_made=[], rounds=1,
            stop_reason="stop", control={}, error=None, error_detail=None,
        )

    def fake_registered_tool_executor(**kwargs):
        executor_call.update(kwargs)

        async def execute(_name, _args):
            return "ok"

        return execute

    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(skill_service, "get_skill_by_slug", fake_get_skill_by_slug)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_skill_agent_loop", fake_loop)
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_prompt_skill_registered_tool_executor",
        fake_registered_tool_executor,
    )

    await skill_service.invoke_skill(
        object(), "skill-creator", "ent_1", "build a pdf skill",
        user_id="user_1", workspace_id="ws_1", enforce_agent_access=False,
        allowed_tool_names={"invoke_skill"},
        runtime_tool_context={
            "_runtime_run_id_from_context": "run-1",
            "_runtime_tool_call_id_from_context": "outer-call-1",
        },
    )

    nested = executor_call["runtime_tool_context"]
    assert nested["_runtime_parent_skill_from_context"] == "skill-creator"
    # The outer run identity still travels with the nested calls.
    assert nested["_runtime_run_id_from_context"] == "run-1"
    assert nested["_runtime_tool_call_id_from_context"] == "outer-call-1"


@pytest.mark.asyncio
async def test_sandbox_skill_binding_carries_the_parent_skill(monkeypatch, tmp_path) -> None:
    from packages.core.services import skill_service
    from packages.core.services.sandbox_session_service import SandboxSessionError

    skill_dir = tmp_path / "pdf_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# pdf", encoding="utf-8")
    (skill_dir / "run.py").write_text("print('ok')", encoding="utf-8")

    bindings: list = []

    async def fake_acquire(*, owner, bundle, binding, db=None, **_kwargs):
        bindings.append(binding)
        return SandboxSessionError.of("nested_allocation_unavailable", "refused")

    monkeypatch.setattr(skill_service, "acquire_sandbox_session", fake_acquire)

    result = await skill_service._invoke_sandbox_skill(
        SimpleNamespace(
            id="skill_pdf", entity_id=None, name="pdf", slug="pdf",
            system_prompt="# pdf", config={"source": "builtin", "skill_dir": str(skill_dir)},
        ),
        entity_id="ent_1", user_id="user_1", agent_id="agent_1", input_text="run",
        conversation_id="conversation_1",
        runtime_tool_context={
            "_runtime_run_id_from_context": "run-1",
            "_runtime_tool_call_id_from_context": "outer-call-1",
            "_runtime_parent_skill_from_context": "skill-creator",
        },
    )

    assert [b.parent_skill for b in bindings] == ["skill-creator"]
    assert bindings[0].runtime_run_id == "run-1"
    assert result["stop_reason"] == "error"
    assert result["code"] == "nested_allocation_unavailable"
    assert result["retryable"] is False
