"""Real ToolPool schema failures carry host-only, pre-dispatch recovery proof."""
from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.dynamic_mcp import (
    RuntimeDynamicMCPFailureResultFactory,
    RuntimeDynamicMCPRehydrationResult,
    RuntimeDynamicMCPRehydrationStatus,
    RuntimeDynamicMCPToolBinding,
)
from packages.core.ai.runtime.nested_agent import (
    runtime_nested_checkpoint_scope,
    runtime_nested_skill_scope,
    runtime_nested_skill_state,
)
from packages.core.ai.runtime.tool_effect_intents import (
    runtime_tool_effect_checkpoint_scope,
    runtime_tool_effect_intent_scope,
    runtime_tool_effect_result_scope,
)
from packages.core.ai.runtime.tool_input_validation import runtime_tool_input_validation_result
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA
from packages.core.contracts.step_execution import current_step_effects, step_execution_scope


pytestmark = pytest.mark.unit
VALID = {"kind": "document", "name": "report.md", "content": "body"}
INVALID = {"name": "report.md", "content": "body"}
WRITE = {"delivery": "artifact", "effects": "artifact_write"}
MCP_TOOL = "mcp__test__create_file"


async def _allow(self, request):
    return RuntimeApprovalDecision.allow(classify_runtime_tool(request.tool_name, request.arguments), request)


def _call(arguments, call_id="write"):
    return {"id": call_id, "name": "generate_file", "arguments": arguments}


def _binding(schema):
    return RuntimeDynamicMCPRehydrationResult(
        status=RuntimeDynamicMCPRehydrationStatus.BOUND,
        tool_name=MCP_TOOL, provider="test",
        binding=RuntimeDynamicMCPToolBinding(
            provider="test", action="create_file", schema=schema,
            expires_at=999999999.0, effect="write",
        ),
    )


@pytest.mark.parametrize("arguments,rule", [
    (INVALID, "required"),
    ({**VALID, "params": "{}"}, "type"),
    ({**VALID, "kind": "not-a-kind"}, "enum"),
    ({**VALID, "unexpected": "must-not-leak"}, "additionalProperties"),
])
def test_only_real_instance_validation_errors_receive_host_marker(arguments, rule):
    schema = copy.deepcopy(GENERATE_FILE_SCHEMA)
    schema["function"]["parameters"]["additionalProperties"] = False
    result = runtime_tool_input_validation_result(
        tool_name="generate_file", arguments=arguments, tool_schema=schema,
    )
    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["details"]["schema_rule"] == rule
    assert "must-not-leak" not in result
    assert isinstance(copy.deepcopy(result), RuntimePreflightValidationError)
    assert not isinstance(str(result), RuntimePreflightValidationError)
    assert not isinstance(json.loads(json.dumps(result)), RuntimePreflightValidationError)


@pytest.mark.parametrize("schema", [None, {"type": "not-a-json-schema-type"}])
def test_missing_or_broken_schema_does_not_claim_model_can_correct_it(schema):
    result = runtime_tool_input_validation_result(tool_name="tool", arguments={}, tool_schema=schema)
    assert result and not isinstance(result, RuntimePreflightValidationError)


