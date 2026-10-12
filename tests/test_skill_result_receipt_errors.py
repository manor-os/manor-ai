"""Skill receipt failures are diagnostics, never implicit corrective writes."""

import json

import pytest

from packages.core.ai.runtime.skills import runtime_prompt_skill_registered_tool_executor
from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult


SOURCE_RESULT = {"url": "/artifacts/result.png"}
RECEIPT_PATH = "workflow/run-1/receipt.txt"


def _executor():
    return runtime_prompt_skill_registered_tool_executor(
        entity_id="entity-1",
        skill_slug="receipt-test",
        runtime_tool_context={"_workflow_lineage_root_run_id_from_context": "run-1"},
        execution_contract={
            "artifact_scope": {
                "lineage_context_keys": ["_workflow_lineage_root_run_id_from_context"],
            },
            "result_receipts": [{
                "after_tool": "generate_image",
                "source_fields": ["url"],
                "path": "receipt.txt",
                "result_fields": {"receipt_verified": True},
            }],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("save_result", [
    json.dumps({"error": "file_already_exists"}),
    json.dumps({"status": "error", "message": "Knowledge projection failed"}),
    "Error: file save failed",
    RuntimeStructuredToolResult('{"created":true}', structured_content={
        "error": "file_already_exists", "path": RECEIPT_PATH,
    }),
])
async def test_receipt_save_error_returns_evidence_without_read_or_rewrite(monkeypatch, save_result):
    calls = []

    async def execute(name, args, **kwargs):
        calls.append((name, args))
        if name == "generate_image":
            return json.dumps(SOURCE_RESULT)
        if name == "generate_file":
            return save_result
        raise AssertionError(f"Unexpected implicit call: {name}")

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    result = json.loads(await _executor()("generate_image", {"prompt": "image"}))

    assert result["status"] == "error"
    assert result["tool"] == "generate_image"
    assert result["error"] == "result_receipt_write_failed"
    assert result["details"]["required_path"] == RECEIPT_PATH
    assert result["details"]["receipt_verified"] is False
    assert result["details"]["result"] == SOURCE_RESULT
    assert "save_result" in result["details"]
    if isinstance(save_result, RuntimeStructuredToolResult):
        assert result["details"]["save_result"] == save_result.structured_content
    assert [name for name, _ in calls] == ["generate_image", "generate_file"]


@pytest.mark.asyncio
@pytest.mark.parametrize("read_result", [
    json.dumps({"content": "another result"}),
    json.dumps({"error": "source_changed", "content": SOURCE_RESULT["url"]}),
    "unstructured read failure",
    RuntimeStructuredToolResult(json.dumps({"content": SOURCE_RESULT["url"]}), structured_content={
        "error": "source_changed", "content": "different authoritative content",
    }),
])
async def test_receipt_verification_error_never_rewrites_or_rechecks(monkeypatch, read_result):
    calls = []

    async def execute(name, args, **kwargs):
        calls.append((name, args))
        if name == "generate_image":
            return json.dumps(SOURCE_RESULT)
        if name == "generate_file":
            return json.dumps({"created": True, "path": RECEIPT_PATH})
        if name == "read_file":
            return read_result
        raise AssertionError(f"Unexpected implicit call: {name}")

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    result = json.loads(await _executor()("generate_image", {"prompt": "image"}))

    assert result["status"] == "error"
    assert result["error"] == "result_receipt_verification_failed"
    assert result["details"]["required_path"] == RECEIPT_PATH
    assert result["details"]["receipt_verified"] is False
    assert result["details"]["expected"] == SOURCE_RESULT["url"]
    assert result["details"]["result"] == SOURCE_RESULT
    assert "read_result" in result["details"]
    if isinstance(read_result, RuntimeStructuredToolResult):
        assert result["details"]["read_result"] == read_result.structured_content
    assert [name for name, _ in calls] == ["generate_image", "generate_file", "read_file"]


@pytest.mark.asyncio
async def test_failed_receipt_does_not_suppress_a_later_explicit_model_write(monkeypatch):
    calls = []
    write_result = json.dumps({"status": "waiting_human", "error": "approval required"})

    async def execute(name, args, **kwargs):
        calls.append((name, args))
        if name == "generate_image":
            return json.dumps(SOURCE_RESULT)
        if name == "generate_file":
            return json.dumps({"created": True})
        if name == "read_file":
            return json.dumps({"content": "different content"})
        if name == "write_file":
            return write_result
        raise AssertionError(name)

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    executor = _executor()
    failed = json.loads(await executor("generate_image", {"prompt": "image"}))
    assert failed["status"] == "error"
    assert [name for name, _ in calls] == ["generate_image", "generate_file", "read_file"]

    assert await executor("write_file", {
        "path": RECEIPT_PATH, "content": SOURCE_RESULT["url"],
    }) == write_result
    assert [name for name, _ in calls] == ["generate_image", "generate_file", "read_file", "write_file"]
    assert calls[-1][1]["path"] == RECEIPT_PATH
    assert calls[-1][1]["content"] == SOURCE_RESULT["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("control_tool", ["generate_file", "read_file"])
@pytest.mark.parametrize("control_result", [
    json.dumps({"status": "waiting_human", "error": "approval required"}),
    RuntimeStructuredToolResult("display only", structured_content={
        "status": "blocked", "error": "access denied",
    }),
])
async def test_receipt_control_result_is_preserved_without_followup(monkeypatch, control_tool, control_result):
    calls = []

    async def execute(name, args, **kwargs):
        calls.append(name)
        if name == control_tool:
            return control_result
        if name == "generate_image":
            return json.dumps(SOURCE_RESULT)
        if name == "generate_file":
            return json.dumps({"created": True})
        raise AssertionError(f"Unexpected implicit call: {name}")

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    assert await _executor()("generate_image", {"prompt": "image"}) is control_result
    assert calls == (["generate_image", "generate_file"] if control_tool == "generate_file"
                     else ["generate_image", "generate_file", "read_file"])
