"""Worker transcript checkpoint (Section W2).

A failed subagent attempt used to throw its whole transcript away: the retry
re-ran the prompt from scratch, repeating every tool call that had already
succeeded. The checkpoint built here is what the dispatcher parks on
``execution_steps.runtime_checkpoint`` so the next attempt can continue the
conversation instead.
"""
from __future__ import annotations

import json

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.constants.execution import (
    WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES,
    WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA,
)
from packages.core.workers import transcript


def _messages(*, tool_content: str = '{"ok": true}') -> list[dict]:
    return [
        {"role": "system", "content": "you are the worker"},
        {"role": "user", "content": "publish the post"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "write_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": tool_content},
        {"role": "assistant", "content": "done"},
    ]


def test_checkpoint_strips_the_system_message_and_records_bookkeeping():
    checkpoint = transcript.build_worker_transcript_checkpoint(
        _messages(), rounds=3, usage={"prompt_tokens": 10}, tool_calls_made=["write_file"],
    )

    assert checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    assert [m["role"] for m in checkpoint["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert checkpoint["rounds"] == 3
    assert checkpoint["usage"] == {"prompt_tokens": 10}
    assert checkpoint["tool_calls_made"] == ["write_file"]
    assert checkpoint["saved_at"]
    # JSONB-safe: the whole thing round-trips through json.
    assert json.loads(json.dumps(checkpoint)) == checkpoint


def test_checkpoint_is_none_when_nothing_usable_remains():
    assert transcript.build_worker_transcript_checkpoint(
        [], rounds=0, usage={}, tool_calls_made=[],
    ) is None
    assert transcript.build_worker_transcript_checkpoint(
        [{"role": "system", "content": "only the system prompt"}],
        rounds=0, usage={}, tool_calls_made=[],
    ) is None


def test_long_message_content_is_truncated_with_a_marker():
    huge = "x" * (8 * 1024 + 500)
    checkpoint = transcript.build_worker_transcript_checkpoint(
        _messages(tool_content=huge), rounds=1, usage={}, tool_calls_made=[],
    )
    tool_message = next(m for m in checkpoint["messages"] if m["role"] == "tool")

    assert len(tool_message["content"]) < len(huge)
    assert "[... truncated 500 chars ...]" in tool_message["content"]
    # Short messages are left alone.
    assert checkpoint["messages"][0]["content"] == "publish the post"


def test_oldest_tool_results_are_dropped_until_the_checkpoint_fits(monkeypatch):
    messages = [{"role": "user", "content": "go"}]
    for index in range(6):
        call_id = f"c{index}"
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": "search", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": call_id, "content": "r" * 600})

    def build():
        return transcript.build_worker_transcript_checkpoint(
            messages, rounds=6, usage={}, tool_calls_made=["search"] * 6,
        )

    # Size the budget from the real serialized checkpoint so that exactly
    # five results must go: four drops still leave it over budget, five fit.
    full_size = len(json.dumps(build(), ensure_ascii=False).encode("utf-8"))
    saving_per_drop = 600 - len(transcript._DROPPED_TOOL_RESULT_MARKER)
    budget = full_size - 5 * saving_per_drop + 100
    monkeypatch.setattr(transcript, "WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES", budget)

    checkpoint = build()

    assert checkpoint is not None
    assert len(json.dumps(checkpoint, ensure_ascii=False).encode("utf-8")) <= budget
    tool_contents = [m["content"] for m in checkpoint["messages"] if m["role"] == "tool"]
    # Oldest first: the five early results are replaced by a marker, the
    # newest survives.
    assert tool_contents[:5] == [transcript._DROPPED_TOOL_RESULT_MARKER] * 5
    assert tool_contents[-1] == "r" * 600
    assert transcript.worker_transcript_dropped_tool_result_ids(checkpoint) == {
        "c0",
        "c1",
        "c2",
        "c3",
        "c4",
    }
    # The conversation structure is intact: every tool call still has a result.
    assert len(tool_contents) == 6


def test_checkpoint_is_none_when_it_cannot_fit_even_after_dropping_tool_results(monkeypatch):
    monkeypatch.setattr(transcript, "WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES", 200)
    messages = [
        {"role": "user", "content": "u" * 300},
        {"role": "assistant", "content": "a" * 300},
    ]
    assert transcript.build_worker_transcript_checkpoint(
        messages, rounds=1, usage={}, tool_calls_made=[],
    ) is None


def test_dangling_tool_calls_without_results_are_cut_from_the_tail():
    """A loop that died mid-tool-call leaves an assistant tool_call with no
    tool result. Replaying that to a provider is a 400; the checkpoint ends
    before the dangling call instead."""
    messages = _messages() + [
        {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c9", "function": {"name": "publish", "arguments": "{}"}}],
        },
    ]
    checkpoint = transcript.build_worker_transcript_checkpoint(
        messages, rounds=2, usage={}, tool_calls_made=[],
    )
    assert checkpoint["messages"][-1] == {"role": "assistant", "content": "done"}


def test_non_string_content_is_flattened_to_text():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "look at this"},
                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        {"role": "assistant", "content": "ok"},
    ]
    checkpoint = transcript.build_worker_transcript_checkpoint(
        messages, rounds=1, usage={}, tool_calls_made=[],
    )
    assert isinstance(checkpoint["messages"][0]["content"], str)
    assert "look at this" in checkpoint["messages"][0]["content"]


def test_is_worker_transcript_checkpoint_distinguishes_the_two_checkpoint_kinds():
    ours = transcript.build_worker_transcript_checkpoint(
        _messages(), rounds=1, usage={}, tool_calls_made=[],
    )
    request_input = RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "x"}], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "call", "name": "request_input", "arguments": {}},
    ).to_dict()

    assert transcript.is_worker_transcript_checkpoint(ours) is True
    assert transcript.is_worker_transcript_checkpoint(request_input) is False
    assert transcript.is_worker_transcript_checkpoint(None) is False
    assert transcript.is_worker_transcript_checkpoint({"schema_version": WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA}) is False
    assert transcript.is_worker_transcript_checkpoint(
        {"schema_version": WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA, "messages": "nope"}
    ) is False


def test_resume_messages_never_include_a_system_message():
    checkpoint = transcript.build_worker_transcript_checkpoint(
        _messages(), rounds=1, usage={}, tool_calls_made=[],
    )
    # Even a hand-written checkpoint that smuggled one in is cleaned on the
    # way out: the agentic loop prepends its own system prompt.
    checkpoint["messages"].insert(0, {"role": "system", "content": "stale"})

    resumed = transcript.worker_transcript_resume_messages(checkpoint)

    assert resumed[0]["role"] == "user"
    assert all(m["role"] != "system" for m in resumed)
    assert resumed is not checkpoint["messages"]


def test_default_budget_is_the_shared_constant():
    assert transcript.WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES == WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES
