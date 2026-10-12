"""One error classifier, with separate projections for waits and decisions."""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.control import RuntimeToolSuspension, RuntimeTurnAborted
from packages.core.ai.runtime.streams import (
    runtime_tool_call_error,
    runtime_tool_call_outcome,
    runtime_tool_path_memory_outcome,
    runtime_tool_status_for_chat,
)
from packages.core.ai.runtime.tool_execution import (
    RuntimePreparedToolExecution,
    _tool_end_event_data,
    runtime_execute_prepared_tool_handler,
    runtime_execute_scoped_dynamic_tool_handler,
)
from packages.core.ai.runtime.tool_results import (
    RuntimeNoEffectToolResult,
    RuntimePreflightValidationError,
    RuntimeStructuredToolResult,
)


@pytest.mark.parametrize("encode", [lambda value: value, json.dumps], ids=["dict", "json"])
@pytest.mark.parametrize("payload", [
    {"error": "invalid_request", "message": "Missing title"},
    {"error": {"code": "invalid_request", "message": "Missing title"}},
    {"status": "failed", "message": "Failed to read"},
    {"status": "failure"},
    {"status": "timeout"},
    {"ok": False, "message": "Invalid arguments"},
    {"success": False, "detail": "Invalid arguments"},
    {"isError": True, "content": [{"type": "text", "text": "Invalid arguments"}]},
    {"exit_code": 1, "stderr": "Could not compile"},
    {"exit_code": "-9", "stdout": "Killed"},
    {"status": "completed", "error": "Artifact registration failed"},
])
def test_execution_errors_have_consistent_projections(encode, payload):
    result = encode(payload)
    assert runtime_tool_call_error(result)
    assert runtime_tool_status_for_chat(result) == "error"
    assert runtime_tool_path_memory_outcome(result) == "failure"
    assert runtime_tool_call_outcome("test_tool", result) == "error"
    assert _tool_end_event_data("test_tool", result)["status"] == "failed"


@pytest.mark.parametrize("result", [
    "Error: boom", "Tool error (example): boom", "Error invoking skill 'video-edit': boom",
    "compiler failed\n[exit_code: 2]",
])
def test_legacy_text_failures_share_the_same_decoder(result):
    assert runtime_tool_call_error(result)
    assert runtime_tool_status_for_chat(result) == "error"
    assert runtime_tool_path_memory_outcome(result) == "failure"
    assert _tool_end_event_data("invoke_skill", result)["status"] == "failed"


@pytest.mark.parametrize("payload", [
    {"status": "source_updated", "source_saved": True, "contract_valid": False,
     "contract_violations": [{"file": "index.html", "rule": "local_gsap_import"}]},
    {"status": "started", "session_id": "session-1"},
    {"status": "pending", "job_id": "job-1"},
    {"status": "mystery"},
    {"status": "ok", "error": None},
    {"isError": False, "ok": True, "success": True, "exit_code": 0},
    {"isError": "false", "ok": "false", "success": 0, "exit_code": False},
    {"matches": [{"error": "This is a field in search data, not a call failure"}]},
])
def test_domain_results_and_nested_data_are_not_assumed_failed(payload):
    original = copy.deepcopy(payload)
    for result in (payload, json.dumps(payload)):
        assert runtime_tool_call_error(result) is None
        assert runtime_tool_status_for_chat(result) == "success"
        # A pending job is not a failed invocation, but neither is it terminal
        # success evidence for durable path learning.
        assert runtime_tool_path_memory_outcome(result) == (
            None if payload.get("status") == "pending" else "success"
        )
        assert _tool_end_event_data("video_edit", result)["status"] == "completed"
    assert payload == original


@pytest.mark.parametrize("payload", [
    {"__hitl__": True, "error": "approval_required", "approval_token": "opaque"},
    {"status": "approval_required", "approvalId": "approval-1", "ok": False},
    {"approval_required": True, "approvalId": "approval-1", "error": "approval_required"},
    {"status": "hitl_required", "stop_reason": "hitl_required",
     "control": {"kind": "approval_required", "tool_name": "request_input",
                 "blocking_result": {"__hitl__": True, "error": "approval_required"}}},
])
def test_human_gate_is_not_an_execution_failure_or_path_success(payload):
    original = copy.deepcopy(payload)
    for result in (payload, json.dumps(payload)):
        assert runtime_tool_call_error(result) is None
        assert runtime_tool_status_for_chat(result) == "success"
        assert runtime_tool_path_memory_outcome(result) is None
        assert _tool_end_event_data("invoke_skill", result)["status"] == "completed"
    assert payload == original


@pytest.mark.parametrize("status", ["waiting_human", "rejected", "blocked", "cancelled", "canceled"])
def test_gate_decisions_keep_ui_status_without_becoming_execution_failures(status):
    payload = {"status": status, "error": "Operation did not proceed"}
    assert runtime_tool_call_error(payload) is None
    assert runtime_tool_status_for_chat(payload) == "error"
    assert runtime_tool_path_memory_outcome(payload) is None


