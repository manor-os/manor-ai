"""Production regressions: file ACL feedback and spreadsheet call contracts."""
from __future__ import annotations

import io
import json
from unittest.mock import AsyncMock

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table

from packages.core.ai.runtime import ChatSurface
from packages.core.ai.runtime.output_policy import (
    PUBLIC_REQUEST_FAILURE_MESSAGE,
    PUBLIC_TOOL_FAILURE_MESSAGE,
    runtime_assistant_stream_error_content,
    runtime_public_assistant_message_content,
    runtime_public_tool_calls,
    runtime_public_tool_result,
)
from packages.core.ai.runtime.tool_input_validation import runtime_tool_input_validation_result
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.ai.tools import file_tools
from packages.core.contracts.file_engine import normalize_file_patch_operation

pytestmark = pytest.mark.unit

EDIT_DENIED = "You don't have permission to edit this file. Ask the file owner for edit access."
DENIAL = {
    "error": "file_permission_denied",
    "mode": "entity_file_acl_denied",
    "reason": "Edit access is required to write this file",
    "operation": {"tool": "patch_file", "action": "edit", "paths": ["Workspaces/private.xlsx"]},
}


def _failed_calls():
    return [{"name": "patch_file", "status": "error", "result": json.dumps(DENIAL),
             "raw_result": json.dumps(DENIAL)}]


@pytest.mark.parametrize("as_json", [False, True])
def test_file_permission_failure_is_actionable_without_exposing_internal_fields(as_json):
    payload = {**DENIAL, "reason": "private diagnostic sk-test-123456789", "sql": "private query"}
    assert runtime_public_tool_result(json.dumps(payload) if as_json else payload) == EDIT_DENIED
    assert runtime_assistant_stream_error_content(EDIT_DENIED) == EDIT_DENIED
    call = runtime_public_tool_calls(_failed_calls())[0]
    assert call["result"] == EDIT_DENIED
    assert "raw_result" not in call


@pytest.mark.parametrize("mode", ["deny", "ask", "permission_denied", "authorization_receipt_mismatch", "invalid_file_action", None])
def test_other_file_rejections_do_not_invent_a_missing_owner_grant(mode):
    denial = {**DENIAL, "mode": mode}
    assert runtime_public_tool_result(denial) == PUBLIC_TOOL_FAILURE_MESSAGE
    calls = [{"name": "patch_file", "status": "error", "result": json.dumps(denial)}]
    assert runtime_public_assistant_message_content(
        PUBLIC_REQUEST_FAILURE_MESSAGE, {"error": "tool_error"}, calls,
    ) == PUBLIC_REQUEST_FAILURE_MESSAGE


def test_failed_history_uses_the_actual_denial_and_keeps_successful_recovery():
    meta = {"error": "tool_error", "stop_reason": "error", "stream_status": "error",
            "limit_detail": {"source_tool": "patch_file"}}
    assert runtime_public_assistant_message_content(
        PUBLIC_REQUEST_FAILURE_MESSAGE, meta, _failed_calls(),
    ) == EDIT_DENIED
    recovered_calls = _failed_calls() + [
        {"name": "patch_file", "status": "success", "result": '{"patched":true}'},
    ]
    assert runtime_public_assistant_message_content(
        "Saved both rows.", {"stop_reason": "completed"}, recovered_calls,
    ) == "Saved both rows."
    # A later unrelated failure must not be misreported as the old denial.
    assert runtime_public_assistant_message_content(
        PUBLIC_REQUEST_FAILURE_MESSAGE, meta, recovered_calls,
    ) == PUBLIC_REQUEST_FAILURE_MESSAGE
    assert runtime_public_assistant_message_content(
        PUBLIC_REQUEST_FAILURE_MESSAGE, {"error": "llm_call_failed"}, _failed_calls(),
    ) == PUBLIC_REQUEST_FAILURE_MESSAGE
    assert runtime_public_tool_result({"error": "private_failure", "reason": "private SQL"}) == PUBLIC_TOOL_FAILURE_MESSAGE


