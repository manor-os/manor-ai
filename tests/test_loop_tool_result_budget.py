"""What the model sees of a tool result on its first delivery in the loop.

The dedicated budgets for skill handoffs and paged reads (ad164fccf) were
keyed to "no explicit budget", while the loop always passes one; from
2b144f963 on, every handoff and page was cut to 4,000 characters again and no
test noticed because they called the compactor directly. These go through the
loop.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest

loop = importlib.import_module("packages.core.ai.agentic_loop")


def _delivered(monkeypatch, result: str, **loop_kwargs) -> str:
    """Run one tool round and return the tool message the model received."""
    sent: list[list[dict]] = []

    async def complete(messages, tools, **kwargs):
        sent.append(list(messages))
        if len(sent) == 1:
            return "", [{"id": "call_1", "name": "lookup", "arguments": {}}], {"total": 1}
        return "done", [], {"total": 1}

    async def execute(name, args):
        return result

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    asyncio.run(loop.agentic_loop(
        system_prompt="system", user_message="go",
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        tool_executor=execute, max_rounds=3, **loop_kwargs,
    ))
    return next(m["content"] for m in sent[1] if m.get("role") == "tool")


def _config(chars: int) -> str:
    """A Workspace-config-like JSON result of about ``chars`` characters."""
    return json.dumps({"workspace": {"rules": [f"rule {i}: " + "x" * 80 for i in range(chars // 90)]}})


def test_a_skill_handoff_reaches_the_model_whole(monkeypatch):
    skill = "# Skill\n" + "".join(f"step {i}: do the thing carefully\n" for i in range(700))
    handoff = json.dumps({"status": loop._SKILL_HANDOFF_STATUS, "content": skill})
    assert 20_000 < len(handoff) < loop.SKILL_HANDOFF_RESULT_MAX_CHARS
    seen = _delivered(monkeypatch, handoff)
    assert "step 699" in seen


def test_a_paged_read_gets_its_page_and_honest_offsets(monkeypatch):
    text = "".join(f"line {i}\n" for i in range(3_000))
    page = json.dumps({"content": text, "offset": 0, "next_offset": len(text), "returned_chars": len(text)})
    seen = json.loads(_delivered(monkeypatch, page))
    assert len(seen["content"]) > loop.TOOL_RESULT_MAX_CHARS
    assert seen["next_offset"] == len(seen["content"]) and text.startswith(seen["content"])


@pytest.mark.parametrize("chars", [6_000, 11_000])
def test_config_sized_json_arrives_intact_in_task_loops(monkeypatch, chars):
    result = _config(chars)
    assert loop.TOOL_RESULT_MAX_CHARS < len(result) <= loop.LOOP_TOOL_RESULT_MAX_CHARS
    assert _delivered(monkeypatch, result) == result


def test_larger_json_fills_the_loop_budget_and_says_so(monkeypatch):
    seen = _delivered(monkeypatch, _config(30_000))
    # Long lists keep as many items as fit, not a fixed 20 (~2k of the budget).
    assert loop.LOOP_TOOL_RESULT_MAX_CHARS * 0.8 < len(seen) <= loop.LOOP_TOOL_RESULT_MAX_CHARS
    payload = json.loads(seen)
    assert "_tool_result_truncated" in payload and payload["workspace"]["rules"][-1]["_omitted_items"] > 0


def test_a_transcript_budget_is_never_lowered_by_a_dedicated_one(monkeypatch):
    result = _config(30_000)
    assert _delivered(monkeypatch, result, tool_result_max_chars=40_000) == result
    text = "".join(f"line {i}\n" for i in range(3_000))
    page = json.dumps({"content": text, "offset": 0, "next_offset": len(text), "returned_chars": len(text)})
    assert loop.PAGED_TEXT_RESULT_MAX_CHARS < len(page) < 40_000
    seen = json.loads(_delivered(monkeypatch, page, tool_result_max_chars=40_000))
    assert seen["content"] == text  # whole page under the 40k budget, not cut to the 12k paged budget
    longer = "".join(f"line {i}\n" for i in range(6_000))
    page = json.dumps({"content": longer, "offset": 0, "next_offset": len(longer), "returned_chars": len(longer)})
    seen = json.loads(_delivered(monkeypatch, page, tool_result_max_chars=40_000))
    # Over the 40k budget it is cut near 40k, never down to the 12k paged budget.
    assert 30_000 < len(seen["content"]) and seen["next_offset"] == len(seen["content"])


def test_recompaction_with_an_explicit_budget_is_not_a_first_delivery():
    skill = "x" * 20_000
    handoff = json.dumps({"status": loop._SKILL_HANDOFF_STATUS, "content": skill})
    assert len(loop._compact_tool_result_for_context("invoke_skill", handoff, 4_000)) <= 4_000
    assert len(loop._compact_tool_result_for_context("invoke_skill", handoff, 4_000, first_delivery=True)) > 4_000
