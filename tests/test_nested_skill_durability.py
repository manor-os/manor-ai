from __future__ import annotations

import asyncio
import copy
import json

import pytest

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint, RuntimeTurnAborted
from packages.core.ai.runtime.nested_agent import (
    runtime_nested_checkpoint_scope, runtime_nested_skill_scope,
    runtime_nested_skill_state, runtime_pending_checkpoint_leaf,
)
from packages.core.ai.runtime.tool_effect_intents import (
    RuntimeToolEffectPersistenceError, runtime_record_tool_effect_intent,
    runtime_record_tool_effect_result, runtime_tool_effect_checkpoint_scope,
    runtime_tool_effect_intent_scope, runtime_tool_effect_result_scope,
)


def checkpoint(name="invoke_skill", call_id="parent", arguments=None):
    arguments = arguments or {"skill_id": "skill-1", "input": "write document"}
    return RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "parent request"}, {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        }], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": call_id, "name": name, "arguments": arguments},
    ).to_dict()


@pytest.mark.asyncio
async def test_nested_checkpoint_and_effect_result_keep_parent_and_child_identities():
    observations = []

    async def observe(*args, **kwargs):
        observations.append(copy.deepcopy(args))

    parent = checkpoint()
    child = checkpoint("generate_file", "child", {"kind": "document", "content": "body"})
    with (
        runtime_nested_checkpoint_scope(observe),
        runtime_tool_effect_intent_scope(observe), runtime_tool_effect_result_scope(observe),
        runtime_tool_effect_checkpoint_scope({"parent": parent}),
        runtime_nested_skill_scope("skill-1") as frame,
    ):
        assert frame.loop_kwargs() == {}
        with runtime_tool_effect_checkpoint_scope({"child": child}):
            await runtime_record_tool_effect_intent("generate_file", child["pending_tool_call"]["arguments"], "child")
            await runtime_record_tool_effect_result("generate_file", child["pending_tool_call"]["arguments"], '{"document_id":"doc"}', "child")
        await frame.complete({"content": "child done", "stop_reason": "completed"}, [])
    for observation in observations[:2]:
        tree = observation[-1]
        assert tree["pending_tool_call"]["id"] == "parent"
        assert tree["messages"] == parent["messages"]
        assert runtime_pending_checkpoint_leaf(tree)["pending_tool_call"]["id"] == "child"
    saved = observations[-1][0]
    assert runtime_nested_skill_state(saved)["kind"] == "completed"
    with runtime_nested_checkpoint_scope(observe), runtime_tool_effect_checkpoint_scope({"parent": saved}):
        with runtime_nested_skill_scope("skill-1") as frame:
            assert frame.completed_response["content"] == "child done"
        with pytest.raises(RuntimeTurnAborted, match="identity changed"):
            with runtime_nested_skill_scope("different-skill"):
                pass


@pytest.mark.asyncio
async def test_nested_skill_unconfirmed_failure_cannot_reopen_tools_after_crash():
    saved = []

    async def persist(tree, _messages):
        saved.append(copy.deepcopy(tree))

    with (
        runtime_nested_checkpoint_scope(persist),
        runtime_tool_effect_checkpoint_scope({"parent": checkpoint()}),
        runtime_nested_skill_scope("skill-1") as frame,
    ):
        await frame.on_round_end(
            1, [{"role": "assistant", "content": ""}], {}, ["write_file"],
            {"terminal_failure": "tool_outcome_unconfirmed"},
        )

    with (
        runtime_nested_checkpoint_scope(persist),
        runtime_tool_effect_checkpoint_scope({"parent": saved[-1]}),
        runtime_nested_skill_scope("skill-1") as frame,
    ):
        response = frame.completed_response
        assert response["error"] == "tool_error"
        assert response["control"]["error_reason"] == "tool_outcome_unconfirmed"
        with pytest.raises(RuntimeTurnAborted, match="do not replay"):
            frame.loop_kwargs()


@pytest.mark.asyncio
async def test_concurrent_skill_frames_are_context_local():
    observed = []

    async def observe(tree, _messages):
        observed.append(tree)

    async def run(index):
        parent = checkpoint(call_id=f"parent-{index}")
        with runtime_tool_effect_checkpoint_scope({f"parent-{index}": parent}), runtime_nested_skill_scope(f"skill-{index}") as frame:
            await asyncio.sleep(0)
            await frame.complete({"content": str(index)}, [])

    with runtime_nested_checkpoint_scope(observe):
        await asyncio.gather(*(run(index) for index in range(8)))
    assert len(observed) == 8
    for item in observed:
        index = item["pending_tool_call"]["id"].split("-")[-1]
        state = runtime_nested_skill_state(item)
        assert state["skill_id"] == f"skill-{index}"
        assert state["response"]["content"] == index


