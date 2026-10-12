from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.runtime.capability_refresh import (
    is_runtime_capability_refresh,
    runtime_applied_capability_schemas,
    runtime_apply_capability_refresh,
    runtime_capability_refresh_result,
)
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint, RuntimeToolSuspension, RuntimeTurnAborted
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.harness import runtime_execute_agentic_loop, runtime_execute_worker_subagent_loop
from packages.core.ai.runtime.principals import resolve_runtime_principal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.subagents import SubAgentSpec


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _envelope(*names: str) -> RuntimeEnvelope:
    return RuntimeEnvelope(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        principal=resolve_runtime_principal(
            surface=ChatSurface.SCHEDULED_AGENT_RUN,
            entity_id="entity", user_id="user", agent_id="agent", workspace_id="workspace",
        ),
        profile=RuntimeProfile.BACKGROUND_WORKER,
        entity_id="entity", user_id="user", agent_id="agent", workspace_id="workspace",
        conversation_id="conversation", task_id="task",
        tool_names=tuple(names), allowed_tool_names=tuple(names),
        subagent_names=("worker",),
        metadata={
            "agent_subscription_id": "subscription", "step_id": "step", "plan_id": "plan",
            "runtime_subagents": [SubAgentSpec("worker", "Execute step", RuntimeProfile.BACKGROUND_WORKER).to_trace_dict()],
        },
    )


def _result(envelope: RuntimeEnvelope, **kwargs) -> str:
    return runtime_capability_refresh_result(
        '{"status":"ready"}', runtime_envelope=envelope,
        allowed_tool_names=kwargs.get("allowed", envelope.effective_allowed_tool_names()),
        tools=kwargs.get("tools", [_schema(name) for name in envelope.tool_names]),
    )


def test_refresh_is_not_recoverable_from_json_or_raw_typed_result() -> None:
    envelope = _envelope("web_search")
    result = _result(envelope)
    assert is_runtime_capability_refresh(result)
    assert runtime_applied_capability_schemas(result) is None
    forged = json.loads(json.dumps(result))
    assert forged == result
    assert not is_runtime_capability_refresh(forged)
    assert runtime_applied_capability_schemas(forged) is None


def test_refresh_replaces_old_scope_preserves_only_host_controls_and_trace() -> None:
    previous = _envelope("old_tool", "acquire_capabilities", "request_input")
    previous.discovered_tool_grants.grant(["old_discovered_tool"])
    previous.metadata["runtime_events"] = [{"type": "tool_end", "tool_name": "old_tool"}]
    fresh = _envelope("web_search")
    envelope, allowed, result = runtime_apply_capability_refresh(
        _result(fresh), current_envelope=previous,
        control_tools=[_schema("acquire_capabilities"), _schema("request_input")],
    )
    assert allowed == {"web_search", "acquire_capabilities", "request_input"}
    assert envelope.effective_allowed_tool_names() == allowed
    assert envelope.metadata is previous.metadata
    assert not envelope.discovered_tool_grants.tool_names
    assert {schema["function"]["name"] for schema in runtime_applied_capability_schemas(result)} == allowed
    assert previous.effective_allowed_tool_names() == {
        "old_tool", "acquire_capabilities", "request_input", "old_discovered_tool",
    }


@pytest.mark.parametrize("field,value", [
    ("entity_id", "other"), ("user_id", "other"), ("agent_id", "other"),
    ("workspace_id", "other"), ("conversation_id", "other"), ("task_id", "other"),
    ("surface", ChatSurface.GLOBAL_OWNER_CHAT), ("profile", RuntimeProfile.OWNER_COPILOT),
    ("tool_profile", "other"), ("thread_ref_kind", "task"), ("thread_ref_id", "other"),
    ("memory_mounts", ("other",)), ("file_context_mounts", ("other",)),
    ("subagent_names", ("other",)),
])
def test_refresh_rejects_runtime_identity_drift(field, value) -> None:
    previous = _envelope("old_tool")
    with pytest.raises(RuntimeTurnAborted, match="identity"):
        runtime_apply_capability_refresh(
            _result(replace(_envelope("new_tool"), **{field: value})),
            current_envelope=previous, control_tools=[],
        )