def test_structured_mcp_result_classifies_payload_not_display_text():
    result = RuntimeStructuredToolResult("Display text", structured_content={"ok": False, "message": "Actual failure"})
    assert runtime_tool_call_error(result) == "Actual failure"
    assert runtime_tool_status_for_chat(result) == "error"
    assert _tool_end_event_data("mcp__example__read", result)["status"] == "failed"
    result = RuntimeStructuredToolResult("Display text", structured_content={"__hitl__": True, "error": "approval_required"})
    assert runtime_tool_call_error(result) is None
    assert runtime_tool_path_memory_outcome(result) is None


@pytest.mark.parametrize("display,payload,expected", [
    ("Error: display-only text", {"ok": True}, None),
    ('{"__hitl__":true,"error":"approval_required"}', {"ok": False, "message": "Actual provider failure"}, "Actual provider failure"),
])
def test_authoritative_structured_result_cannot_be_overridden_by_display(display, payload, expected):
    result = RuntimeStructuredToolResult(display, structured_content=payload)
    assert runtime_tool_call_error(result) == expected
    assert runtime_tool_status_for_chat(result) == ("error" if expected else "success")
    assert runtime_tool_path_memory_outcome(result) == ("failure" if expected else "success")


def test_malformed_provider_exit_code_cannot_raise_in_the_classifier():
    result = {"exit_code": "9" * 4500}
    assert "invalid exit_code" in runtime_tool_call_error(result)
    assert runtime_tool_status_for_chat(result) == "error"
    assert _tool_end_event_data("example", result)["status"] == "failed"


def test_empty_search_dict_is_not_an_error():
    assert runtime_tool_call_outcome("search_tools", {"matches": []}) == "empty_result"
    assert runtime_tool_call_outcome("search_tools", {"matches": [], "ok": False}) == "error"


def test_raw_mcp_error_text_preserves_all_blocks_in_decoder():
    assert runtime_tool_call_error({"isError": True, "content": [
        {"type": "text", "text": "First failure"},
        {"type": "text", "text": "Repair detail"},
    ]}) == "First failure\nRepair detail"


def test_mcp_error_adapter_preserves_diagnostics_without_promoting_new_controls():
    from packages.core.ai.tools.mcp_builtin import _mcp_error_result_to_text

    source = {"isError": True, "content": [
        {"type": "text", "text": json.dumps({"message": "Old compatible message", "retryable": False})},
        {"type": "text", "text": "Second diagnostic"},
        {"type": "resource_link", "uri": "resource://diagnostic", "name": "Details"},
    ], "structuredContent": {"code": "provider_error", "contract_violations": [
        {"file": "index.html", "pointer": "/root", "rule": "root", "expected": "one root", "repair_hint": "repair index"},
    ], "stop_parent": True, "action_executed": False}}
    original = copy.deepcopy(source)
    payload = json.loads(_mcp_error_result_to_text("example", "edit", source))
    assert payload["error"] == "tool_error"
    assert payload["message"] == "Old compatible message"
    assert payload["retryable"] is False
    assert payload["details"] == source
    assert "stop_parent" not in payload
    assert "action_executed" not in payload
    assert source == original


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status": "source_updated", "source_saved": True, "contract_valid": False,
     "contract_violations": [{"file": "index.html", "rule": "root"}], "title": "视频"},
    [{"success": True}],
])
async def test_registered_handler_dict_and_list_results_remain_valid_json(payload):
    async def handler(**kwargs):
        return payload

    result = await runtime_execute_prepared_tool_handler(
        tool_name="test_tool", handler=handler, prepared=RuntimePreparedToolExecution(arguments={}),
        entity_id="entity-1", user_id="user-1",
    )
    assert json.loads(result) == payload


@pytest.mark.asyncio
async def test_dynamic_handler_dict_is_json_without_changing_execution_boundary(monkeypatch):
    from packages.core.ai.runtime import tool_execution

    prepare = AsyncMock(return_value=RuntimePreparedToolExecution(arguments={}))
    monkeypatch.setattr(tool_execution, "runtime_prepare_tool_execution", prepare)
    payload = {"ok": False, "error": {"code": "invalid_request", "message": "Missing input"}}
    result = await runtime_execute_scoped_dynamic_tool_handler(
        tool_name="test_tool", arguments={}, handler=lambda arguments: payload,
        entity_id="entity-1", user_id="user-1",
    )
    assert json.loads(result) == payload
    prepare.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    RuntimePreflightValidationError('{"error":"invalid_request"}'),
    RuntimeNoEffectToolResult.document_target_exists(),
    RuntimeStructuredToolResult("Display", structured_content={"ok": True}),
    RuntimeToolSuspension(kind="waiting_human", reservation_id="request-1", poll_after_seconds=1, deadline_at="later"),
])
async def test_result_serialization_preserves_host_owned_carriers(result):
    returned = await runtime_execute_prepared_tool_handler(
        tool_name="test_tool", handler=lambda **kwargs: result,
        prepared=RuntimePreparedToolExecution(arguments={}), entity_id="entity-1", user_id="user-1",
    )
    assert returned is result


@pytest.mark.asyncio
async def test_runtime_abort_remains_control_flow_and_is_not_normalized():
    def handler(**kwargs):
        raise RuntimeTurnAborted("Effect checkpoint failed")

    with pytest.raises(RuntimeTurnAborted, match="Effect checkpoint failed"):
        await runtime_execute_prepared_tool_handler(
            tool_name="test_tool", handler=handler, prepared=RuntimePreparedToolExecution(arguments={}),
            entity_id="entity-1", user_id="user-1",
        )