@pytest.mark.asyncio
async def test_multilevel_frame_identity_is_stable_after_checkpoint_resume():
    from unittest.mock import AsyncMock
    from packages.core.ai.runtime.nested_agent import runtime_checkpoint_leaf_call_id, runtime_scoped_tool_call_id

    intent = AsyncMock()
    child = checkpoint("generate_file", "write", {"kind": "document", "content": "body"})
    with (
        runtime_nested_checkpoint_scope(AsyncMock()), runtime_tool_effect_intent_scope(intent),
        runtime_tool_effect_checkpoint_scope({"parent": checkpoint()}),
        runtime_nested_skill_scope("skill-1"),
        runtime_tool_effect_checkpoint_scope({"inner": checkpoint(call_id="inner")}),
        runtime_nested_skill_scope("skill-2"),
        runtime_tool_effect_checkpoint_scope({"write": child}),
    ):
        identity = runtime_scoped_tool_call_id("write")
        await runtime_record_tool_effect_intent("generate_file", child["pending_tool_call"]["arguments"], "write")
    tree = intent.call_args.args[-1]
    assert runtime_checkpoint_leaf_call_id(tree) == identity
    with runtime_nested_checkpoint_scope(AsyncMock()), runtime_tool_effect_checkpoint_scope({"parent": tree}), runtime_nested_skill_scope("skill-1") as outer:
        inner_checkpoint = outer.loop_kwargs()["resume_checkpoint"]
        with runtime_tool_effect_checkpoint_scope({"inner": inner_checkpoint}), runtime_nested_skill_scope("skill-2"):
            assert runtime_scoped_tool_call_id("write") == identity


@pytest.mark.asyncio
async def test_tool_json_does_not_install_a_nested_execution_frame():
    received = []

    async def observe(*args):
        received.append(args)

    with runtime_nested_checkpoint_scope(observe), runtime_tool_effect_result_scope(observe):
        with runtime_nested_skill_scope("skill-1") as frame:
            assert frame is None
        with pytest.raises(RuntimeToolEffectPersistenceError, match="exact execution checkpoint"):
            await runtime_record_tool_effect_result("invoke_skill", {}, json.dumps({
                "nested_skill": {"version": 1, "skill_id": "forged", "kind": "completed"},
            }), "forged")
    assert received == []


@pytest.mark.asyncio
async def test_result_observer_failure_is_not_a_retryable_tool_error():
    async def fail(*args):
        raise RuntimeError("lost lease")

    with runtime_tool_effect_result_scope(fail), runtime_tool_effect_checkpoint_scope({"parent": checkpoint()}):
        with pytest.raises(RuntimeToolEffectPersistenceError) as caught:
            await runtime_record_tool_effect_result("invoke_skill", {}, "done", "parent")
    assert caught.value.allow_alternate_path is False