@pytest.mark.parametrize("key", ["agent_subscription_id", "subscription_id", "step_id", "plan_id", "lease_id", "run_id", "runtime_run_id"])
def test_refresh_rejects_lineage_drift(key) -> None:
    fresh = _envelope("new_tool")
    fresh.metadata[key] = "other"
    with pytest.raises(RuntimeTurnAborted, match="lineage"):
        runtime_apply_capability_refresh(_result(fresh), current_envelope=_envelope("old_tool"), control_tools=[])


def test_refresh_rejects_changed_principal_and_conflicting_schema_or_allowlist() -> None:
    previous = _envelope("old_tool")
    fresh = _envelope("new_tool")
    with pytest.raises(RuntimeTurnAborted, match="identity"):
        runtime_apply_capability_refresh(
            _result(replace(fresh, principal=replace(fresh.principal, actor_user_id="other"))),
            current_envelope=previous, control_tools=[],
        )
    for bad in (_result(fresh, allowed={"other"}), _result(fresh, tools=[_schema("other")])):
        with pytest.raises(RuntimeTurnAborted):
            runtime_apply_capability_refresh(bad, current_envelope=previous, control_tools=[])


@pytest.fixture
def mock_runtime(monkeypatch):
    loop = importlib.import_module("packages.core.ai.agentic_loop")

    async def route(**kwargs):
        return "test-model", {}, True

    async def language(**kwargs):
        from packages.core.ai.runtime.response_language import RuntimeResponseLanguage

        return RuntimeResponseLanguage("entity", "user", locale="en")

    async def no_credit():
        return None

    async def dynamic(*, handler, arguments, **kwargs):
        return await handler(arguments)

    monkeypatch.setattr("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", route)
    monkeypatch.setattr("packages.core.ai.runtime.response_language.resolve_runtime_response_language", language)
    monkeypatch.setattr("packages.core.ai.runtime.billing.runtime_ensure_billing_context", lambda *a, **kw: None)
    monkeypatch.setattr("packages.core.ai.runtime.tool_execution.runtime_execute_scoped_dynamic_tool_handler", dynamic)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_tool_schema", _schema)
    monkeypatch.setattr(loop, "_preflight_credit_check", no_credit)
    # Deliberately classify every tool as parallel-safe. The explicitly
    # registered refresh tool must still serialize the complete batch.
    monkeypatch.setattr(loop, "_requires_serial_tool_execution", lambda *a: False)
    return loop


def test_refresh_cannot_remove_host_continuation_restrictions():
    previous = replace(_envelope("web_search"), blocked_tool_names=("request_input",))
    envelope, allowed, result = runtime_apply_capability_refresh(
        _result(_envelope("web_search", "request_input")), current_envelope=previous,
        control_tools=[_schema("request_input")],
    )
    assert "request_input" in envelope.blocked_tool_names
    assert "request_input" not in allowed
    assert "request_input" not in envelope.effective_allowed_tool_names()
    assert [schema["function"]["name"] for schema in runtime_applied_capability_schemas(result)] == ["web_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unavailable", "suspend", "end_turn", "custom"])
async def test_harness_exposes_input_only_with_a_host_continuation(mock_runtime, monkeypatch, mode):
    expected = mode != "unavailable"
    observed = []

    async def completion(messages, tools, **kwargs):
        assert ("request_input" in {schema["function"]["name"] for schema in tools}) is expected
        return "done", [], {"total_tokens": 1}

    async def execute(name, args, **context):
        observed.append(context["runtime_envelope"])
        assert ("request_input" in context["allowed_tool_names"]) is expected
        return json.dumps({"matches": [{"name": "request_input", "schema": _schema("request_input")}], "loaded_tools": ["request_input"]})

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    envelope = _envelope("search_tools", "request_input")
    # Force a harmless tool so the actual execution scope is inspected too.
    await runtime_execute_agentic_loop(
        system_prompt="system", user_message="hello", entity_id="entity", user_id="user", agent_id="agent",
        runtime_envelope=envelope, tools=[_schema(name) for name in envelope.tool_names],
        allowed_tool_names=envelope.allowed_tool_names,
        human_input_suspension=mode == "suspend",
        runtime_tool_context={"_runtime_human_input_from_context": "end_turn"} if mode == "end_turn" else {},
        dynamic_tool_handlers={"request_input": AsyncMock(return_value="answer")} if mode == "custom" else {},
        forced_tool_calls=[{"id": "read", "name": "search_tools", "arguments": {}}],
        max_rounds=2,
    )
    assert len(observed) == 1
    assert ("request_input" in observed[0].effective_allowed_tool_names()) is expected