def test_validator_infrastructure_exception_does_not_mint_correction(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("validator failed")

    monkeypatch.setattr("packages.core.ai.runtime.tool_input_validation.Draft202012Validator.iter_errors", fail)
    result = runtime_tool_input_validation_result(
        tool_name="generate_file", arguments=VALID, tool_schema=GENERATE_FILE_SCHEMA,
    )
    assert json.loads(result)["details"]["schema_rule"] is None
    assert not isinstance(result, RuntimePreflightValidationError)


@pytest.mark.parametrize("refreshed", [False, True])
async def test_dynamic_schema_rejection_is_typed_only_before_dispatch(monkeypatch, refreshed):
    schema = copy.deepcopy(GENERATE_FILE_SCHEMA)
    schema["function"]["name"] = MCP_TOOL
    earlier = copy.deepcopy(schema)
    earlier["function"]["parameters"].pop("required")
    pool = ToolPool()
    discover = AsyncMock(side_effect=[_binding(earlier), _binding(schema)] if refreshed else [_binding(schema)])
    execute = AsyncMock(return_value=RuntimeDynamicMCPFailureResultFactory.stale_preflight_binding(
        provider="test", tool_name=MCP_TOOL,
    ))
    monkeypatch.setattr(pool, "_ensure_dynamic_mcp_tool_binding", discover)
    monkeypatch.setattr("packages.core.ai.tool_pool.runtime_execute_registered_tool", execute)
    result = await pool.execute(MCP_TOOL, INVALID, entity_id="entity", user_id="user")
    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["details"]["schema_rule"] == "required"
    assert discover.await_count == (2 if refreshed else 1)
    assert execute.await_count == (1 if refreshed else 0)


@pytest.mark.parametrize("origin", ["handler", "stringified", "json_roundtrip"])
async def test_unproven_stale_result_never_refreshes_or_replays_handler(monkeypatch, origin):
    stale = RuntimeDynamicMCPFailureResultFactory.stale_preflight_binding(provider="test", tool_name=MCP_TOOL)
    result = {
        "handler": RuntimeDynamicMCPFailureResultFactory.stale_binding(provider="test", tool_name=MCP_TOOL),
        "stringified": str(stale),
        "json_roundtrip": json.loads(json.dumps(stale)),
    }[origin]
    pool = ToolPool()
    discover = AsyncMock(return_value=_binding({"type": "object"}))
    execute = AsyncMock(return_value=result)
    monkeypatch.setattr(pool, "_ensure_dynamic_mcp_tool_binding", discover)
    monkeypatch.setattr("packages.core.ai.tool_pool.runtime_execute_registered_tool", execute)
    assert await pool.execute(MCP_TOOL, {}, entity_id="entity", user_id="user") == result
    discover.assert_awaited_once()
    execute.assert_awaited_once()


async def test_repeated_invalid_schema_is_bounded_by_the_round_budget(monkeypatch):
    pool = ToolPool()
    handler = AsyncMock(side_effect=AssertionError("invalid input must never reach handler"))
    pool.register("generate_file", GENERATE_FILE_SCHEMA, handler)
    calls = 0

    async def model(messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        # Every repair round keeps the tool and sees each prior rejection.
        assert [tool["function"]["name"] for tool in tools] == ["generate_file"]
        assert len([m for m in messages if m.get("role") == "tool"]) == calls - 1
        return "", [_call(INVALID, f"invalid-{calls}")], {}

    summary = AsyncMock(return_value=("unexpected forced summary", {}))
    final = AsyncMock(return_value=("Could not correct the missing kind; no file was created.", {}))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", final)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="write", tools=[GENERATE_FILE_SCHEMA],
        tool_executor=pool.execute, max_rounds=12,
    )
    # No consecutive-error breaker: only the total round budget ends repair.
    assert calls == 12
    assert result.stop_reason == "max_rounds"
    assert result.content == "Could not correct the missing kind; no file was created."
    summary.assert_not_awaited()
    final.assert_awaited_once()
    assert len(result.tool_calls_made) == 12
    handler.assert_not_awaited()


@pytest.mark.parametrize("parallelism", [32, 64])
async def test_concurrent_schema_correction_is_isolated_between_steps(monkeypatch, parallelism):
    pool = ToolPool()
    barrier = asyncio.Event()
    entered = 0
    written = []
    intents = []

    async def handler(**kwargs):
        written.append(kwargs["name"])
        return json.dumps({"status": "completed", "fs_path": kwargs["name"]})

    async def intent(name, arguments, *args, **kwargs):
        intents.append(arguments["name"])

    async def model(messages, tools, **kwargs):
        nonlocal entered
        name = messages[1]["content"]
        results = [message for message in messages if message.get("role") == "tool"]
        if not results:
            entered += 1
            if entered == parallelism:
                barrier.set()
            await barrier.wait()
            return "", [_call({**INVALID, "name": name}, "same-invalid-id")], {}
        if len(results) == 1:
            assert json.loads(results[0]["content"])["details"]["schema_rule"] == "required"
            return "", [_call({**VALID, "name": name}, "same-corrected-id")], {}
        return name, [], {}

    async def run(index):
        with step_execution_scope(WRITE), runtime_tool_effect_intent_scope(intent):
            result = await loop_module.agentic_loop(
                system_prompt="system", user_message=f"report-{index}.md",
                tools=[GENERATE_FILE_SCHEMA], tool_executor=pool.execute, max_rounds=4,
            )
            assert result.error is None
            assert result.content == f"report-{index}.md"
        assert current_step_effects() is None

    pool.register("generate_file", GENERATE_FILE_SCHEMA, handler)
    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", _allow)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    await asyncio.gather(*(run(index) for index in range(parallelism)))
    assert sorted(written) == sorted(intents) == sorted(f"report-{index}.md" for index in range(parallelism))


async def test_nested_skill_checkpoint_keeps_schema_error_without_replaying_parent(monkeypatch):
    class Crash(BaseException):
        pass

    pool = ToolPool()
    saved, writes, intents, receipts = [], [], [], []
    crashed = False
    parent = RuntimeAgentCheckpoint(
        messages=[{"role": "system", "content": "parent"}], usage={}, rounds=1,
        tool_calls_made=[], pending_tool_call={"id": "parent", "name": "invoke_skill", "arguments": {"skill_id": "skill"}},
    ).to_dict()

    async def persist(tree, messages):
        nonlocal crashed
        saved.append(copy.deepcopy(tree))
        if not crashed:
            crashed = True
            raise Crash()

    async def handler(**kwargs):
        writes.append(kwargs["name"])
        return json.dumps({"status": "completed", "fs_path": kwargs["name"]})

    async def intent(name, arguments, call_id, *args, **kwargs):
        intents.append(call_id)

    async def receipt(name, arguments, result, call_id, classification, tree):
        receipts.append((call_id, copy.deepcopy(tree)))

    async def model(messages, tools, **kwargs):
        results = [message for message in messages if message.get("role") == "tool"]
        if not results:
            return "", [_call(INVALID, "invalid")], {}
        if len(results) == 1:
            assert "tool_input_validation_failed" in results[0]["content"]
            return "", [_call(VALID, "corrected")], {}
        return "child done", [], {}

    async def child(tree):
        with runtime_tool_effect_checkpoint_scope({"parent": tree}), runtime_nested_skill_scope("skill") as frame:
            # The runtime harness restores serialized evidence before invoking
            # the raw loop; exercise that same boundary in this loop-level test.
            loop_kwargs = frame.loop_kwargs()
            evidence = RuntimeToolEvidence()
            evidence.restore_checkpoint_state(loop_kwargs.pop("initial_tool_evidence", {}))
            return await loop_module.agentic_loop(
                system_prompt="child", user_message="write", tools=[GENERATE_FILE_SCHEMA],
                tool_executor=pool.execute, max_rounds=5,
                on_round_end=frame.on_round_end, on_round_end_strict=True,
                tool_evidence=evidence, **loop_kwargs,
            )

    pool.register("generate_file", GENERATE_FILE_SCHEMA, handler)
    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", _allow)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    with (
        step_execution_scope(WRITE), runtime_nested_checkpoint_scope(persist),
        runtime_tool_effect_intent_scope(intent), runtime_tool_effect_result_scope(receipt),
    ):
        with pytest.raises(Crash):
            await child(parent)
        assert writes == intents == []
        state = runtime_nested_skill_state(saved[-1])
        assert state["kind"] == "transcript"
        assert saved[-1]["pending_tool_call"]["id"] == "parent"
        result = await child(json.loads(json.dumps(saved[-1])))
    assert result.error is None and result.content == "child done"
    assert writes == ["report.md"]
    assert len(intents) == 1 and intents[0].startswith("nested_")
    assert receipts[0][0] == intents[0]
    assert receipts[0][1]["pending_tool_call"]["id"] == "parent"
