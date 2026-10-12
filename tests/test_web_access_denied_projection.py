"""Public website denial stays precise without changing execution outcomes."""
from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from packages.core.ai.runtime.output_policy import (
    PUBLIC_TOOL_FAILURE_MESSAGE,
    PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
    runtime_public_tool_calls,
    runtime_public_tool_payload,
    runtime_public_tool_result,
    runtime_public_transport_payload,
)
from packages.core.ai.runtime.streams import RuntimeToolStreamSink, runtime_tool_status_for_chat
from packages.core.ai.tools.extended_tools import _web_fetch_handler
from packages.core.services.assistant_blocks import AssistantBlocksBuilder
from packages.core.services.web_access_denial_projection import project_web_access_denials

pytestmark = pytest.mark.unit
URL = "https://example.com/source"


def denial():
    return {"error": f"Failed to fetch {URL}: HTTP 403", "status_code": 403,
            "reason": "website_access_denied", "hint": "private fetch advice"}


def history():
    calls = [{"name": "web_fetch", "status": "error", "arguments": {"url": URL},
              "result": PUBLIC_TOOL_FAILURE_MESSAGE}]
    blocks = [{"type": "process", "lifecycle_status": "failed", "steps": [{
        "id": "step_1", "kind": "tool", "name": "web_fetch", "status": "error",
        "arguments_preview": json.dumps({"url": URL}), "result_preview": PUBLIC_TOOL_FAILURE_MESSAGE,
    }]}]
    metadata = {"runtime": {"metadata": {"runtime_events": [{
        "type": "tool_end", "tool_name": "web_fetch", "status": "failed",
        "error": f"Failed to fetch {URL}: HTTP 403",
    }]}}}
    return calls, blocks, metadata


@pytest.mark.asyncio
async def test_web_fetch_preserves_original_403_and_adds_typed_reason(monkeypatch):
    from packages.core.services import web_fetch

    request = httpx.Request("GET", URL)
    response = httpx.Response(403, request=request)
    monkeypatch.setattr(web_fetch, "fetch_url", AsyncMock(side_effect=httpx.HTTPStatusError(
        "Forbidden", request=request, response=response,
    )))
    raw = json.loads(await _web_fetch_handler(url=URL))
    assert raw["error"] == f"Failed to fetch {URL}: HTTP 403"
    assert raw["status_code"] == 403
    assert raw["reason"] == "website_access_denied"
    assert "Do not retry" in raw["hint"]
    assert runtime_tool_status_for_chat(json.dumps(raw)) == "error"


@pytest.mark.parametrize("as_json", [False, True])
def test_public_denial_is_fixed_and_idempotent(as_json):
    raw = json.dumps(denial()) if as_json else denial()
    public = runtime_public_tool_result(raw)
    assert public == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert runtime_public_tool_result(public) == public
    call = {"name": "web_fetch", "status": "error", "result": raw, "raw_result": raw}
    step = {**call, "kind": "tool", "result_preview": raw}
    payload = runtime_public_transport_payload({"tool_calls": [call], "assistant_blocks": [
        {"type": "process", "lifecycle_status": "failed", "steps": [step]},
    ]})
    for projected in (payload["tool_calls"][0], payload["assistant_blocks"][0]["steps"][0]):
        assert projected["name"] == "web_fetch"
        assert projected["status"] == "error"
        assert projected["result"] == public
        assert projected["reason"] == "website_access_denied"
        assert projected["status_code"] == 403
        assert "raw_result" not in projected
    assert payload["assistant_blocks"][0]["lifecycle_status"] == "failed"
    assert runtime_public_transport_payload(payload) == payload
    assert runtime_public_tool_payload(payload) == payload


@pytest.mark.parametrize("raw", [
    {"error": "HTTP 403", "status_code": 403},
    {"error": "Provider authentication returned HTTP 403", "status_code": 403},
    {"error": f"Failed to fetch {URL}: HTTP 403", "status_code": 403},
    {"error": "private failure", "reason": "website_access_denied", "status_code": 500},
    {"error": "private failure", "reason": "website_access_denied"},
])
def test_untyped_provider_internal_and_legacy_403_remain_generic(raw):
    assert runtime_public_tool_result(raw) == PUBLIC_TOOL_FAILURE_MESSAGE


def test_successful_page_quote_and_other_tools_do_not_gain_denial_classification():
    for name, status in (("web_fetch", "success"), ("provider_request", "error")):
        projected = runtime_public_tool_calls([{
            "name": name, "status": status, "result": PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
        }])[0]
        assert "reason" not in projected
        assert projected["status"] == status


