"""Turn lifecycle is runtime evidence, independent of individual tool attempts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.runtime import ChatSurface
from packages.core.services.assistant_blocks import AssistantBlocksBuilder
from packages.core.services.chat_service import _chat_result_process_lifecycle, stream_chat_response


def _process(builder):
    return next(block for block in builder.blocks() if block["type"] == "process")


def test_process_clock_exists_before_text_or_first_tool_without_changing_text_phase():
    builder = AssistantBlocksBuilder(started_at_ms=1_000)
    initial = _process(builder).copy()
    assert initial["started_at_ms"] == 1_000
    assert initial["status"] == "running"
    assert initial["lifecycle_status"] == "running"
    assert initial["steps"] == []
    assert initial["duration_ms"] == 0
    assert "ended_at_ms" not in initial

    builder.append_text("I will check the files.")
    assert next(block for block in builder.blocks() if block["type"] == "text")["phase"] == "opening"
    builder.start_tool("read_file", now_ms=4_000)
    assert _process(builder)["id"] == initial["id"]
    assert _process(builder)["started_at_ms"] == initial["started_at_ms"]


@pytest.mark.asyncio
async def test_stream_start_publishes_clock_before_context_or_model_work():
    context = AsyncMock()
    with (
        patch("packages.core.services.chat_service.time", SimpleNamespace(time=lambda: 10.0)),
        patch("packages.core.services.chat_service.resolve_runtime_chat_context", new=context),
    ):
        stream = stream_chat_response("Plain reply", "conv-clock", persist_messages=False)
        first = await anext(stream)
        payload = json.loads(first.split("\ndata: ", 1)[1])
        assert first.startswith("event: stream_start\n")
        assert payload["conversation_id"] == "conv-clock"
        process = payload["assistant_blocks"][0]
        assert process["started_at_ms"] == 10_000
        assert process["lifecycle_status"] == "running"
        assert process["steps"] == []
        context.assert_not_awaited()
        await stream.aclose()


def test_process_clock_keeps_start_and_elapsed_floor_during_long_nested_tool():
    builder = AssistantBlocksBuilder(started_at_ms=1_000)
    builder.start_tool("invoke_skill", now_ms=2_000)
    builder.start_tool("read_file", now_ms=3_000)
    builder.end_tool("read_file", now_ms=4_000, duration_ms=1_000)
    process = _process(builder)
    assert process["lifecycle_status"] == "running"
    assert process["started_at_ms"] == 1_000
    assert process["duration_ms"] == 3_000
    assert "ended_at_ms" not in process
    # A refresh while the outer tool waits has the same origin, not a new timer.
    assert _process(builder)["started_at_ms"] == 1_000
    builder.end_tool("invoke_skill", now_ms=602_000, duration_ms=600_000)
    builder.finish("completed", now_ms=605_000)
    assert process["duration_ms"] == 604_000
    assert process["ended_at_ms"] == 605_000


def test_final_prose_and_successful_unrelated_tool_do_not_recover_failed_attempt():
    builder = AssistantBlocksBuilder(started_at_ms=1_000)
    builder.start_tool("video_edit", now_ms=2_000)
    builder.end_tool("video_edit", result={"error": "render failed"}, status="error", now_ms=3_000)
    builder.start_tool("read_file", now_ms=4_000)
    builder.end_tool("read_file", result="found", now_ms=5_000)
    builder.start_final_summary()
    builder.set_final_text("The requested video is complete.")
    process = _process(builder)
    assert process["lifecycle_status"] == "running"
    assert process["status"] == "error"
    assert "ended_at_ms" not in process
    builder.finish("completed", now_ms=6_000)
    assert process["lifecycle_status"] == "completed"
    assert process["status"] == "error"
    assert process["steps"][0]["status"] == "error"
    assert "recovered_by_step_id" not in process["steps"][0]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "waiting"])
def test_terminal_lifecycle_and_clock_are_immutable(status):
    builder = AssistantBlocksBuilder(started_at_ms=1_000)
    builder.start_tool("read_file", now_ms=2_000)
    builder.finish(status, now_ms=3_000)
    builder.set_final_text("An explanation, not a lifecycle decision.")
    builder.finish("completed", now_ms=90_000)
    process = _process(builder)
    assert process["lifecycle_status"] == status
    assert process["ended_at_ms"] == 3_000
    assert process["duration_ms"] == 2_000


def test_legacy_step_duration_without_timestamps_is_preserved():
    builder = AssistantBlocksBuilder()
    builder.start_tool("read_file")
    builder.end_tool("read_file", duration_ms=700)
    builder.finish("completed", now_ms=2_000)
    assert _process(builder)["duration_ms"] == 700
    builder.start_tool("late_notification")
    builder.end_tool("late_notification", duration_ms=500)
    assert _process(builder)["duration_ms"] == 700


def test_late_process_without_timestamps_cannot_reopen_finished_turn():
    builder = AssistantBlocksBuilder()
    builder.finish("cancelled", now_ms=2_000)
    builder.start_tool("late_notification")
    builder.end_tool("late_notification", duration_ms=500)
    process = _process(builder)
    assert process["lifecycle_status"] == "cancelled"
    assert process["ended_at_ms"] == 2_000
    assert process["duration_ms"] == 0


@pytest.mark.parametrize(
    "stop_reason,error,hitl_data,expected",
    [
        ("completed", None, None, "completed"),
        ("skill_terminal", None, None, "completed"),
        ("error", "provider_failed", None, "failed"),
        ("completed", "provider_failed", None, "failed"),
        ("credit_exhausted", None, None, "failed"),
        ("max_rounds", None, None, "failed"),
        ("cancelled", None, None, "cancelled"),
        ("waiting_resource", None, None, "waiting"),
        ("hitl_required", None, None, "waiting"),
        ("needs_hitl", None, None, "waiting"),
        ("completed", None, {"approval_token": "request-1"}, "waiting"),
        ("error", "provider_failed", {"approval_token": "request-1"}, "failed"),
    ],
)
def test_loop_outcome_not_answer_text_owns_process_lifecycle(stop_reason, error, hitl_data, expected):
    result = SimpleNamespace(stop_reason=stop_reason, error=error)
    assert _chat_result_process_lifecycle(result, hitl_data=hitl_data) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_status", [None, "answered", "completed", "partial", "blocked", "not_attempted"])
@pytest.mark.parametrize(
    "stop_reason,expected",
    [("completed", "completed"), ("max_rounds", "failed"), ("error", "failed"), ("hitl_required", "waiting"), ("cancelled", "cancelled")],
)
async def test_stream_terminal_clock_uses_loop_outcome_instead_of_failed_attempt(stop_reason, expected, outcome_status):
    if stop_reason == "max_rounds" and outcome_status:
        # An exhausted round budget with a host-accepted reply completed with
        # what it got; only a max_rounds turn without an accepted reply failed.
        expected = "completed"
    clock = [1.0]

    async def fake_context(*_args, **_kwargs):
        return "system", [], [], SimpleNamespace(
            workspace_id=None, task_id=None, runtime_envelope=None,
            tool_profile=None, allowed_tool_names=None, user=None, entity=None,
        )

    async def fake_loop(**kwargs):
        clock[0] = 2.0
        kwargs["on_tool_start"]("read_file", {"path": "missing.txt"})
        clock[0] = 3.0
        kwargs["on_tool_end"]("read_file", '{"error":"not found"}', 1_000, {"path": "missing.txt"})
        clock[0] = 4.0
        kwargs["on_tool_start"]("list_files", {"path": "/"})
        clock[0] = 15.0
        kwargs["on_tool_end"]("list_files", '{"files":[]}', 11_000, {"path": "/"})
        clock[0] = 20.0
        return AgenticResult(
            content="Explanation", messages=[], usage={}, rounds=2,
            tool_calls_made=["read_file", "list_files"], stop_reason=stop_reason,
            outcome={"status": outcome_status, "evidence_refs": [], "limitations": []} if outcome_status else None,
        )

    events = []
    with (
        patch("packages.core.services.chat_service.time", SimpleNamespace(time=lambda: clock[0])),
        patch("packages.core.services.chat_service.resolve_runtime_chat_context", new=fake_context),
        patch("packages.core.services.chat_service.runtime_execute_chat_agent_loop", new=fake_loop),
        patch("packages.core.services.chat_service.save_assistant_stream_interrupted_message", new=AsyncMock(return_value=None)),
        patch("packages.core.services.chat_service.record_chat_llm_usage", new=AsyncMock()),
    ):
        async for raw in stream_chat_response(
            "Inspect files", "conv-clock", entity_id="ent-clock", user_id="user-clock",
            persist_messages=False, runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
        ):
            if "\ndata: " in raw:
                events.append((raw.split("\n", 1)[0].removeprefix("event: "), json.loads(raw.split("\ndata: ", 1)[1])))

    tool_starts = [payload for event, payload in events if event == "tool_start"]
    assert tool_starts[-1]["assistant_blocks"][0]["lifecycle_status"] == "running"
    assert "outcome_status" not in tool_starts[-1]["assistant_blocks"][0]
    terminal = next(payload for event, payload in events if event == "stream_end")
    process = next(block for block in terminal["assistant_blocks"] if block["type"] == "process")
    assert process["lifecycle_status"] == expected
    assert process["started_at_ms"] == 1_000
    assert process["ended_at_ms"] == 20_000
    assert process["duration_ms"] == 19_000
    assert process["steps"][0]["status"] == "error"
    assert process.get("outcome_status") == (outcome_status if expected == "completed" else None)