@pytest.mark.asyncio
async def test_live_chat_snapshot_preserves_permission_failure(monkeypatch):
    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.services import chat_service
    from test_chat_stream_resume import _run_turn, _snapshots

    monkeypatch.setattr(chat_service, "get_chat_turn_cancel_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(chat_service, "chat_turn_cancellation_requested", AsyncMock(return_value=False))

    async def denied_loop(**kwargs):
        kwargs["on_tool_start"]("patch_file", {"path": "Workspaces/private.xlsx"})
        kwargs["on_tool_end"]("patch_file", json.dumps(DENIAL), 12, {"path": "Workspaces/private.xlsx"})
        return AgenticResult(
            content="The spreadsheet service is temporarily unavailable.",
            messages=[], usage={}, rounds=2, tool_calls_made=["patch_file"],
            stop_reason="error", error="tool_error",
            error_detail={"source_tool": "patch_file", "reason": "tool_outcome_unconfirmed"},
        )

    payloads = await _run_turn(workspace_id=None, surface=ChatSurface.GLOBAL_OWNER_CHAT, loop=denied_loop)
    assert _snapshots(payloads)[-1]["data"]["content"] == EDIT_DENIED


@pytest.mark.parametrize("surface", [ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.WORKSPACE_CHAT, ChatSurface.PUBLIC_CUSTOMER_CHAT])
@pytest.mark.asyncio
async def test_permission_failure_reaches_stream_and_nonstream_responses(monkeypatch, surface):
    import asyncio
    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.services import chat_service
    from packages.core.ai.runtime.streams import runtime_persisted_tool_calls_history_summary
    from test_byok_provider_failure import _patch_chat_service

    result = AgenticResult(
        content=PUBLIC_REQUEST_FAILURE_MESSAGE, messages=[], usage={}, rounds=1,
        tool_calls_made=["patch_file"], stop_reason="error", error="tool_error",
        error_detail={"source_tool": "patch_file"},
    )
    save = _patch_chat_service(monkeypatch, result)

    async def denied_loop(**kwargs):
        args = {"path": "Workspaces/private.xlsx"}
        kwargs["on_tool_start"]("patch_file", args)
        kwargs["on_tool_end"]("patch_file", json.dumps(DENIAL), 12, args)
        return result

    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", denied_loop)
    events = []
    async for frame in chat_service.stream_chat_response(
        "Update the file.", "conversation-1", entity_id="entity-1", user_id="user-1",
        assistant_message_id="reply-1", runtime_surface=surface,
    ):
        event, data = frame.split("\ndata: ", 1)
        events.append((event.removeprefix("event: "), json.loads(data)))
    if chat_service._SNAPSHOT_PUBLISH_TASKS:
        await asyncio.gather(*list(chat_service._SNAPSHOT_PUBLISH_TASKS))
    terminal = next(data for event, data in events if event == "stream_end")
    assert terminal["error"] == EDIT_DENIED
    visible = ""
    for event, data in events:
        if event == "text_reset":
            visible = ""
        elif event == "text_delta":
            visible += data.get("content", "")
    assert visible == EDIT_DENIED
    assert save.await_args.kwargs["meta"]["error"] == "tool_error"
    assert json.loads(save.await_args.kwargs["tool_calls"][0]["raw_result"]) == DENIAL
    assert EDIT_DENIED in runtime_persisted_tool_calls_history_summary(save.await_args.kwargs["tool_calls"])
    response = await chat_service.run_chat_message(
        "Update the file.", "conversation-1", entity_id="entity-1", user_id="user-1", runtime_surface=surface,
    )
    assert response["content"] == response["error"] == EDIT_DENIED


BAD_OPERATIONS = [
    {"op": "update_row", "range": "A5:B5", "values": [["2026-09-27", "Calvin"]]},
    {"op": "set_cell", "range": "A5:B5", "values": [["2026-09-27", "Calvin"]]},
    {"op": "update_row", "range": "A5:B5", "updates": {"A": "2026-09-27", "B": "Calvin"}},
    {"op": "update_row", "table": "DailyTasks", "match_column": "Date", "row": {"Date": "2026-09-27"}},
    {"op": "row.append", "table": "DailyTasks", "rows": [{"Date": "2026-09-28"}]},
]


@pytest.mark.parametrize("operation", BAD_OPERATIONS)
@pytest.mark.parametrize("alias", [False, True])
def test_production_argument_errors_are_rejected_before_execution(operation, alias):
    operation = {**operation, "sheet": "Daily"}
    if alias:
        operation["op"] = {"update_row": "row.update", "set_cell": "cell.set",
                           "row.append": "append_row"}.get(operation["op"], operation["op"])
    result = runtime_tool_input_validation_result(
        tool_name="patch_file", arguments={"path": "daily.xlsx", "operations": [operation]},
        tool_schema=file_tools.PATCH_FILE_SCHEMA,
    )
    assert isinstance(result, RuntimePreflightValidationError), result
    payload = json.loads(result)
    assert payload["details"]["path"].startswith("$.operations[0]")
    assert "Calvin" not in result


def test_patch_correction_names_all_missing_fields_at_once():
    result = runtime_tool_input_validation_result(
        tool_name="patch_file", arguments={"path": "daily.xlsx", "operations": [BAD_OPERATIONS[0]]},
        tool_schema=file_tools.PATCH_FILE_SCHEMA,
    )
    message = json.loads(result)["message"]
    for field in ("match_column", "match_value", "updates"):
        assert field in message


@pytest.mark.parametrize("operation", [
    {"op": "row.append", "row": {"Member": "Calvin"}, "values": ["Calvin"]},
    {"op": "row.append", "values": [["Calvin"]]},
    {"op": "row.append", "table": "DailyTasks", "row": {"Member": "Calvin"}, "header_row": 1},
    {"op": "row.update", "match_column": "Member", "match_value": "Calvin", "updates": {}},
    {"op": "row.update", "match_column": "Member", "match_value": "Calvin", "updates": {"Status": "Done"}, "table": "DailyTasks"},
    {"op": "cell.set", "cell": "A1", "value": ["Calvin"]},
])
def test_ambiguous_or_unsupported_spreadsheet_payloads_cannot_reach_a_handler(operation):
    result = runtime_tool_input_validation_result(
        tool_name="patch_file", arguments={"path": "daily.xlsx", "operations": [operation]},
        tool_schema=file_tools.PATCH_FILE_SCHEMA,
    )
    assert isinstance(result, RuntimePreflightValidationError)
    assert "Calvin" not in result


@pytest.mark.parametrize("path,operation", [
    ("table.csv", {"op": "row.append", "values": ["Calvin", None, 1, False]}),
    ("report.docx", {"op": "cell.set", "table_index": 0, "cell": "A1", "value": "Done"}),
    ("deck.pptx", {"op": "cell.set", "slide": 1, "shape_id": 2, "cell": "A1", "value": "Done"}),
    ("report.docx", {"op": "table.insert", "index": 0, "rows": [["Header"], ["Value"]]}),
    ("data.json", {"op": "json.replace", "pointer": "/items", "value": [1, 2]}),
])
def test_spreadsheet_validation_preserves_other_native_patch_formats(path, operation):
    assert runtime_tool_input_validation_result(
        tool_name="patch_file", arguments={"path": path, "operations": [operation]},
        tool_schema=file_tools.PATCH_FILE_SCHEMA,
    ) is None


def test_row_update_schema_declares_matching_fields_and_examples_are_executable(tmp_path):
    properties = file_tools.PATCH_FILE_SCHEMA["function"]["parameters"]["properties"]["operations"]["items"]["properties"]
    assert {"match_value", "match_mode", "match_case_sensitive", "header_row"} <= set(properties)
    source = tmp_path / "daily.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Daily"
    sheet.append(["Date", "Member", "Status"])
    sheet.append(["2026-09-26", "Calvin", "In progress"])
    sheet.add_table(Table(displayName="DailyTasks", ref="A1:C2"))
    workbook.save(source)
    workbook.close()
    operations = [
        {"op": "row.update", "sheet": "Daily", "header_row": 1, "match_column": "Member",
         "match_value": "Calvin", "updates": {"Status": "Done"}},
        {"op": "cell.set", "sheet": "Daily", "cell": "A2", "value": "2026-09-27"},
        {"op": "row.append", "sheet": "Daily", "table": "DailyTasks",
         "row": {"Date": "2026-09-28", "Member": "Simon", "Status": "In progress"}},
        {"op": "row.append", "sheet": "Daily", "table": "DailyTasks",
         "row": {"Date": "2026-09-28", "Member": "Simon", "Status": "Done"}},
    ]
    assert runtime_tool_input_validation_result(
        tool_name="patch_file", arguments={"path": source.name, "operations": operations},
        tool_schema=file_tools.PATCH_FILE_SCHEMA,
    ) is None
    result = file_tools._apply_office_patch_sequence_sync(
        str(source), [normalize_file_patch_operation(op) for op in operations],
    )
    assert result["operations_applied"] == 4
    edited = load_workbook(io.BytesIO(result["_persisted_bytes"]))
    assert list(edited["Daily"].values) == [
        ("Date", "Member", "Status"), ("2026-09-27", "Calvin", "Done"),
        ("2026-09-28", "Simon", "In progress"), ("2026-09-28", "Simon", "Done"),
    ]
    assert edited["Daily"].tables["DailyTasks"].ref == "A1:C4"
    edited.close()


@pytest.mark.asyncio
async def test_model_can_correct_patch_arguments_before_any_file_handler_runs(monkeypatch):
    from packages.core.ai import agentic_loop as loop
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    handler = AsyncMock(return_value='{"patched":true,"operations_applied":1}')
    pool.register("patch_file", file_tools.PATCH_FILE_SCHEMA, handler)
    correct = {"op": "row.append", "sheet": "Daily", "table": "DailyTasks", "row": {"Member": "Calvin"}}

    async def allow(self, request):
        return RuntimeApprovalDecision.allow(classify_runtime_tool(request.tool_name, request.arguments), request)

    async def model(messages, tools, **kwargs):
        results = [m for m in messages if m.get("role") == "tool"]
        if not results:
            operation = BAD_OPERATIONS[0]
        elif len(results) == 1:
            assert json.loads(results[0]["content"])["error"] == "tool_input_validation_failed"
            handler.assert_not_awaited()
            operation = correct
        else:
            return "Saved.", [], {}
        return "", [{"id": f"patch-{len(results)}", "name": "patch_file",
                     "arguments": {"path": "daily.xlsx", "operations": [operation]}}], {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", AsyncMock(return_value=("Failed.", {})))
    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", allow)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="Append the update.", tools=[file_tools.PATCH_FILE_SCHEMA],
        tool_executor=pool.execute, max_rounds=4,
    )
    assert result.error is None and result.content == "Saved.", result.messages
    handler.assert_awaited_once()
    assert handler.call_args.kwargs["operations"] == [correct]