def test_live_tool_transport_preserves_failed_status_and_raw_audit():
    class Queue:
        def __init__(self):
            self.frames = []

        def put_nowait(self, frame):
            self.frames.append(frame)

    queue = Queue()
    recorded = []
    raw = json.dumps(denial())
    sink = RuntimeToolStreamSink(
        event_queue=queue, format_event=lambda kind, payload: (kind, payload),
        record_tool_event=lambda kind, payload: recorded.append((kind, payload)),
        format_tool_result=lambda name, value: runtime_public_tool_result(value),
        resolve_tool_status=runtime_tool_status_for_chat,
    )
    sink.emit_tool_end("web_fetch", raw, args={"url": URL}, duration_ms=12)
    call = queue.frames[0][1]["tool_call"]
    assert call["result"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert call["reason"] == "website_access_denied"
    assert call["status"] == "error"
    assert recorded[0][1]["tool_call"]["raw_result"] == raw
    builder = AssistantBlocksBuilder()
    builder.start_tool("web_fetch", {"url": URL})
    builder.end_tool("web_fetch", result=call["result"], status="error")
    step = runtime_public_tool_payload(builder.blocks())[0]["steps"][0]
    assert step["result_preview"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert step["reason"] == "website_access_denied"


def test_history_recovers_both_projections_without_mutating_audit_or_lifecycle():
    calls, blocks, metadata = history()
    original = deepcopy((calls, blocks, metadata))
    projected_calls, projected_blocks = project_web_access_denials(calls, blocks, metadata)
    assert projected_calls[0]["result"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert projected_blocks[0]["steps"][0]["result_preview"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert projected_blocks[0]["steps"][0]["reason"] == "website_access_denied"
    assert projected_calls[0]["status"] == "error"
    assert projected_blocks[0]["lifecycle_status"] == "failed"
    assert (calls, blocks, metadata) == original
    assert project_web_access_denials(projected_calls, projected_blocks, metadata) == (projected_calls, projected_blocks)
    public_calls = runtime_public_tool_calls(projected_calls)
    public_blocks = runtime_public_tool_payload(projected_blocks)
    assert public_calls[0]["reason"] == "website_access_denied"
    assert public_calls[0]["status_code"] == 403
    assert public_blocks[0]["steps"][0]["reason"] == "website_access_denied"
    assert public_blocks[0]["steps"][0]["result_preview"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE


@pytest.mark.parametrize("location", ["call", "step"])
def test_history_does_not_override_a_specific_conflicting_failure(location):
    calls, blocks, metadata = history()
    if location == "call":
        calls[0]["result"] = json.dumps({"error": "HTTP 500", "status_code": 500})
    else:
        blocks[0]["steps"][0]["result_preview"] = "The request timed out."
    assert project_web_access_denials(calls, blocks, metadata) == (calls, blocks)


@pytest.mark.parametrize("shape", [7, "damaged", {"bad": "steps"}, None])
def test_malformed_history_process_steps_are_preserved_without_raising(shape):
    calls, blocks, metadata = history()
    blocks[0]["steps"] = shape
    projected_calls, projected_blocks = project_web_access_denials(calls, blocks, metadata)
    assert projected_calls[0]["result"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert projected_blocks == blocks


@pytest.mark.parametrize("conflict", ["no_events", "other_url", "other_tool", "wrong_status", "http500",
                                           "duplicate_event", "duplicate_call", "duplicate_step", "non_tool_step",
                                           "successful_call", "successful_step"])
def test_legacy_recovery_requires_unique_same_message_url_and_403_evidence(conflict):
    calls, blocks, metadata = history()
    events = metadata["runtime"]["metadata"]["runtime_events"]
    if conflict == "no_events":
        events.clear()
    elif conflict == "other_url":
        events[0]["error"] = "Failed to fetch https://another.example/source: HTTP 403"
    elif conflict == "other_tool":
        events[0]["tool_name"] = "provider_request"
    elif conflict == "wrong_status":
        events[0]["status"] = "completed"
    elif conflict == "http500":
        events[0]["error"] = f"Failed to fetch {URL}: HTTP 500"
    elif conflict == "duplicate_event":
        events.append(dict(events[0]))
    elif conflict == "duplicate_call":
        calls.append(dict(calls[0]))
    elif conflict == "duplicate_step":
        blocks[0]["steps"].append(dict(blocks[0]["steps"][0]))
    elif conflict == "non_tool_step":
        blocks[0]["steps"][0]["kind"] = "commentary"
    elif conflict == "successful_call":
        calls[0]["status"] = "success"
    elif conflict == "successful_step":
        blocks[0]["steps"][0]["status"] = "success"
    projected_calls, projected_blocks = project_web_access_denials(calls, blocks, metadata)
    if conflict == "non_tool_step":
        assert projected_calls[0]["result"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
        assert projected_blocks == blocks
    else:
        assert projected_calls == calls
        assert projected_blocks == blocks