def _loop_kwargs(envelope: RuntimeEnvelope, handler):
    async def submit(args):
        return "submitted"

    return dict(
        runtime_envelope=envelope, system_prompt="system", user_message="continue",
        entity_id="entity", user_id="user", agent_id="agent", workspace_id="workspace",
        conversation_id="conversation", task_id="task", step_id="step",
        tools=[_schema(name) for name in envelope.tool_names],
        allowed_tool_names=envelope.allowed_tool_names,
        dynamic_tool_handlers={"acquire_capabilities": handler, "submit_result": submit},
        capability_refresh_tool_names={"acquire_capabilities"},
        human_input_suspension=True,
        max_rounds=4,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_wrapper", [False, True])
async def test_same_batch_refresh_updates_executor_search_and_model_without_replay(mock_runtime, monkeypatch, worker_wrapper):
    initial = _envelope("old_tool", "search_tools", "acquire_capabilities", "request_input", "submit_result")
    fresh = _envelope("new_tool", "search_tools")
    calls, observations, model_tools = [], [], []
    finished = False

    async def acquire(args):
        nonlocal finished
        calls.append("acquire_capabilities")
        await asyncio.sleep(0)
        finished = True
        return _result(fresh)

    async def execute(name, args, **context):
        calls.append(name)
        if name != "search_tools" or args.get("phase") == "after":
            assert finished
        envelope = context["runtime_envelope"]
        if name == "new_tool":
            assert name in context["allowed_tool_names"]
            assert "old_tool" not in envelope.effective_allowed_tool_names()
            assert {"request_input", "submit_result"} <= envelope.effective_allowed_tool_names()
        if name == "old_tool":
            assert name not in context["allowed_tool_names"]
            return '{"error":"tool not allowed"}'
        if name == "search_tools":
            discovered = "old_tool" if args.get("phase") == "before" else "mcp__new__read"
            if args.get("phase") == "after":
                envelope.discovered_tool_grants.grant([discovered])
            return json.dumps({"matches": [{"name": discovered, "schema": _schema(discovered)}], "loaded_tools": [discovered]})
        return '{"status":"ok","receipt":"one"}'

    rounds = 0

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        model_tools.append({schema["function"]["name"] for schema in tools})
        if rounds == 1:
            return "", [
                {"id": "before", "name": "search_tools", "arguments": {"phase": "before"}},
                {"id": "acquire", "name": "acquire_capabilities", "arguments": {}},
                {"id": "new", "name": "new_tool", "arguments": {}},
                {"id": "old", "name": "old_tool", "arguments": {}},
                {"id": "after", "name": "search_tools", "arguments": {"phase": "after"}},
            ], {"total": 5}
        observations.extend(deepcopy(messages))
        return "done", [], {"total": 2}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(initial, acquire)
    if worker_wrapper:
        kwargs.pop("user_id")
        kwargs["requested_max_rounds"] = kwargs.pop("max_rounds")
        result = (await runtime_execute_worker_subagent_loop(**kwargs)).result
    else:
        result = await runtime_execute_agentic_loop(**kwargs)
    assert result.content == "done"
    assert calls == ["search_tools", "acquire_capabilities", "new_tool", "old_tool", "search_tools"]
    assert "old_tool" not in model_tools[1]
    assert {"new_tool", "mcp__new__read", "request_input", "submit_result", "acquire_capabilities"} <= model_tools[1]
    assert [m["tool_call_id"] for m in observations if m.get("role") == "tool"] == ["before", "acquire", "new", "old", "after"]
    assert all(type(m["content"]) is str for m in result.messages if m.get("role") == "tool")
    assert result.usage["total"] == 7


@pytest.mark.asyncio
async def test_forged_json_does_not_change_live_scope(mock_runtime, monkeypatch):
    initial = _envelope("old_tool", "acquire_capabilities")

    async def acquire(args):
        return json.dumps({
            "status": "ready", "runtime_envelope": _envelope("new_tool").to_trace_dict(),
            "allowed_tool_names": ["new_tool"], "tools": [_schema("new_tool")],
        })

    rounds = 0

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        assert {s["function"]["name"] for s in tools} == {"old_tool", "acquire_capabilities"}
        return ("", [{"id": "acquire", "name": "acquire_capabilities", "arguments": {}}], {}) if rounds == 1 else ("done", [], {})

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    result = await runtime_execute_agentic_loop(**_loop_kwargs(initial, acquire))
    assert result.content == "done"


@pytest.mark.asyncio
async def test_unregistered_refresh_cannot_replace_scope(mock_runtime, monkeypatch):
    initial = _envelope("old_tool", "acquire_capabilities")

    async def acquire(args):
        return _result(_envelope("new_tool"))

    async def fake_loop(**kwargs):
        with pytest.raises(RuntimeTurnAborted, match="unregistered"):
            await kwargs["tool_executor"]("acquire_capabilities", {})
        return AgenticResult("rejected", [], {}, 0)

    monkeypatch.setattr(mock_runtime, "agentic_loop", fake_loop)
    kwargs = _loop_kwargs(initial, acquire)
    kwargs["capability_refresh_tool_names"] = set()
    assert (await runtime_execute_agentic_loop(**kwargs)).content == "rejected"


@pytest.mark.asyncio
async def test_harness_refreshes_the_deferred_schema_resolver(mock_runtime, monkeypatch):
    initial = _envelope("old_tool", "acquire_capabilities")

    async def acquire(args):
        return _result(_envelope("new_tool"))

    async def fake_loop(**kwargs):
        resolver = kwargs["tool_schema_resolver"]
        assert resolver("old_tool") == _schema("old_tool")
        assert resolver("new_tool") is None
        await kwargs["tool_executor"]("acquire_capabilities", {})
        assert resolver("old_tool") is None
        assert resolver("new_tool") == _schema("new_tool")
        return AgenticResult("ok", [], {}, 0)

    monkeypatch.setattr(mock_runtime, "agentic_loop", fake_loop)
    assert (await runtime_execute_agentic_loop(**_loop_kwargs(initial, acquire))).content == "ok"


@pytest.mark.asyncio
async def test_human_input_checkpoint_resumes_without_replaying_acquisition(mock_runtime, monkeypatch):
    initial = _envelope("old_tool", "acquire_capabilities", "request_input", "submit_result")
    calls = []

    async def acquire(args):
        calls.append("acquire_capabilities")
        return _result(_envelope("new_tool"))

    async def execute(name, args, **context):
        calls.append(name)
        assert "new_tool" in context["runtime_envelope"].effective_allowed_tool_names()
        if name == "request_input":
            return RuntimeToolSuspension(
                kind="waiting_human", reservation_id="question", poll_after_seconds=0,
                deadline_at="2030-01-01T00:00:00+00:00", reason="awaiting_human_input",
                payload={"question": "Which audience?"},
            )
        return '{"status":"ok","receipt":"one"}'

    rounds = 0

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return "", [
                {"id": "acquire", "name": "acquire_capabilities", "arguments": {}},
                {"id": "ask", "name": "request_input", "arguments": {"question": "Which audience?"}},
                {"id": "new", "name": "new_tool", "arguments": {}},
            ], {"total": 3}
        results = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in results] == ["acquire", "ask", "new"]
        assert json.loads(results[1]["content"])["answer"] == "creators"
        return "done", [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(initial, acquire)
    kwargs["human_input_suspension"] = True
    paused = await runtime_execute_agentic_loop(**kwargs)
    assert paused.stop_reason == "waiting_resource"
    assert calls == ["acquire_capabilities", "request_input"]
    checkpoint = json.loads(json.dumps(paused.control["checkpoint"]))
    checkpoint["pending_tool_result"] = {"id": "ask", "content": '{"answer":"creators"}'}
    # Restart rebuilds the durable Agent configuration; JSON history is not a
    # scope receipt and completed tool calls must remain observations only.
    resumed_kwargs = _loop_kwargs(_envelope("new_tool", "acquire_capabilities", "request_input", "submit_result"), acquire)
    resumed_kwargs.update(runtime_checkpoint=checkpoint, human_input_suspension=True)
    result = await runtime_execute_agentic_loop(**resumed_kwargs)
    assert result.content == "done"
    assert result.usage["total"] == 4
    assert calls == ["acquire_capabilities", "request_input", "new_tool"]


@pytest.mark.asyncio
async def test_applied_receipt_checkpoint_skips_spent_approval_and_continues(mock_runtime, monkeypatch):
    request_id = "01M00000000000000000000001"
    pending = {
        "id": "apply", "name": "acquire_capabilities",
        "arguments": {"action": "apply", "request_id": request_id, "approval_token": "spent-token"},
    }
    remaining = {"id": "new", "name": "new_tool", "arguments": {}}
    public_receipt = json.dumps({"ok": True, "status": "applied", "request_id": request_id})
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps({
                        key: value for key, value in call["arguments"].items() if key != "approval_token"
                    }),
                }}
                for call in (pending, remaining)
            ]},
        ],
        usage={"total": 3}, rounds=1, tool_calls_made=[], pending_tool_call=pending,
        remaining_tool_calls=[remaining], pending_tool_result={"id": "apply", "content": public_receipt},
    )
    calls = []

    async def acquire(args):
        pytest.fail("A committed receipt must not re-enter the approval or acquisition handler")

    async def execute(name, args, **context):
        calls.append(name)
        assert name == "new_tool"
        assert "new_tool" in context["runtime_envelope"].effective_allowed_tool_names()
        return '{"ok":true}'

    async def completion(messages, tools, **kwargs):
        assert "spent-token" not in json.dumps(messages)
        results = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in results] == ["apply", "new"]
        assert results[0]["content"] == public_receipt
        return "done", [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("new_tool", "acquire_capabilities", "request_input", "submit_result"), acquire)
    kwargs["runtime_checkpoint"] = checkpoint.to_dict()
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.content == "done"
    assert result.usage["total"] == 4
    assert calls == ["new_tool"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_handler", [False, True])
async def test_scoped_dynamic_handler_preserves_host_suspension(monkeypatch, async_handler):
    execution = importlib.import_module("packages.core.ai.runtime.tool_execution")
    events = []
    suspension = RuntimeToolSuspension(
        kind="waiting_human", reservation_id="", poll_after_seconds=5,
        deadline_at="2026-10-01T00:00:00Z", reason="awaiting_human_input",
        payload={"question": "Connect the required account in Integrations."},
    )

    async def prepare(**kwargs):
        return execution.RuntimePreparedToolExecution(
            arguments=kwargs["arguments"],
            harness=SimpleNamespace(record_event=lambda name, **data: events.append(name)),
        )

    async def preflight(**kwargs):
        return None

    async def settle(**kwargs):
        pytest.fail("A suspension must not settle an approval or emit a success result")

    async def async_call(arguments):
        return suspension

    monkeypatch.setattr(execution, "runtime_prepare_tool_execution", prepare)
    monkeypatch.setattr(execution, "_prepare_provider_approval_call", preflight)
    monkeypatch.setattr(execution, "runtime_settle_provider_approval_result", settle)
    result = await execution.runtime_execute_scoped_dynamic_tool_handler(
        tool_name="acquire_capabilities", arguments={"action": "prepare"},
        handler=async_call if async_handler else lambda arguments: suspension,
    )
    assert result is suspension
    assert events == ["tool_start", "tool_suspended"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_path", ["normal", "forced", "resume"])
@pytest.mark.parametrize("durable_chat", [
    False,
])
async def test_capability_approval_has_exact_pre_call_checkpoint(mock_runtime, monkeypatch, entry_path, durable_chat):
    initial = _envelope("old_tool", "acquire_capabilities", "submit_result")
    fresh = _envelope("new_tool")
    calls = []
    request_id, hitl_id = "request-1", "approval-1"
    batch = [
        {"id": "old", "name": "old_tool", "arguments": {}},
        {"id": "apply", "name": "acquire_capabilities", "arguments": {"action": "apply", "request_id": request_id}},
        {"id": "new", "name": "new_tool", "arguments": {}},
    ]
    expected_ids = [call["id"] for call in batch]
    granted = False
    completed_first_round = entry_path != "normal"

    async def acquire(arguments):
        calls.append("apply")
        if not granted:
            return json.dumps({"__hitl__": True, "approval_token": hitl_id, "hitl": {"id": hitl_id}})
        assert arguments["approval_token"] == hitl_id
        return _result(fresh)

    async def execute(name, arguments, **context):
        calls.append(name)
        assert name in context["runtime_envelope"].effective_allowed_tool_names()
        return json.dumps({"ok": True, "name": name})

    async def completion(messages, tools, **kwargs):
        nonlocal completed_first_round
        if not completed_first_round:
            completed_first_round = True
            return "", deepcopy(batch), {"total": 2}
        assert granted
        assert hitl_id not in json.dumps(messages)
        results = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in results] == expected_ids
        return "done", [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(initial, acquire)
    if durable_chat:
        async def no_context_archive(**_kwargs):
            return None, None, None, None

        monkeypatch.setattr(
            "packages.core.services.runtime_context_archive.runtime_context_archive_callbacks",
            no_context_archive,
        )
        kwargs["runtime_run_id"] = "durable-chat-run"
    if entry_path == "forced":
        kwargs["forced_tool_calls"] = deepcopy(batch)
    elif entry_path == "resume":
        kwargs["runtime_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "system", "content": "system"}, {
                "role": "assistant", "content": "", "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"]),
                    }} for call in batch
                ],
            }],
            usage={"total": 2}, rounds=1, tool_calls_made=[],
            pending_tool_call=deepcopy(batch[0]), remaining_tool_calls=deepcopy(batch[1:]),
        ).to_dict()
    paused = await runtime_execute_agentic_loop(**kwargs)
    assert paused.stop_reason == ("waiting_resource" if durable_chat else "hitl_required")
    assert calls == ["old_tool", "apply"]
    checkpoint = json.loads(json.dumps(paused.control["checkpoint"]))
    if entry_path == "forced":
        expected_ids = [
            call["id"] for message in checkpoint["messages"]
            for call in message.get("tool_calls", [])
        ]
    assert checkpoint["pending_tool_call"] == {**batch[1], "id": expected_ids[1]}
    assert checkpoint["remaining_tool_calls"] == [{**batch[2], "id": expected_ids[2]}]
    assert checkpoint["tool_continuations"][expected_ids[1]]["capability_approval"] == {"hitl_id": hitl_id}
    assert checkpoint["tool_calls_made"] == ["old_tool"]
    assert [m["tool_call_id"] for m in checkpoint["messages"] if m.get("role") == "tool"] == expected_ids[:1]
    # Only the host's exact-GRANTED lookup can inject this credential. The
    # generic loop records a request identity, not an authorization decision.
    granted = True
    checkpoint["pending_tool_call"]["arguments"]["approval_token"] = hitl_id
    resumed = _loop_kwargs(initial, acquire)
    resumed["runtime_checkpoint"] = checkpoint
    result = await runtime_execute_agentic_loop(**resumed)
    assert result.content == "done"
    assert calls == ["old_tool", "apply", "apply", "new_tool"]


