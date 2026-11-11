"""A sandbox skill handoff must leave the turn enough rounds to finish the workflow.

Seen end-to-end: a chat turn planned for 8 tool rounds allocated a sandbox,
read the contract, and ran out of rounds before writing the script.
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from packages.core.ai import agentic_loop
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint


def test_handoff_guarantees_a_minimum_remaining_budget():
    extra = agentic_loop.SANDBOX_HANDOFF_MIN_REMAINING_ROUNDS
    assert extra >= 8
    # An 8-round chat turn handing off at round 2 gets 2 + extra.
    assert agentic_loop._rounds_after_sandbox_handoff(8, 2) == 2 + extra
    # A generous budget is never shrunk, and nothing exceeds the loop default.
    assert agentic_loop._rounds_after_sandbox_handoff(200, 2) == 200
    assert agentic_loop._rounds_after_sandbox_handoff(8, 195) == agentic_loop.DEFAULT_MAX_ROUNDS
    # A caller that asked for more than the default keeps every round.
    assert agentic_loop._rounds_after_sandbox_handoff(500, 2) == 500


def test_extension_is_latched_once_per_skill_per_turn():
    """One message asking for an xlsx and then a docx hands off twice; the
    second workflow needs its own rounds (the docx save was cut off at 13
    rounds when the latch was per turn). Re-handing off the same skill must
    not extend again."""
    source = Path("packages/core/ai/agentic_loop.py").read_text(encoding="utf-8")
    assert source.count("sandbox_budget_extended_for.add(handoff_skill)") == 2
    assert source.count("handoff_skill not in sandbox_budget_extended_for") == 2
    # The latch is bound before the resume/live branch split, so a resumed
    # turn (checkpoint present) reads a bound name.
    init = source.index("sandbox_budget_extended_for: set[str] = set()")
    assert init < source.index("resume_calls = [checkpoint.pending_tool_call")

    assert agentic_loop._sandbox_handoff_skill_name({"status": "sandbox_ready", "skill": "DOCX"}) == "docx"
    assert agentic_loop._sandbox_handoff_skill_name({"status": "sandbox_ready"}) == "<unnamed>"


def test_generate_file_handoff_is_recognised_like_invoke_skill():
    assert {"invoke_skill", "generate_file"} <= set(agentic_loop._SANDBOX_HANDOFF_TOOL_NAMES)
    handoff = {"status": "sandbox_ready", "sandbox_id": "sb_1", "loaded_tools": ["sandbox"]}
    assert agentic_loop._invoke_skill_schema_load_names(handoff) == ["sandbox"]


def test_loop_extends_the_budget_on_both_handoff_paths():
    """The live tool path and the durable resume path (where a reserved
    sandbox's skill call is re-executed) must both extend the budget."""
    source = Path("packages/core/ai/agentic_loop.py").read_text(encoding="utf-8")
    assert source.count("extended_rounds = _rounds_after_sandbox_handoff(") == 2
    assert source.count("hard_max_rounds=hard_max_rounds,") == 2
    assert source.count("max_rounds = extended_rounds") == 2
    live = source.index('if tc["name"] in _SANDBOX_HANDOFF_TOOL_NAMES')
    resume = source.index("in _SANDBOX_HANDOFF_TOOL_NAMES\n")
    assert resume < live, "the resume block precedes the live tool loop"


async def test_a_turn_keeps_its_handed_off_sandbox_alive_until_it_ends(monkeypatch):
    """A workflow agent spent four ~100 s model rounds writing a manifest; the
    staging runner reclaims a sandbox idle for 300 s, so the turn came back to
    sandbox_expired and lost every file in it."""
    import asyncio

    from packages.core.ai.runtime import sandbox as runtime_sandbox
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    touched: list[str] = []

    class FakeClient:
        async def touch(self, sandbox_id):
            if sandbox_id == "sb_gone":
                raise SandboxNotFoundError("gone")
            touched.append(sandbox_id)

        async def close(self):
            return None

    async def fake_client(_sandbox_id):
        return FakeClient()

    async def fake_turn(*_args, **_kwargs):
        agentic_loop._note_turn_sandbox({"status": "sandbox_ready", "sandbox_id": "sb_live"})
        agentic_loop._note_turn_sandbox({"status": "sandbox_ready", "sandbox_id": "sb_gone"})
        agentic_loop._note_turn_sandbox({"status": "failed", "sandbox_id": "sb_not_handed"})
        await asyncio.sleep(0.35)  # a long model round with no sandbox call
        return "done"

    monkeypatch.setattr(runtime_sandbox, "runtime_sandbox_client", fake_client)
    monkeypatch.setattr(agentic_loop, "SANDBOX_KEEPALIVE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(agentic_loop, "_agentic_loop", fake_turn)

    assert await agentic_loop.agentic_loop("system", "user") == "done"
    beats = len(touched)
    assert beats >= 3 and set(touched) == {"sb_live"}
    await asyncio.sleep(0.2)
    assert len(touched) == beats, "the keepalive stops when the turn ends"
    assert agentic_loop._TURN_SANDBOXES.get() is None


def test_both_handoff_paths_register_the_turn_sandbox():
    source = Path("packages/core/ai/agentic_loop.py").read_text(encoding="utf-8")
    assert source.count("_note_turn_sandbox(tool_result)") == 2


@pytest.mark.unit
@pytest.mark.parametrize("failure_reason", ["sandbox_allocation_failed", "sandbox_queue_timeout", "sandbox_capacity_timeout", "sandbox_runner_unavailable", "sandbox_allocation_rejected"])
@pytest.mark.parametrize("cancel_summary", [False, True])
async def test_exhausted_allocation_gets_one_billed_model_summary_without_replaying_tools(monkeypatch, cancel_summary, failure_reason):
    calls = [
        {"id": "pdf", "name": "generate_file", "arguments": {"format": "pdf"}},
        {"id": "send", "name": "send_email", "arguments": {}},
    ]
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "Help with the user's files."},
            {"role": "user", "content": "Fill the rental PDF using the details I supplied."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in calls
            ]},
        ],
        rounds=2, usage={}, tool_calls_made=["read_file", "request_input"],
        pending_tool_call=calls[0], remaining_tool_calls=calls[1:],
        pending_tool_result={"id": "pdf", "content": json.dumps({
            "status": "error", "message": "Dependencies could not be installed.",
            "error": failure_reason, "allocation_attempts": 5,
        }), "terminal_failure": failure_reason},
    )
    events = []
    cancelled = [False]
    summary = "I have your details, but the PDF could not be prepared because the file tools could not start."

    async def no_execution(*_args, **_kwargs):
        pytest.fail("A failed allocation must not retry tools or execute the queued suffix")

    async def final(messages, **kwargs):
        events.append("model")
        assert "tools" not in kwargs
        assert any("details I supplied" in str(m.get("content")) for m in messages)
        assert [m["tool_call_id"] for m in messages if m.get("role") == "tool"] == ["pdf"]
        assert [c["id"] for m in messages for c in m.get("tool_calls", [])] == ["pdf"]
        assert any("Dependencies could not be installed" in str(m.get("content")) for m in messages)
        return summary, {"input_tokens": 10, "output_tokens": 12}

    async def reserve(*_args):
        events.append("reserve")

    async def settle(*_args):
        events.append("settle")

    def on_tool_end(*_args):
        cancelled[0] = cancel_summary

    monkeypatch.setattr(agentic_loop, "runtime_execute_agentic_round_tool_completion", no_execution)
    monkeypatch.setattr(agentic_loop, "runtime_execute_agentic_final_completion", final)
    result = await agentic_loop.agentic_loop(
        system_prompt="Help with files", user_message="continue", tools=[],
        tool_executor=no_execution, resume_checkpoint=checkpoint.to_dict(),
        forced_tool_calls=[calls[1]], on_llm_call_before=reserve, on_llm_usage_settled=settle,
        on_tool_end=on_tool_end, is_cancelled=lambda: cancelled[0],
    )
    if cancel_summary:
        assert events == []
        assert result.stop_reason == "cancelled"
        return
    assert events == ["reserve", "model", "settle"]
    assert result.content == summary
    assert result.error == failure_reason
    assert result.stop_reason == "error"
    from packages.core.ai.runtime.output_policy import runtime_assistant_result_meta, runtime_public_assistant_message_content

    assert runtime_public_assistant_message_content(result.content, runtime_assistant_result_meta(result)) == summary