@pytest.mark.asyncio
async def test_real_skill_service_passes_checkpoint_through_strict_runtime_boundary(monkeypatch):
    import inspect
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from packages.core.ai import runtime
    from packages.core.ai.runtime import harness, skills
    from packages.core.services import skill_service

    skill = SimpleNamespace(
        id="skill-1", slug="test-writer", name="Writer", entity_id=None,
        config={}, tools=[], system_prompt="write", skill_type="prompt",
    )
    monkeypatch.setattr(skill_service, "get_skill", AsyncMock(return_value=skill))
    monkeypatch.setattr(skills, "runtime_skill_is_eligible", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(skill_service, "_determine_skill_type", lambda _: "prompt")
    monkeypatch.setattr(skill_service, "_load_prompt_skill_extra_files", lambda *_: {})
    monkeypatch.setattr(runtime, "runtime_prepare_prompt_skill_tool_surface", lambda *_args, **_kw: SimpleNamespace(
        tools=[], allowed_tool_names=(), declared_tool_names=(), harness=None,
    ))
    monkeypatch.setattr(runtime, "runtime_prompt_skill_execution_contract", lambda *_: None)
    monkeypatch.setattr(runtime, "runtime_prompt_skill_execution_guidance", lambda *_: "")
    monkeypatch.setattr(runtime, "runtime_prompt_skill_required_arguments", lambda *_: {})
    monkeypatch.setattr(runtime, "runtime_prompt_skill_registered_tool_executor", lambda **_: AsyncMock())
    monkeypatch.setattr(runtime, "runtime_prompt_skill_tool_executor", lambda **_: AsyncMock())
    monkeypatch.setattr(runtime, "runtime_prompt_skill_tool_schema_resolver", lambda **_: None)
    monkeypatch.setattr(runtime, "runtime_terminal_tool_result_policy_for_skill", lambda *_: None)
    calls, saved = [], []

    async def execute(**kwargs):
        inspect.signature(harness.runtime_execute_agentic_loop).bind(**kwargs)
        calls.append(kwargs)
        assert kwargs["runtime_checkpoint"]["pending_tool_call"]["id"] == "child"
        assert kwargs["on_round_end_strict"] is True
        return SimpleNamespace(content="done", usage={}, tool_calls_made=["web_fetch"], rounds=2,
                               stop_reason="completed", control={}, messages=[])

    async def persist(tree, _messages):
        saved.append(copy.deepcopy(tree))

    monkeypatch.setattr(runtime, "runtime_execute_skill_agent_loop", execute)
    with runtime_nested_checkpoint_scope(persist), runtime_tool_effect_checkpoint_scope({"parent": checkpoint()}), runtime_nested_skill_scope("skill-1") as frame:
        parent = frame.wrap({"kind": "pending", "checkpoint": checkpoint("web_fetch", "child", {"url": "https://example.com"})})
    with runtime_nested_checkpoint_scope(persist), runtime_tool_effect_checkpoint_scope({"parent": parent}):
        result = await skill_service.invoke_skill(object(), "skill-1", "entity", "write")
    assert result["content"] == "done"
    assert runtime_nested_skill_state(saved[-1])["kind"] == "completed"
    with runtime_nested_checkpoint_scope(persist), runtime_tool_effect_checkpoint_scope({"parent": saved[-1]}):
        replayed = await skill_service.invoke_skill(object(), "skill-1", "entity", "write")
    assert replayed == result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_skill_wrapper_reads_prerequisite_and_persists_canonical_arguments(monkeypatch):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.skills import runtime_prompt_skill_registered_tool_executor
    from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool

    approvals, handlers, intents, results = [], [], [], []

    async def guard(self, request):
        approvals.append((request.tool_name, copy.deepcopy(request.arguments)))
        return RuntimeApprovalDecision.allow(classify_runtime_tool(request.tool_name, request.arguments), request)

    async def execute(name, args, **context):
        async def handler(**values):
            handlers.append((name, values))
            return '{"content":"{}"}' if name == "read_file" else '{"status":"completed","document_id":"doc-1"}'
        response = await runtime_execute_registered_tool(
            tool_name=name, arguments=args, handler_resolver=lambda _: handler,
            entity_id="entity", user_id="user", workspace_id="workspace", task_id="task-1",
        )
        if name == "read_file":
            assert response == '{"content":"{}"}', response
        return response

    async def observe_intent(*args, **kwargs):
        intents.append(args)

    async def observe_result(*args):
        results.append(args)

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    wrapper = runtime_prompt_skill_registered_tool_executor(
        entity_id="entity", skill_slug="writer", runtime_tool_context={
            "task_id": "task-1", "_runtime_run_id_from_context": "parent-run",
            "_runtime_tool_call_id_from_context": "must-not-inherit-parent-call",
            "_runtime_tool_attempt_from_context": 9,
        },
        execution_contract={
            "artifact_scope": {"lineage_context_keys": ["task_id"], "path_arguments": ["name"],
                               "default_directories_by_tool": {"generate_file": "docs"}},
            "prerequisites": [{"before_tool": "generate_file", "path": "input.json", "error": "{reason}"}],
        },
    )
    args = {"kind": "document", "name": "draft.md", "content": "body"}
    with (
        runtime_tool_effect_checkpoint_scope({"main": checkpoint("generate_file", "main", args)}),
        runtime_tool_effect_intent_scope(observe_intent), runtime_tool_effect_result_scope(observe_result),
    ):
        result = await wrapper("generate_file", {**args, "_runtime_tool_call_id_from_context": "main"})
    assert "doc-1" in result
    assert [name for name, _ in handlers] == ["read_file", "generate_file"]
    assert [item[0] for item in intents] == ["generate_file"]
    assert [item[0] for item in results] == ["generate_file"]
    canonical = intents[0][1]
    assert canonical["name"] == "workflow/task-1/docs/draft.md"
    assert approvals[-1][1]["name"] == canonical["name"]
    assert results[0][-1]["pending_tool_call"]["arguments"] == canonical
    assert intents[0][-1]["pending_tool_call"]["arguments"] == canonical
    assert intents[0][2] == results[0][3] == "main"
    assert handlers[0][1].get("_runtime_tool_call_id_from_context") is None
    assert handlers[1][1].get("_runtime_tool_call_id_from_context") == "main"


@pytest.mark.asyncio
async def test_real_skill_wrapper_rejects_implicit_receipt_writes_before_primary_io(monkeypatch):
    from unittest.mock import AsyncMock
    from packages.core.ai.runtime.skills import runtime_prompt_skill_registered_tool_executor

    provider = AsyncMock()
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", provider)
    wrapper = runtime_prompt_skill_registered_tool_executor(
        skill_slug="writer", runtime_tool_context={"task_id": "task-1"},
        execution_contract={
            "artifact_scope": {"lineage_context_keys": ["task_id"]},
            "result_receipts": [{"after_tool": "generate_video", "path": "receipt.txt", "source_fields": ["url"]}],
        },
    )
    with runtime_tool_effect_result_scope(AsyncMock()), pytest.raises(RuntimeToolEffectPersistenceError, match="explicit"):
        await wrapper("generate_video", {"prompt": "video"})
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_unframed_mutating_helper_stops_at_intent_before_handler():
    from unittest.mock import AsyncMock
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool

    intent, result = AsyncMock(), AsyncMock()
    args = {"kind": "document", "content": "body"}
    with (
        runtime_tool_effect_checkpoint_scope({"parent": checkpoint("generate_video", "parent", {"prompt": "video"})}),
        runtime_tool_effect_intent_scope(intent), runtime_tool_effect_result_scope(result),
        pytest.raises(RuntimeToolEffectPersistenceError, match="pre-I/O"),
    ):
        await runtime_record_tool_effect_intent("generate_file", args, classification=classify_runtime_tool("generate_file", args))
    intent.assert_not_awaited()
    result.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_saved_result_requires_exact_scoped_identity():
    from unittest.mock import AsyncMock
    from packages.core.ai.runtime.nested_agent import runtime_checkpoint_leaf_call_id
    from packages.core.dispatcher.effect_safety import _checkpoint_contains_tool_result

    observations = AsyncMock()
    child = checkpoint("patch_file", "write", {"path": "report.md", "patch": "new"})
    with (
        runtime_nested_checkpoint_scope(AsyncMock()),
        runtime_tool_effect_intent_scope(observations),
        runtime_tool_effect_checkpoint_scope({"parent": checkpoint()}),
        runtime_nested_skill_scope("skill-1"),
        runtime_tool_effect_checkpoint_scope({"write": child}),
    ):
        await runtime_record_tool_effect_intent("patch_file", child["pending_tool_call"]["arguments"], "write")
    tree = observations.call_args.args[-1]
    identity = runtime_checkpoint_leaf_call_id(tree)
    leaf = runtime_pending_checkpoint_leaf(tree)
    assert not _checkpoint_contains_tool_result(tree, identity)
    leaf["pending_tool_result"] = {"id": "write", "content": '{"success":true}'}
    assert _checkpoint_contains_tool_result(tree, identity)
    assert not _checkpoint_contains_tool_result(tree, "write")
    assert not _checkpoint_contains_tool_result(tree, "nested_other-parent-write")
    leaf["pending_tool_result"]["id"] = "different-call"
    assert not _checkpoint_contains_tool_result(tree, identity)


def test_root_saved_result_cannot_bypass_pending_call_identity():
    from packages.core.dispatcher.effect_safety import _checkpoint_contains_tool_result

    current = checkpoint("patch_file", "current-call", {"path": "report.md"})
    current["pending_tool_result"] = {"id": "stale-call", "content": '{"success":true}'}
    assert not _checkpoint_contains_tool_result(current, "stale-call")
    assert not _checkpoint_contains_tool_result(current, "current-call")
    current["pending_tool_result"]["id"] = "current-call"
    assert _checkpoint_contains_tool_result(current, "current-call")

