"""Malformed provider arguments remain repairable before any tool side effect."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.llm_client import _parse_tool_arguments
from packages.core.ai.runtime.tool_input_validation import (
    runtime_tool_input_validation_result,
)
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.ai.tool_pool import ToolPool
import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint


pytestmark = pytest.mark.unit


def _schema(*, strict=True):
    return {
        "type": "object",
        "properties": {"operation": {"type": "string"}, "files": {"type": "object"}},
        "required": ["operation"],
        "additionalProperties": not strict,
    }


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("raw", [
    '{"operation":"write_files","files":{"index.html":"secret-value',
    '{"operation":"write_files",\n "files": {"index.html":"bad\nsecret-value"}}',
    '{"operation":"write_files","files":{"index.html":"secret-value"},}',
])
def test_parse_failure_explains_syntax_without_echoing_content(strict, raw):
    result = runtime_tool_input_validation_result(
        tool_name="video_edit", arguments=_parse_tool_arguments(raw),
        tool_schema=_schema(strict=strict),
    )
    assert isinstance(result, RuntimePreflightValidationError)
    payload = json.loads(result)
    assert payload["details"]["schema_rule"] == "json"
    assert payload["details"]["path"] == "$"
    assert payload["contract"] == "tool_error"
    assert payload["details"]["expected"] == "JSON object"
    assert payload["details"]["line"] >= 1 and payload["details"]["column"] >= 1
    assert payload["details"]["parser_reason"]
    assert "line " in payload["message"] and "column " in payload["message"]
    assert "Nothing was executed" in payload["message"]
    assert "complete JSON object" in payload["message"]
    assert "files, operation" in payload["message"]
    assert "secret-value" not in result


def test_schema_declaring_raw_is_not_mistaken_for_parser_envelope():
    assert runtime_tool_input_validation_result(
        tool_name="raw_text", arguments={"_raw": "not json"},
        tool_schema={"type": "object", "properties": {"_raw": {"type": "string"}}},
    ) is None


@pytest.mark.parametrize("schema", [None, {"type": "invalid"}])
def test_missing_or_invalid_schema_keeps_infrastructure_error(schema):
    result = runtime_tool_input_validation_result(
        tool_name="video_edit", arguments=_parse_tool_arguments('{"files":'),
        tool_schema=schema,
    )
    assert not isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["error"].startswith("tool_schema_")


def test_valid_arguments_preserve_file_content_and_schema_validation():
    source = '<script>const value = "quoted";</script>\n'
    arguments = {"operation": "write_files", "files": {"index.html": source}}
    parsed = _parse_tool_arguments(json.dumps(arguments))
    assert parsed == arguments
    assert runtime_tool_input_validation_result(
        tool_name="video_edit", arguments=parsed, tool_schema=_schema(),
    ) is None
    # Parsing valid JSON is not permission to unwrap and execute model input.
    wrapped = runtime_tool_input_validation_result(
        tool_name="video_edit", arguments={"_raw": json.dumps(arguments)}, tool_schema=_schema(),
    )
    wrapped_payload = json.loads(wrapped)
    assert wrapped_payload["details"]["schema_rule"] == "required"
    assert wrapped_payload["details"]["expected"] == "operation"
    assert source not in wrapped


@pytest.mark.parametrize("arguments, actual_type", [
    ([], "array"), ("text", "string"), (None, "null"), (False, "boolean"),
])
def test_non_object_arguments_are_rejected_without_coercion(arguments, actual_type):
    result = runtime_tool_input_validation_result(
        tool_name="video_edit", arguments=arguments,
        tool_schema=_schema(strict=False),
    )
    assert isinstance(result, RuntimePreflightValidationError)
    payload = json.loads(result)
    assert payload["details"]["schema_rule"] == "type"
    assert payload["details"]["path"] == "$"
    assert payload["details"]["expected"] == "object"
    assert actual_type in payload["message"]
    assert "Nothing was executed" in payload["message"]


def test_required_field_error_reports_constraint_without_argument_values():
    secret = "do-not-echo-secret"
    result = runtime_tool_input_validation_result(
        tool_name="video_edit", arguments={"files": {"index.html": secret}},
        tool_schema=_schema(strict=True),
    )
    assert isinstance(result, RuntimePreflightValidationError)
    payload = json.loads(result)
    assert payload["details"]["schema_rule"] == "required"
    assert payload["details"]["expected"] == "operation"
    assert payload["details"]["path"] == "$"
    assert secret not in result


@pytest.mark.parametrize("rule, constraint, invalid, expected", [
    ("enum", ["draft", "publish"], "secret-instance", ["draft", "publish"]),
    ("const", "draft", "secret-instance", "draft"),
    ("minimum", 1, 0, 1),
    ("maximum", 10, 11, 10),
    ("pattern", "^[a-z]+$", "secret-instance-123", "^[a-z]+$"),
])
def test_schema_constraint_is_reported_without_echoing_invalid_value(
    rule, constraint, invalid, expected,
):
    value_type = "integer" if rule in {"minimum", "maximum"} else "string"
    result = runtime_tool_input_validation_result(
        tool_name="publish", arguments={"mode": invalid},
        tool_schema={"type": "object", "properties": {
            "mode": {"type": value_type, rule: constraint},
        }},
    )
    assert isinstance(result, RuntimePreflightValidationError)
    payload = json.loads(result)
    assert payload["details"]["schema_rule"] == rule
    assert payload["details"]["path"] == "$.mode"
    assert payload["details"]["expected"] == expected
    assert "secret-instance" not in result


def test_sensitive_schema_constraint_is_redacted():
    result = runtime_tool_input_validation_result(
        tool_name="login", arguments={"api_key": "wrong-value"},
        tool_schema={"type": "object", "properties": {
            "api_key": {"const": "provider-secret-value"},
        }},
    )
    payload = json.loads(result)
    assert payload["details"]["expected"] == "<redacted>"
    assert "wrong-value" not in result
    assert "provider-secret-value" not in result


@pytest.mark.parametrize("strict", [True, False])
async def test_malformed_arguments_never_reach_authorization_or_handler(monkeypatch, strict):
    pool = ToolPool()
    handler = AsyncMock(side_effect=AssertionError("must not execute"))
    pool.register("video_edit", {"type": "function", "function": {
        "name": "video_edit", "parameters": _schema(strict=strict),
    }}, handler)
    dispatch = AsyncMock(side_effect=AssertionError("must reject before approval/dispatch"))
    monkeypatch.setattr("packages.core.ai.tool_pool.runtime_execute_registered_tool", dispatch)
    result = await pool.execute(
        "video_edit", _parse_tool_arguments('{"operation":"write_files","files":'),
        entity_id="entity", user_id="user",
    )
    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["details"]["schema_rule"] == "json"
    handler.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
@pytest.mark.parametrize("invalid", [{"_raw": '{"operation":"write_files","files":'}, [], "text", None, False])
async def test_custom_executor_corrects_syntax_inside_same_loop(monkeypatch, entry, invalid):
    """Host controls bypass ToolPool, but must not execute malformed arguments."""
    schema = {"type": "function", "function": {"name": "video_edit", "parameters": _schema()}}
    batch = [
        {"id": "invalid", "name": "video_edit", "arguments": invalid},
        {"id": "suffix", "name": "video_edit", "arguments": {"operation": "must-not-execute"}},
    ]
    executed, checkpoints, callbacks = [], [], []
    model_count = 0

    async def execute(name, arguments):
        assert arguments == {"operation": "status", "_runtime_tool_call_id_from_context": "corrected"}
        executed.append(name)
        return '{"status":"active"}'

    async def model(messages, tools, **kwargs):
        nonlocal model_count
        model_count += 1
        if entry == "normal" and model_count == 1:
            return "", batch, {}
        observations = [m["content"] for m in messages if m.get("role") == "tool"]
        if not executed:
            assert "Nothing was executed" in observations[-1]
            assert "must-not-execute" not in json.dumps(messages)
            return "", [{"id": "corrected", "name": "video_edit", "arguments": {"operation": "status"}}], {}
        return "done", [], {}

    def checkpoint(rounds, messages):
        checkpoints.append(json.loads(json.dumps(messages)))

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    options = {}
    if entry == "forced":
        options["forced_tool_calls"] = batch
    if entry == "resume":
        options["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in batch
            ]}], usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call=batch[0], remaining_tool_calls=batch[1:],
        ).to_dict()
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="continue", tools=[schema],
        tool_executor=execute, max_rounds=4, on_round_end=checkpoint,
        on_tool_end=lambda name, result, *args: callbacks.append(str(result)), **options,
    )
    assert result.error is None
    assert result.content == "done"
    assert executed == ["video_edit"]
    assert any("Nothing was executed" in value for value in callbacks)
    assert any("Nothing was executed" in json.dumps(value) for value in checkpoints)


async def test_parallel_loops_do_not_share_syntax_repair_state(monkeypatch):
    count = 32
    entered = 0
    barrier = asyncio.Event()
    executions = []
    schema = {"type": "function", "function": {"name": "video_edit", "parameters": _schema()}}

    async def model(messages, tools, **kwargs):
        nonlocal entered
        identity = messages[1]["content"]
        observations = [m["content"] for m in messages if m.get("role") == "tool"]
        if not observations:
            entered += 1
            if entered == count:
                barrier.set()
            await barrier.wait()
            return "", [{"id": "same-id", "name": "video_edit", "arguments": {"_raw": '{"operation":'}}], {}
        if len(observations) == 1:
            assert json.loads(observations[0])["details"]["schema_rule"] == "json"
            return "", [{"id": "corrected", "name": "video_edit", "arguments": {"operation": identity}}], {}
        return identity, [], {}

    async def execute(name, arguments):
        executions.append(arguments["operation"])
        return '{"status":"active"}'

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    results = await asyncio.gather(*(
        loop_module.agentic_loop(
            system_prompt="system", user_message=f"project-{i}", tools=[schema],
            tool_executor=execute, max_rounds=4,
        ) for i in range(count)
    ))
    assert all(result.error is None for result in results)
    assert sorted(executions) == sorted(f"project-{i}" for i in range(count))
    assert [result.content for result in results] == [f"project-{i}" for i in range(count)]
