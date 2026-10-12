import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.api.routers.workspace_chat import _to_message
from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.runtime import ChatSurface
from packages.core.ai.runtime.streams import (
    runtime_tool_arguments_for_chat,
    runtime_tool_result_for_chat,
    runtime_tool_status_for_chat,
)
from packages.core.ai.runtime.workspace_operation_actions import _operation_review_payload
from packages.core.models.task import Message
from packages.core.services.assistant_blocks import (
    AssistantBlocksBuilder,
    assistant_tool_uses_structured_result,
    project_workspace_draft_retry_recovery,
)
from packages.core.services.chat_service import stream_chat_response


def draft_retry_trace():
    draft = {
        "id": "draft-retry", "workspace_id": "workspace-retry", "status": "open",
        "base_revision": 0, "diff": {},
        "patches": [{"op": "execution_policy.upsert", "payload": {}}],
        "validation": {"valid": True, "errors": [], "warnings": []},
    }
    review = _operation_review_payload(draft, prompt="Review this change", content={})
    results = [
        {"workspace_id": "workspace-retry", "state": {"notes": "x" * 800}},
        {"error": "create_draft requires patches as [{op, payload}] "
         "(keys accepted: patches, operations, ops). Supported operations."},
        review,
    ]
    calls = []
    for index, result in enumerate(results):
        args = {"action": "workspace", "params": {"action": "operation", "params": {
            "action": "get_current" if index == 0 else "create_draft",
            **({"title": "Remove a hard block"} if index else {}),
            **({"patches": draft["patches"]} if index == 2 else {}),
        }}}
        raw = json.dumps(result)
        calls.append({
            "name": "manor", "arguments": runtime_tool_arguments_for_chat("manor", args),
            "raw_result": raw, "result": runtime_tool_result_for_chat("manor", raw),
            "status": runtime_tool_status_for_chat(raw),
        })
    return calls, [review["hitl"]]


def build_trace(calls, *, with_scope=True):
    builder = AssistantBlocksBuilder(workspace_id="workspace-retry" if with_scope else None)
    for call in calls:
        args = call["arguments"]
        builder.start_tool(call["name"], args)
        builder.end_tool(
            call["name"], arguments=args, result=call["result"], status=call["status"],
            structured_result=call["raw_result"] if assistant_tool_uses_structured_result(call["name"], args) else None,
        )
    return builder.blocks()


def legacy_trace(calls):
    blocks = build_trace(calls)
    for step in blocks[0]["steps"]:
        step.pop("recovered_by_step_id", None)
        step.pop("approval_request_id", None)
    return blocks


def test_live_preview_path_recovers_missing_patches_with_runtime_scope():
    calls, _ = draft_retry_trace()
    # No get_current is needed on a new turn: the chat context owns its scope.
    blocks = build_trace(calls[1:])
    failed, gate = blocks[0]["steps"]
    assert failed["status"] == "error"
    assert failed["recovered_by_step_id"] == gate["id"]
    assert gate["approval_request_id"] == json.loads(calls[-1]["raw_result"])["approval_token"]
    assert blocks[0]["status"] == "completed"


def test_unscoped_draft_retry_cannot_be_inferred_from_its_title():
    calls, _ = draft_retry_trace()
    assert "recovered_by_step_id" not in build_trace(calls[1:], with_scope=False)[0]["steps"][0]