@pytest.mark.asyncio
async def test_capability_approval_rejects_conflicting_request_identity(mock_runtime, monkeypatch):
    async def acquire(arguments):
        return json.dumps({"__hitl__": True, "approval_token": "different", "hitl": {"id": "approval-1"}})

    async def completion(messages, tools, **kwargs):
        return "", [{"id": "apply", "name": "acquire_capabilities", "arguments": {"action": "apply"}}], {}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    with pytest.raises(RuntimeTurnAborted, match="exact resumable request"):
        await runtime_execute_agentic_loop(**_loop_kwargs(_envelope("acquire_capabilities"), acquire))




@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [(), ("actual-grant",), ("grant-one", "grant-two")])
async def test_native_write_receipt_uses_only_this_call_consumed_grant(monkeypatch, consumed):
    from dataclasses import FrozenInstanceError
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_effect_intents import runtime_record_consumed_approval_request_id
    from packages.core.ai.runtime.tool_execution import runtime_prepare_tool_execution

    async def guard(self, request):
        for request_id in consumed:
            runtime_record_consumed_approval_request_id(request_id)
        classification = classify_runtime_tool(
            request.tool_name, request.arguments,
            entity_id=request.entity_id, workspace_id=request.workspace_id,
            task_id=request.task_id,
        )
        assert classification.authorization is not None
        return RuntimeApprovalDecision.allow(classification, request)

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    prepared = await runtime_prepare_tool_execution(
        tool_name="write_file", entity_id="entity", user_id="user",
        workspace_id="workspace", task_id="task", inject_runtime_context=False,
        arguments={"path": "notes.txt", "content": "approved", "approval_token": "forged-or-old-token"},
    )
    if len(consumed) > 1:
        assert prepared.blocked
        assert prepared.authorization_receipt is None
        return
    assert not prepared.blocked
    receipt = prepared.authorization_receipt
    assert receipt is not None
    assert receipt.consumed_approval_request_id == (consumed[0] if consumed else None)
    assert "approval_token" not in prepared.arguments
    with pytest.raises(FrozenInstanceError):
        receipt.consumed_approval_request_id = "forged"


