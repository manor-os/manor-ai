"""Office preparation and failed commands must retain their actual outcomes."""
from __future__ import annotations

import copy
import json

import pytest

from packages.core.ai.runtime.streams import (
    runtime_tool_call_error,
    runtime_tool_result_for_chat,
)
from packages.core.services.assistant_blocks import (
    AssistantBlocksBuilder,
    project_browser_display_names,
    project_skill_display_names,
)


HANDOFF = json.dumps({
    "status": "sandbox_ready", "skill": "pptx", "content": "instructions" * 5000,
    "sandbox_id": "sandbox-1", "loaded_tools": ["sandbox"],
})
STDERR = "sed: can't read references/layout-contract.md: No such file or directory"
FAILED_COMMAND = "# Project - Design Spec\n" + "Reference content\n" * 3000 + "[stderr]\n" + STDERR + "\n[exit_code: 2]"


def test_office_handoff_remains_parseable_before_chat_preview_truncation():
    result = runtime_tool_result_for_chat("generate_file", HANDOFF)
    assert json.loads(result) == {"status": "sandbox_ready", "skill": "pptx"}
    assert "instructions" not in result


def test_office_preparation_is_not_displayed_as_a_generated_file():
    builder = AssistantBlocksBuilder()
    args = {"kind": "presentation", "name": "detective.pptx"}
    builder.start_tool("generate_file", args)
    builder.end_tool("generate_file", result=runtime_tool_result_for_chat("generate_file", HANDOFF))
    step = builder.blocks()[0]["steps"][0]
    assert step["status"] == "success"
    assert step["display_key"] == "component.assistant_process.tool.skill.prepare"
    assert step["display_params"] == {"target": "PPTX"}


def test_generated_file_receipt_keeps_generation_presentation():
    builder = AssistantBlocksBuilder()
    args = {"kind": "image", "name": "cover.png"}
    builder.start_tool("generate_file", args)
    builder.end_tool("generate_file", result=json.dumps({"status": "completed", "document_id": "doc-1"}))
    step = builder.blocks()[0]["steps"][0]
    assert step["display_key"] == "component.assistant_process.tool.media.generate"
    assert step["display_params"] == {"target": "cover.png"}


@pytest.mark.parametrize("name,args", [
    ("sandbox", {"action": "exec", "params": {"command": "sed"}}),
    ("sandbox_exec", {"command": "sed"}),
])
def test_failed_sandbox_preview_prioritizes_stderr_over_long_stdout(name, args):
    expected = f"Command exited with status 2: {STDERR}"
    assert runtime_tool_call_error(FAILED_COMMAND) == expected
    assert runtime_tool_result_for_chat(name, FAILED_COMMAND) == expected
    builder = AssistantBlocksBuilder()
    builder.start_tool(name, args)
    builder.end_tool(name, result=FAILED_COMMAND, status="error")
    step = builder.blocks()[0]["steps"][0]
    assert step["status"] == "error"
    assert step["result_preview"] == expected
    assert "Reference content" not in step["result_preview"]


def test_historical_handoff_and_failed_preview_use_exact_receipts():
    args = {"kind": "presentation", "name": "detective.pptx"}
    sandbox_args = {"action": "exec", "params": {"command": "sed"}}
    blocks = [{"id": "p", "type": "process", "status": "error", "steps": [
        {"id": "prep", "name": "generate_file", "status": "success", "display_key": "component.assistant_process.tool.media.generate", "result_preview": "old preview"},
        {"id": "failed", "name": "sandbox", "status": "error", "result_preview": "# Project - Design Spec"},
    ]}]
    calls = [
        {"name": "generate_file", "arguments": args, "raw_result": HANDOFF},
        {"name": "sandbox", "arguments": sandbox_args, "raw_result": FAILED_COMMAND},
    ]
    original = copy.deepcopy(blocks)
    result = project_skill_display_names(blocks, calls)
    assert result[0]["steps"][0]["display_key"] == "component.assistant_process.tool.skill.prepare"
    assert result[0]["steps"][1]["result_preview"] == f"Command exited with status 2: {STDERR}"
    assert [step["status"] for step in result[0]["steps"]] == ["success", "error"]
    assert blocks == original
    assert project_skill_display_names(blocks, list(reversed(calls))) == original


def test_stdout_failure_and_successful_command_preserve_existing_semantics():
    assert runtime_tool_call_error("Compiler failed\n[exit_code: 1]") == "Command exited with status 1: Compiler failed"
    assert runtime_tool_result_for_chat("sandbox", "Reference content\n[exit_code: 0]") == "Reference content\n[exit_code: 0]"


def test_projection_tolerates_historical_non_dict_arguments():
    arguments = '{"path":"notes.md"}'
    builder = AssistantBlocksBuilder()
    builder.end_tool("read_file", arguments=arguments, result="Read notes", status="success")
    blocks = builder.blocks()
    calls = [{"name": "read_file", "arguments": arguments, "raw_result": "Read notes"}]
    assert project_skill_display_names(blocks, calls) == blocks
    assert blocks[0]["steps"][0]["result_preview"] == "Read notes"


@pytest.mark.parametrize("steps", [None, {}, "legacy steps"])
def test_projection_tolerates_historical_non_list_step_fields(steps):
    blocks = [{"type": "text", "text": "Recorded answer", "steps": steps}]
    assert project_skill_display_names(blocks, []) == blocks


@pytest.mark.parametrize("result", [None, ""])
def test_missing_sandbox_receipt_preserves_existing_preview(result):
    args = {"command": "sed"}
    builder = AssistantBlocksBuilder()
    builder.end_tool("sandbox_exec", arguments=args, result=result, status="error")
    assert "result_preview" not in builder.blocks()[0]["steps"][0]
    blocks = [{"type": "process", "steps": [{
        "name": "sandbox_exec", "status": "error", "result_preview": "Previous failure evidence",
    }]}]
    calls = [{"name": "sandbox_exec", "arguments": args, "raw_result": result}]
    assert project_skill_display_names(blocks, calls) == blocks


@pytest.mark.parametrize("call", [
    {"raw_result": "Read failed"},
    {"name": None, "raw_result": "Read failed"},
], ids=["missing_name", "null_name"])
def test_historical_diagnostics_preserve_blocks_for_invalid_receipt_names(call):
    blocks = [{"type": "process", "steps": [{
        "name": None, "status": "error", "result_preview": "Earlier diagnostic",
    }]}]
    original = copy.deepcopy(blocks)
    assert project_browser_display_names(blocks, [call]) is blocks
    assert blocks == original