@pytest.mark.asyncio
async def test_stream_callback_persists_recovery_using_context_scope_and_full_review():
    calls, _ = draft_retry_trace()
    calls = calls[1:]

    async def fake_context(*_args, **_kwargs):
        return "system", [], [], SimpleNamespace(
            workspace_id="workspace-retry", task_id=None, runtime_envelope=None,
            tool_profile=None, allowed_tool_names=None, user=None, entity=None,
        )

    async def fake_loop(**kwargs):
        for index, call in enumerate(calls):
            args = deepcopy(call["arguments"])
            args["params"]["params"]["action"] = "create_draft"
            if index:
                args["params"]["params"]["patches"] = json.loads(call["raw_result"])["operation"]["patches"]
            kwargs["on_tool_start"](call["name"], args)
            kwargs["on_tool_end"](call["name"], call["raw_result"], 5, args)
        return AgenticResult(
            content="Review this change", messages=[], usage={}, rounds=2,
            tool_calls_made=["manor", "manor"], stop_reason="hitl_required",
        )

    saved = []

    async def fake_save(**kwargs):
        saved.append(kwargs)
        return "message-retry"

    with (
        patch("packages.core.services.chat_service.get_chat_turn_cancel_generation", new=AsyncMock(return_value=0)),
        patch("packages.core.services.chat_service.chat_turn_cancellation_requested", new=AsyncMock(return_value=False)),
        patch("packages.core.services.chat_service.resolve_runtime_chat_context", new=fake_context),
        patch("packages.core.services.chat_service.runtime_execute_chat_agent_loop", new=fake_loop),
        patch("packages.core.services.chat_service.save_or_update_assistant_stream_message", new=fake_save),
        patch("packages.core.services.chat_service.runtime_persist_chat_stream_runtime_events", new=AsyncMock()),
        patch("packages.core.services.chat_service.record_chat_llm_usage", new=AsyncMock()),
        patch("packages.core.services.chat_service.record_chat_runtime_learning", new=AsyncMock(return_value=[])),
    ):
        events = [raw async for raw in stream_chat_response(
            "Remove a hard block", "conversation-retry", entity_id="entity-retry",
            user_id="user-retry", agent_id="agent-retry", assistant_message_id="message-retry",
            persist_messages=True, runtime_surface=ChatSurface.WORKSPACE_CHAT,
        )]
    assert any("event: hitl_required" in raw for raw in events)
    failed, gate = saved[-1]["meta"]["assistant_blocks"][0]["steps"]
    assert failed["status"] == "error"
    assert failed["recovered_by_step_id"] == gate["id"]
    assert gate["approval_request_id"] == json.loads(calls[-1]["raw_result"])["approval_token"]


def test_legacy_api_projects_recovery_without_mutating_or_exposing_audit_results():
    calls, requests = draft_retry_trace()
    meta = {"assistant_blocks": legacy_trace(calls), "hitl_requests": requests}
    before = deepcopy((meta, calls))
    message = Message(
        id="message-retry", conversation_id="conversation-retry", role="assistant",
        content="Review this change", message_kind="hitl_request", author_kind="agent",
        meta=meta, tool_calls=calls, created_at=datetime.now(UTC),
    )
    response = _to_message(message)
    failed, gate = response.assistant_blocks[0]["steps"][1:]
    assert failed["status"] == "error"
    assert failed["recovered_by_step_id"] == gate["id"]
    assert gate["approval_request_id"] == json.loads(calls[-1]["raw_result"])["approval_token"]
    assert all("raw_result" not in call for call in response.tool_calls)
    assert (message.meta, message.tool_calls) == before


@pytest.mark.parametrize("change", [
    "scope", "title", "validation", "invalid", "token", "empty_patches", "other_error", "call_order", "missing_scope",
])
def test_legacy_projection_requires_exact_recovery_evidence(change):
    calls, requests = draft_retry_trace()
    blocks = legacy_trace(calls)
    review = json.loads(calls[2]["raw_result"])
    if change == "scope":
        review["operation"]["workspace_id"] = "other-workspace"
    elif change == "title":
        calls[1]["arguments"]["params"]["params"]["title"] = "Another request"
    elif change == "validation":
        review["operation"]["validation"]["errors"] = ["invalid"]
    elif change == "invalid":
        review["operation"]["validation"]["valid"] = False
    elif change == "token":
        review["approval_token"] = "other-draft"
    elif change == "empty_patches":
        review["operation"]["patches"] = []
    elif change == "other_error":
        calls[1]["raw_result"] = '{"error":"apply failed after side effect"}'
    elif change == "call_order":
        calls.reverse()
    elif change == "missing_scope":
        calls[0]["raw_result"] = '{"state":{}}'
    if change != "call_order":
        calls[2]["raw_result"] = json.dumps(review)
    projected = project_workspace_draft_retry_recovery(blocks, calls, requests)
    assert "recovered_by_step_id" not in projected[0]["steps"][1]