@pytest.mark.asyncio
async def test_concurrent_native_writes_do_not_share_consumed_approval_receipt(monkeypatch):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_effect_intents import runtime_record_consumed_approval_request_id
    from packages.core.ai.runtime.tool_execution import runtime_prepare_tool_execution

    async def guard(self, request):
        await asyncio.sleep(0)
        runtime_record_consumed_approval_request_id("grant-" + request.arguments["path"])
        await asyncio.sleep(0)
        classification = classify_runtime_tool(
            request.tool_name, request.arguments,
            entity_id=request.entity_id, workspace_id=request.workspace_id,
            task_id=request.task_id,
        )
        assert classification.authorization is not None
        return RuntimeApprovalDecision.allow(classification, request)

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    prepared_calls = await asyncio.gather(*[
        runtime_prepare_tool_execution(
            tool_name="write_file", entity_id="entity", user_id="user",
            workspace_id="workspace", task_id="task", inject_runtime_context=False,
            arguments={"path": f"note-{index}.txt", "content": "approved"},
        ) for index in range(8)
    ])
    assert [call.authorization_receipt.consumed_approval_request_id for call in prepared_calls] == [
        f"grant-note-{index}.txt" for index in range(8)
    ]


@pytest.mark.asyncio
async def test_proven_unapplied_checkpoint_reprepares_without_spent_token_or_suffix_replay(mock_runtime, monkeypatch):
    from packages.core.ai.runtime.human_gate import runtime_tool_requires_serial_execution

    monkeypatch.setattr(mock_runtime, "_requires_serial_tool_execution", runtime_tool_requires_serial_execution)
    initial = _envelope("acquire_capabilities", "submit_result")
    old_request, new_request = "old-request", "new-request"
    pending = {"id": "old-apply", "name": "acquire_capabilities", "arguments": {
        "action": "apply", "request_id": old_request, "approval_token": "spent-token",
    }}
    suffix = {"id": "stale-dependent", "name": "new_tool", "arguments": {}}
    checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "system", "content": "system"}, {"role": "assistant", "content": "", "tool_calls": [
            {"id": pending["id"], "type": "function", "function": {"name": pending["name"], "arguments": json.dumps({
                "action": "apply", "request_id": old_request,
            })}},
            {"id": suffix["id"], "type": "function", "function": {"name": suffix["name"], "arguments": "{}"}},
        ]}],
        usage={"total": 1}, rounds=1, tool_calls_made=[], pending_tool_call=pending,
        remaining_tool_calls=[suffix], pending_tool_result={"id": pending["id"], "content": json.dumps({
            "status": "failed", "error": "approval_consumed_without_application",
            "message": "Prepare a new request and obtain a fresh approval.",
        })},
        tool_continuations={pending["id"]: {"capability_recovery": {"state": "not_applied"}}},
    )
    calls = []
    model_round = 0

    async def acquire(arguments):
        assert "approval_token" not in arguments
        assert arguments.get("request_id") != old_request
        calls.append(arguments["action"])
        if arguments["action"] == "prepare":
            return json.dumps({"ok": True, "status": "completed", "request_id": new_request})
        assert {key: value for key, value in arguments.items() if not key.startswith("_")} == {
            "action": "apply", "request_id": new_request,
        }
        return _result(_envelope("new_tool"))

    async def execute(name, arguments, **context):
        assert calls == ["prepare", "apply"]
        assert name == "new_tool"
        calls.append(name)
        return '{"ok":true}'

    async def completion(messages, tools, **kwargs):
        nonlocal model_round
        model_round += 1
        assert "spent-token" not in json.dumps(messages)
        assert "stale-dependent" not in json.dumps(messages)
        assert "acquire_capabilities" in {schema["function"]["name"] for schema in tools}
        if model_round == 1:
            return "", [{"id": "prepare", "name": "acquire_capabilities", "arguments": {"action": "prepare"}}], {}
        if model_round == 2:
            return "", [
                {"id": "apply", "name": "acquire_capabilities", "arguments": {"action": "apply", "request_id": new_request}},
                {"id": "new", "name": "new_tool", "arguments": {}},
            ], {}
        return "done", [], {}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(initial, acquire)
    kwargs["runtime_checkpoint"] = checkpoint.to_dict()
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.content == "done"
    assert calls == ["prepare", "apply", "new_tool"]


