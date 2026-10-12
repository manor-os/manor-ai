"""A Workflow agent step can refuse named operations of a tool, nested Skills included."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.contracts.step_execution import (
    TOOL_OPERATION_DENYLIST_KEY,
    tool_operation_denied_error,
    tool_operation_denylist_scope,
)


pytestmark = pytest.mark.unit
NO_RENDER = {"video_edit": ["render"]}


def test_denylist_matches_tool_and_operation_only_inside_its_scope():
    assert tool_operation_denied_error("video_edit", {"operation": "render"}) is None
    with tool_operation_denylist_scope(NO_RENDER):
        assert "No action was executed" in tool_operation_denied_error("video_edit", {"operation": "render"})
        assert tool_operation_denied_error("video_edit", {"operation": " RENDER "})
        assert tool_operation_denied_error("video_edit", {"operation": "review"}) is None
        assert tool_operation_denied_error("video_edit", {"operation": "write_files"}) is None
        assert tool_operation_denied_error("probe_media", {"operation": "render"}) is None
        assert tool_operation_denied_error("video_edit", {}) is None
    assert tool_operation_denied_error("video_edit", {"operation": "render"}) is None


def test_nested_scopes_only_add_denials():
    with tool_operation_denylist_scope(NO_RENDER):
        with tool_operation_denylist_scope(None):
            assert tool_operation_denied_error("video_edit", {"operation": "render"})
        with tool_operation_denylist_scope({"video_edit": ["close"]}):
            assert tool_operation_denied_error("video_edit", {"operation": "render"})
            assert tool_operation_denied_error("video_edit", {"operation": "close"})
        assert tool_operation_denied_error("video_edit", {"operation": "close"}) is None


async def _allow_approval(monkeypatch, calls):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware

    async def guard(self, request):
        calls.append((request.tool_name, (request.arguments or {}).get("operation")))
        return RuntimeApprovalDecision.allow(classify_runtime_tool(request.tool_name, request.arguments), request)

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)


async def _video_edit(operation, handler):
    from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool

    return await runtime_execute_registered_tool(
        tool_name="video_edit", arguments={"operation": operation, "session_id": "s1"},
        handler_resolver=lambda _: handler, entity_id="entity", user_id="user", workspace_id="workspace",
    )


async def test_runtime_refuses_before_approval_and_handler_as_a_preflight_result(monkeypatch):
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    approvals, handled = [], []
    await _allow_approval(monkeypatch, approvals)

    async def handler(**kwargs):
        handled.append(kwargs["operation"])
        return {"ok": True}

    with tool_operation_denylist_scope(NO_RENDER):
        refused = await _video_edit("render", handler)
        allowed = await _video_edit("review", handler)
    assert isinstance(refused, RuntimePreflightValidationError)
    payload = json.loads(refused)
    assert payload["error"] == "tool_operation_denied"
    assert "video_edit(operation='render')" in payload["message"]
    assert json.loads(allowed) == {"ok": True}
    assert approvals == [("video_edit", "review")]
    assert handled == ["review"]


async def test_prompt_skill_loop_tools_inherit_the_step_denylist(monkeypatch):
    """The nested Skill executor re-enters the same Runtime gate in the same async context."""
    from packages.core.ai.runtime import tool_registry
    from packages.core.ai.runtime.skills import runtime_prompt_skill_registered_tool_executor
    from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    approvals, handled = [], []
    await _allow_approval(monkeypatch, approvals)

    async def handler(**kwargs):
        handled.append(kwargs["operation"])
        return {"ok": True}

    class Registry:
        async def execute(self, name, args, **kwargs):
            return await runtime_execute_registered_tool(
                tool_name=name, arguments=args, handler_resolver=lambda _: handler,
                entity_id=kwargs.get("entity_id"), user_id=kwargs.get("user_id"),
                workspace_id=kwargs.get("workspace_id"),
            )

    monkeypatch.setattr(tool_registry, "runtime_ensure_tool_registry_initialized", lambda: Registry())
    nested = runtime_prompt_skill_registered_tool_executor(
        entity_id="entity", user_id="user", workspace_id="workspace", skill_slug="video-edit",
        runtime_tool_context={"_workflow_run_id_from_context": "run", "_workflow_step_id_from_context": "produce"},
    )
    with tool_operation_denylist_scope(NO_RENDER):
        refused = await nested("video_edit", {"operation": "render", "session_id": "s1"})
        allowed = await nested("video_edit", {"operation": "write_files", "session_id": "s1"})
    assert isinstance(refused, RuntimePreflightValidationError)
    assert json.loads(refused)["error"] == "tool_operation_denied"
    assert json.loads(allowed) == {"ok": True}
    assert handled == ["write_files"]


def _appendix():
    async def appendix(*args, **kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names={"video_edit"}, envelope=None)

    return appendix


@pytest.mark.parametrize("denylist,render_refused", [(NO_RENDER, True), (None, False)])
async def test_runner_installs_the_step_denylist_for_the_agent_loop(monkeypatch, denylist, render_refused):
    from packages.core.ai import workflow_runner as module
    from packages.core.ai.workflow_runner import WorkflowRunner

    approvals, handled, seen = [], [], []
    await _allow_approval(monkeypatch, approvals)

    async def handler(**kwargs):
        handled.append(kwargs["operation"])
        return {"ok": True}

    async def loop(**kwargs):
        seen.append(tool_operation_denied_error("video_edit", {"operation": "render"}) is not None)
        await _video_edit("render", handler)
        return SimpleNamespace(content='{"status": "ready"}', usage={}, tool_calls_made=[], rounds=1,
                               stop_reason="completed", error=None, error_detail=None)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", _appendix())
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", loop)
    config = {"prompt": "Produce", "tools": ["video_edit"], "output_format": "json"}
    if denylist is not None:
        config[TOOL_OPERATION_DENYLIST_KEY] = denylist
    result = await WorkflowRunner()._execute_agent_step({"id": "produce", "type": "agent", "config": config},
                                                         {}, "entity")
    assert result["status"] == "completed"
    assert seen == [render_refused]
    assert handled == ([] if render_refused else ["render"])
    # The scope ends with the step.
    assert tool_operation_denied_error("video_edit", {"operation": "render"}) is None


async def test_runner_installs_the_step_denylist_around_a_skill_step(monkeypatch):
    from packages.core.ai import workflow_runner as module
    from packages.core.ai.workflow_runner import WorkflowRunner

    seen = []

    async def invoke(*args, **kwargs):
        seen.append(tool_operation_denied_error("video_edit", {"operation": "render"}) is not None)
        return {"content": "done", "usage": {}, "tools_used": []}

    async def skill_definition(_db, skill_ref):
        assert skill_ref == "video-edit"
        return SimpleNamespace(config={"type": "prompt"})

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", skill_definition)
    monkeypatch.setattr(module, "runtime_invoke_skill", invoke)
    step = {"id": "produce", "type": "agent", "config": {
        "skill": "video-edit", "input": "Build it", TOOL_OPERATION_DENYLIST_KEY: NO_RENDER}}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def release(db):
        return None

    monkeypatch.setattr(module, "release_read_transaction", release)
    result = await WorkflowRunner(session_factory=Session)._execute_agent_step(step, {}, "entity")
    assert result["status"] == "completed", result
    assert seen == [True]
    assert tool_operation_denied_error("video_edit", {"operation": "render"}) is None


def _graph(config, *, step_type="agent"):
    return [
        {"id": "start", "type": "trigger", "config": {}, "next": ["produce"]},
        {"id": "produce", "type": step_type, "config": config, "next": []},
    ]


def test_workflow_validation_accepts_a_real_tool_operation():
    from packages.core.services.workflow_service import validate_workflow_steps

    result = validate_workflow_steps(_graph({"prompt": "x", TOOL_OPERATION_DENYLIST_KEY: NO_RENDER}))
    assert result["valid"], result["errors"]


@pytest.mark.parametrize("config,fragment", [
    ({TOOL_OPERATION_DENYLIST_KEY: {"video_edit": ["rendr"]}}, "rendr"),
    ({TOOL_OPERATION_DENYLIST_KEY: {"video_edits": ["render"]}}, "video_edits"),
    ({TOOL_OPERATION_DENYLIST_KEY: {"probe_media": ["render"]}}, "operation"),
    ({TOOL_OPERATION_DENYLIST_KEY: {"video_edit": "render"}}, "list"),
    ({TOOL_OPERATION_DENYLIST_KEY: {"video_edit": []}}, "list"),
    ({TOOL_OPERATION_DENYLIST_KEY: ["video_edit"]}, "object"),
    ({"tool_operation_denylsit": NO_RENDER}, TOOL_OPERATION_DENYLIST_KEY),
    ({"tool_operations_denylist": NO_RENDER}, TOOL_OPERATION_DENYLIST_KEY),
])
def test_workflow_validation_rejects_typos_and_bad_shapes(config, fragment):
    from packages.core.services.workflow_service import validate_workflow_steps

    result = validate_workflow_steps(_graph({"prompt": "x", **config}))
    assert not result["valid"]
    assert any(fragment in error["message"] for error in result["errors"]), result["errors"]


def test_workflow_validation_rejects_the_key_outside_agent_steps():
    from packages.core.services.workflow_service import validate_workflow_steps

    result = validate_workflow_steps(_graph(
        {"tool": "video_edit", TOOL_OPERATION_DENYLIST_KEY: NO_RENDER}, step_type="tool",
    ))
    assert not result["valid"]
    assert any("agent" in error["message"] for error in result["errors"]), result["errors"]
