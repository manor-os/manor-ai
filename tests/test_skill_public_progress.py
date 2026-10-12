"""Nested public commentary is isolated from child final answers and replay reasoning."""
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.streams import (
    RuntimeToolStreamSink,
    runtime_skill_progress_callbacks,
    runtime_tool_stream_sink_var,
)


def bridge():
    events = []
    sink = RuntimeToolStreamSink(
        event_queue=SimpleNamespace(put_nowait=events.append),
        format_event=lambda event, data: {"event": event, **data},
    )
    token = runtime_tool_stream_sink_var.set(sink)
    try:
        start, handler = runtime_skill_progress_callbacks(None)
    finally:
        runtime_tool_stream_sink_var.reset(token)
    return events, start, handler


@pytest.mark.asyncio
async def test_progress_redacts_secrets_and_discards_private_protocol_text():
    events, start, handler = bridge()
    await handler("text_delta", {"content": "Checking the account. Authorization: Bearer sk-live-1234567890abcdef"})
    start("read_page", {})
    assert "sk-live-1234567890abcdef" not in json.dumps(events)
    assert "<redacted>" in json.dumps(events)
    for text in (
        "Tool error (mcp__private__call): private diagnostic",
        "<manor-final-response>Unvalidated result",
        "Traceback (most recent call last): ValueError('/private/config')",
    ):
        await handler("text_delta", {"content": text})
        start("read_page", {})
    assert len(events) == 1


@pytest.mark.asyncio
async def test_concurrent_child_handlers_and_resets_do_not_share_progress():
    events_a, start_a, handler_a = bridge()
    events_b, start_b, handler_b = bridge()
    await handler_a("text_delta", {"content": "Checking A."})
    await handler_b("text_delta", {"content": "Checking B."})
    await handler_a("text_reset", {})
    start_a("read_page", {})
    start_b("read_page", {})
    assert events_a == []
    assert events_b == [{"event": "process_note", "content": "Checking B."}]
    await handler_b("final_response_start", {})
    await handler_b("text_delta", {"content": "Rejected candidate"})
    start_b("read_page", {})
    assert len(events_b) == 1


def test_nonchat_skill_keeps_nonstreaming_execution():
    token = runtime_tool_stream_sink_var.set(None)
    try:
        def start(*_args):
            pass

        assert runtime_skill_progress_callbacks(start) == (start, None)
    finally:
        runtime_tool_stream_sink_var.reset(token)


def test_progress_recorder_only_exports_public_blocks_not_private_bookkeeping():
    events = []

    def record(payload):
        payload["assistant_blocks"] = [{"type": "text", "phase": "progress", "text": payload["content"]}]
        payload["assistant_blocks_schema"] = "v1"
        payload["private_trace"] = "private-provider-payload"

    sink = RuntimeToolStreamSink(
        event_queue=SimpleNamespace(put_nowait=events.append),
        format_event=lambda event, data: {"event": event, **data},
        record_progress=record,
    )
    sink.emit_progress("Checking the upload.")
    assert events[0]["assistant_blocks"][0]["phase"] == "progress"
    assert "private_trace" not in events[0]