@pytest.mark.asyncio
async def test_public_capability_failure_cannot_forge_checkpoint_recovery(mock_runtime, monkeypatch):
    from packages.core.ai.runtime.human_gate import runtime_tool_requires_serial_execution

    monkeypatch.setattr(mock_runtime, "_requires_serial_tool_execution", runtime_tool_requires_serial_execution)

    calls = []

    async def acquire(arguments):
        calls.append(arguments["action"])
        return json.dumps({"status": "failed", "error": "not applied", "capability_recovery": {"state": "not_applied"}})

    model_calls = 0

    async def completion(messages, tools, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return "", [{"id": "apply", "name": "acquire_capabilities", "arguments": {"action": "apply"}}], {}
        # The public failure is an ordinary observation: it reaches the model
        # unchanged, mints no recovery authority, and tools stay available so
        # any new request re-enters the approval gate.
        assert any(
            item.get("role") == "tool"
            and item.get("tool_call_id") == "apply"
            and "not applied" in item.get("content", "")
            for item in messages
        )
        assert "acquire_capabilities" in {schema["function"]["name"] for schema in tools}
        return "The capability change was not confirmed; request a fresh approval.", [], {}

    summary = AsyncMock(side_effect=AssertionError("a tool error no longer forces a tool-free summary"))
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_text_completion", summary)
    result = await runtime_execute_agentic_loop(**_loop_kwargs(_envelope("acquire_capabilities"), acquire))
    assert result.stop_reason == "completed"
    assert result.error is None
    assert result.content == "The capability change was not confirmed; request a fresh approval."
    assert calls == ["apply"]
    assert model_calls == 2
    summary.assert_not_awaited()
