"""Presentation evidence must never rewrite an attempt or authorize a retry."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.api.routers.chat import _message_assistant_blocks
from apps.api.routers.workspace_chat import _to_message
from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.runtime.output_policy import PUBLIC_TOOL_FAILURE_MESSAGE, runtime_public_tool_payload
from packages.core.models.base import generate_ulid
from packages.core.models.task import Message
from packages.core.services import assistant_blocks
from packages.core.services.chat_service import run_chat_message


WAIT = "mcp__chrome__wait"
CLICK = "mcp__chrome__click_element"
WAIT_RESULT = {"ok": False, "status": "timeout", "next_required_tool": "mcp__chrome__read_page"}
CLICK_RESULT = {
    "ok": False, "status": "read_page_required", "reason": "snapshot_mismatch",
    "action_executed": False,
    "action_receipt": {"contract": "chrome_action_v1", "execution": "not_executed", "verification": "failed", "action_id": "stale-click"},
}


def _builder(name=WAIT, result=WAIT_RESULT):
    builder = assistant_blocks.AssistantBlocksBuilder(started_at_ms=1_000)
    builder.start_tool(name, {}, now_ms=2_000)
    builder.end_tool(name, arguments={}, result=PUBLIC_TOOL_FAILURE_MESSAGE,
                     structured_result=result, status="error", now_ms=3_000)
    return builder


def _process(blocks):
    return next(block for block in blocks if block["type"] == "process")


def _outcome(status):
    return {"status": status, "evidence_refs": ["current-call"], "limitations": [], "claims": []}


@pytest.mark.parametrize("name,result,kind", [
    (WAIT, WAIT_RESULT, "chrome_wait_observation_timeout"),
    (WAIT, {**WAIT_RESULT, "next_required_tool": "read_page"}, "chrome_wait_observation_timeout"),
    (CLICK, CLICK_RESULT, "chrome_stale_snapshot_rejection"),
    (CLICK, {key: value for key, value in CLICK_RESULT.items() if key != "action_receipt"}, "chrome_stale_snapshot_rejection"),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_only_exact_observation_and_nonexecuted_click_get_neutral_presentation(name, result, kind, wrapped):
    raw = {"error": "tool_error", "detail": json.dumps(result)} if wrapped else result
    original = deepcopy(raw)
    builder = _builder(name, raw)
    builder.finish("completed", now_ms=4_000)
    process = _process(builder.blocks())
    step = process["steps"][0]
    assert step.get("presentation") == {"kind": kind, "tone": "neutral"}
    assert process["status"] == step["status"] == "error"
    assert "recovered_by_step_id" not in step
    assert "verified_by_step_id" not in step
    assert raw == original
    assert runtime_public_tool_payload(builder.blocks())[0]["steps"][0]["presentation"] == step["presentation"]


@pytest.mark.parametrize("name,result", [
    (WAIT, {**WAIT_RESULT, "ok": True}),
    (WAIT, {**WAIT_RESULT, "status": "event_timeout"}),
    (WAIT, {**WAIT_RESULT, "next_required_tool": "mcp__chrome__upload"}),
    (WAIT, {**WAIT_RESULT, "pending_verification": {"action_id": "unknown-action"}}),
    (WAIT, {**WAIT_RESULT, "action_executed": 0}),
    (WAIT, {**WAIT_RESULT, "action_executed": 0.0}),
    ("mcp__chrome__upload", WAIT_RESULT),
    (CLICK, {**CLICK_RESULT, "reason": "another_reason"}),
    (CLICK, {**CLICK_RESULT, "action_executed": True}),
    (CLICK, {**CLICK_RESULT, "action_executed": None}),
    (CLICK, {**CLICK_RESULT, "action_receipt": {"contract": "chrome_action_v1", "execution": "unknown"}}),
    (CLICK, {**CLICK_RESULT, "pending_verification": {"action_id": "another-action"}}),
])
def test_other_failures_and_uncertain_effects_are_not_neutralized(name, result):
    step = _process(_builder(name, result).blocks())["steps"][0]
    assert "presentation" not in step
    assert "recovered_by_step_id" not in step


@pytest.mark.parametrize("name,result", [(WAIT, WAIT_RESULT), (CLICK, CLICK_RESULT)])
@pytest.mark.parametrize("verification", ["unknown", "pending", "unverified", "not_requested", None])
def test_uncertain_or_incomplete_chrome_receipts_are_not_neutralized(name, result, verification):
    raw = {**result, "action_receipt": {**CLICK_RESULT["action_receipt"], "verification": verification}}
    step = _process(_builder(name, raw).blocks())["steps"][0]
    assert "presentation" not in step
    assert step["status"] == "error"


def test_later_page_read_and_save_cannot_recover_the_wait_or_rejected_click():
    builder = _builder()
    builder.start_tool(CLICK, {})
    builder.end_tool(CLICK, structured_result=CLICK_RESULT, status="error")
    for name in ("mcp__chrome__read_page", CLICK):
        builder.start_tool(name, {})
        builder.end_tool(name, structured_result={"ok": True}, status="success")
    builder.finish("completed", now_ms=6_000, outcome=_outcome("completed"))
    process = _process(builder.blocks())
    assert process["outcome_status"] == "completed"
    assert process["status"] == "error"
    assert [step["status"] for step in process["steps"]] == ["error", "error", "success", "success"]
    assert not any("recovered_by_step_id" in step for step in process["steps"])


@pytest.mark.parametrize("status", ["answered", "completed", "partial", "blocked", "not_attempted"])
def test_terminal_outcome_is_bounded_separate_from_attempt_status_and_frozen(status):
    builder = _builder()
    builder.finish("completed", now_ms=4_000, outcome=_outcome(status))
    builder.finish("completed", now_ms=9_000, outcome=_outcome("answered"))
    process = _process(builder.blocks())
    assert process["outcome_status"] == status
    assert process["status"] == "error"
    assert process["ended_at_ms"] == 4_000


@pytest.mark.parametrize("lifecycle", ["running", "failed", "cancelled", "waiting"])
def test_noncompleted_lifecycle_does_not_gain_a_success_outcome(lifecycle):
    builder = _builder()
    builder.finish(lifecycle, now_ms=4_000, outcome=_outcome("completed"))
    assert "outcome_status" not in _process(builder.blocks())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason,lifecycle,outcome_status",
    [("completed", "completed", "completed"), ("hitl_required", "waiting", None)],
)
async def test_run_chat_message_persists_process_outcome_with_chrome_wait(
    stop_reason, lifecycle, outcome_status,
):
    class Session:
        async def commit(self):
            pass

    context = SimpleNamespace(
        workspace_id=None,
        agent_id=None,
        task_id=None,
        runtime_envelope=None,
        tool_profile=None,
        allowed_tool_names=None,
        runtime_skill_descriptors=[],
        user=None,
        entity=None,
        model="test-model",
        llm_metadata={},
    )
    result = AgenticResult(
        content="I checked the current page.",
        messages=[],
        usage={},
        rounds=1,
        tool_calls_made=[WAIT],
        stop_reason=stop_reason,
        outcome=_outcome("completed"),
    )

    async def fake_context(*_args, **_kwargs):
        return "system", [], [], context

    async def fake_loop(**kwargs):
        kwargs["on_tool_start"](WAIT, {})
        kwargs["on_tool_end"](WAIT, json.dumps(WAIT_RESULT), 12, {})
        return result

    save = AsyncMock(return_value=SimpleNamespace(id="assistant-process-1"))
    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=fake_context,
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=fake_loop,
        ),
        patch("packages.core.services.chat_service.add_message", new=save),
        patch(
            "packages.core.services.chat_service.resolve_author_subscription_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_runtime_learning",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "packages.core.services.chat_service.schedule_learning_candidate_applies",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_runtime_events",
            new=AsyncMock(),
        ),
        patch("packages.core.services.chat_service._append_turn_transcript", new=AsyncMock()),
    ):
        await run_chat_message(
            "Check the current page.",
            "conversation-process-1",
            entity_id="entity-process-1",
            user_id="user-process-1",
            db=Session(),
        )

    stored = save.await_args.kwargs
    assert json.loads(stored["tool_calls"][0]["raw_result"]) == WAIT_RESULT
    process = _process(stored["meta"]["assistant_blocks"])
    assert process["lifecycle_status"] == lifecycle
    assert process.get("outcome_status") == outcome_status
    assert process["steps"][0]["presentation"] == {
        "kind": "chrome_wait_observation_timeout",
        "tone": "neutral",
    }


@pytest.mark.parametrize("outcome", [None, {}, {"status": "unknown_effect"}, {"status": []}, "completed"])
def test_missing_or_unrecognized_outcome_does_not_infer_completion(outcome):
    builder = _builder()
    builder.finish("completed", now_ms=4_000, outcome=outcome)
    assert "outcome_status" not in _process(builder.blocks())


@pytest.mark.parametrize("history_surface", ["ordinary", "workspace"])
def test_history_projects_the_same_facts_as_live_without_rewriting_storage(history_surface):
    builder = _builder(CLICK, CLICK_RESULT)
    builder.finish("completed", now_ms=4_000)
    blocks = deepcopy(builder.blocks())
    step = _process(blocks)["steps"][0]
    step.pop("presentation", None)
    calls = [{"name": CLICK, "status": "error", "result": PUBLIC_TOOL_FAILURE_MESSAGE, "raw_result": json.dumps(CLICK_RESULT)}]
    metadata = {"assistant_blocks": blocks, "outcome": _outcome("completed")}
    original = deepcopy(metadata)
    message = Message(id=generate_ulid(), conversation_id=generate_ulid(), role="assistant",
                      content="Video saved", created_at=datetime.now(timezone.utc),
                      message_kind="text", author_kind="agent", meta=metadata, tool_calls=calls)
    projected = _message_assistant_blocks(message) if history_surface == "ordinary" else _to_message(message).assistant_blocks
    process = _process(projected)
    assert process["outcome_status"] == "completed"
    assert process["steps"][0]["presentation"] == {"kind": "chrome_stale_snapshot_rejection", "tone": "neutral"}
    assert process["steps"][0]["status"] == "error"
    assert "raw_result" not in json.dumps(projected)
    assert message.meta == original
    assert message.tool_calls == calls


def test_history_without_aligned_tool_receipts_keeps_its_existing_error():
    builder = _builder()
    builder.finish("completed", now_ms=4_000)
    blocks = deepcopy(builder.blocks())
    _process(blocks)["steps"][0].pop("presentation", None)
    projected = assistant_blocks.project_browser_display_names(blocks, [{"name": CLICK, "raw_result": json.dumps(CLICK_RESULT)}])
    assert projected == blocks
    assert "presentation" not in _process(projected)["steps"][0]


@pytest.mark.parametrize("history_surface", ["ordinary", "workspace"])
@pytest.mark.parametrize("serialized", [True, False])
@pytest.mark.parametrize("name,raw,expected", [
    (CLICK, {"error": "approval_continuation_unavailable", "message": "Knowledge document unavailable"},
     "The selected attachment is no longer accessible for approval."),
    ("mcp__chrome__upload", {"ok": False, "status": "error", "error": "Knowledge document unavailable",
                           "action_executed": False},
     "The selected attachment is missing or no longer accessible."),
    ("read_file", {"error": "File not found: /private/video.mp4"},
     "The requested file is missing or no longer accessible."),
])
def test_history_restores_safe_attempt_reason_from_aligned_raw_receipt(history_surface, serialized, name, raw, expected):
    builder = _builder(name, raw)
    builder.finish("completed", now_ms=4_000, outcome=_outcome("blocked"))
    calls = [{"name": name, "status": "error", "result": PUBLIC_TOOL_FAILURE_MESSAGE,
              "raw_result": json.dumps(raw) if serialized else raw}]
    metadata = builder.meta()
    original = deepcopy(metadata)
    message = Message(id=generate_ulid(), conversation_id=generate_ulid(), role="assistant",
                      content="The attachment is unavailable.", created_at=datetime.now(timezone.utc),
                      message_kind="text", author_kind="agent", meta=metadata, tool_calls=calls)
    projected = _message_assistant_blocks(message) if history_surface == "ordinary" else _to_message(message).assistant_blocks
    assert _process(projected)["outcome_status"] == "blocked"
    assert _process(projected)["steps"][0]["result_preview"] == expected
    assert _process(projected)["steps"][0]["status"] == "error"
    assert "/private" not in json.dumps(projected)
    assert message.meta == original
    assert message.tool_calls == calls
