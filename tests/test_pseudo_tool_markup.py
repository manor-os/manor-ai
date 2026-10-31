"""Fabricated tool-call markup in model text is detected, repaired once, then refused.

E8: a Skill child with zero tools answered with invented ``<tool_call>`` /
``<tool_response>`` blocks and the parent recorded it as confirmed work.
"""
from __future__ import annotations

import importlib
import json

import pytest

from packages.core.ai.runtime.output_policy import runtime_pseudo_tool_markup


@pytest.mark.parametrize("text,kind", [
    ('<tool_call>{"name": "notion", "arguments": {}}</tool_call>', "tool_call"),
    ('Done.\n<tool_response>{"request_id": "b5a7"}</tool_response>', "tool_response"),
    ('<tool_result>ok</tool_result>', "tool_result"),
    ('<tool_use id="1">x</tool_use>', "tool_use"),
    ('<function_calls>\n<invoke name="search">\n<parameter name="q">x</parameter>\n</invoke>\n</function_calls>',
     "function_calls"),
    ('<function_results>ok</function_results>', "function_results"),
    ('<invoke name="search">\n<parameter name="q">x</parameter>\n</invoke>', "invoke"),
    ('<invoke name="search">\n<parameter name="q">x</parameter>\n</invoke>', "parameter"),
    ('<xyz:invoke name="search"><xyz:parameter name="q">x</xyz:parameter></xyz:invoke>', "invoke"),
    ('<function_calls><invoke name="s"></invoke></function_calls>', "function_calls"),
    ('[TOOL_CALLS] [{"name": "search"}]', "[TOOL_CALLS]"),
    ('<|tool_call|>{"name": "search"}', "<|tool_call|>"),
    ('<｜tool▁calls▁begin｜>search', "<|tool_call|>"),
    ('<function=search>{"q": "x"}</function>', "<function=name>"),
])
def test_detector_flags_tool_call_delimiters(text, kind):
    assert kind in runtime_pseudo_tool_markup(text)


@pytest.mark.parametrize("text", [
    "",
    "I made a tool call to the search API and it returned three results.",
    "Use the `<tool_call>` tag only inside function calling.",
    "```xml\n<tool_call>{\"name\": \"x\"}</tool_call>\n```",
    "~~~\n<function_calls><invoke name=\"x\"></invoke></function_calls>\n~~~",
    "<div class=\"invoker\">Hello</div> <p>parameters</p> <span>tool</span>",
    "The function=foo pattern and a <toolbar> element are fine.",
    "Compare a < b and c > d; tool_call is a word here.",
    # Configuration and document formats that reuse the words.
    '<Context><Parameter name="debug" value="false" override="false"/></Context>',
    '<service name="Echo"><parameter name="ServiceClass">org.example.Echo</parameter></service>',
    '<sequence><invoke partnerLink="client" operation="initiate"/></sequence>',
    '<funcsynopsis><parameter>path</parameter></funcsynopsis>',
    "Put the value inside a <parameter> element.",
    "<invoke-button label=\"Run\"></invoke-button>",
    "if x <invoke> y then stop",
    '<invoke name="search"></invoke>',
    "<TOOL_CALL>shouting</TOOL_CALL>",
])
def test_detector_ignores_prose_code_and_html(text):
    assert runtime_pseudo_tool_markup(text) == []


FAKE_CALL = '<tool_call>{"name": "notion", "arguments": {}}</tool_call>\n<tool_response>{"ok": true}</tool_response>'


def _scripted_loop(monkeypatch, replies: list[str]):
    seen: list[list[dict]] = []
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")

    async def tool_round(messages, tools, **_kwargs):
        seen.append(list(messages))
        return replies[len(seen) - 1], None, {"total": 1}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", tool_round)
    return loop_module, seen


async def _no_tool(name, args):
    raise AssertionError("no tool may run")


@pytest.mark.asyncio
async def test_loop_repairs_pseudo_tool_markup_once(monkeypatch):
    loop_module, seen = _scripted_loop(monkeypatch, [FAKE_CALL, "Notion is not connected, so I could not create the page."])
    events: list[str] = []

    async def stream(event_type, _payload):
        events.append(event_type)

    result = await loop_module.agentic_loop(
        system_prompt="test", user_message="create a notion page",
        tools=[{"type": "function", "function": {"name": "search_tools"}}],
        tool_executor=_no_tool, stream_handler=stream, max_rounds=5,
    )

    assert result.stop_reason == "completed"
    assert result.content == "Notion is not connected, so I could not create the page."
    assert len(seen) == 2
    repair = str(seen[1][-1]["content"])
    assert "No tool was executed" in repair and "function calling" in repair
    assert "put it in a code block" in repair
    # The note refers to a reply the model can see in its transcript.
    assert seen[1][-2] == {"role": "assistant", "content": FAKE_CALL}
    assert "text_reset" in events


@pytest.mark.asyncio
async def test_loop_fails_when_pseudo_tool_markup_repeats(monkeypatch):
    loop_module, seen = _scripted_loop(monkeypatch, [FAKE_CALL, FAKE_CALL])

    result = await loop_module.agentic_loop(
        system_prompt="test", user_message="create a notion page",
        tools=[{"type": "function", "function": {"name": "search_tools"}}],
        tool_executor=_no_tool, max_rounds=5,
    )

    assert result.stop_reason == "error"
    assert result.error == "pseudo_tool_markup"
    assert "<tool_call>" not in result.content and "<tool_response>" not in result.content
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_loop_accepts_markup_inside_code_fences(monkeypatch):
    answer = "Tool calls look like this:\n```\n<tool_call>{}</tool_call>\n```"
    loop_module, seen = _scripted_loop(monkeypatch, [answer])

    result = await loop_module.agentic_loop(
        system_prompt="test", user_message="explain the syntax",
        tools=[{"type": "function", "function": {"name": "search_tools"}}],
        tool_executor=_no_tool, max_rounds=5,
    )

    assert result.stop_reason == "completed" and result.content == answer
    assert len(seen) == 1


def _child_result(content: str, *, tools_used: list[str] | None = None, stop_reason: str = "completed") -> dict:
    return {
        "skill": "mcp_notion", "content": content, "tools_used": [] if tools_used is None else tools_used,
        "rounds": 1, "stop_reason": stop_reason, "tool_observations": {}, "tool_evidence": {},
        "usage": {"total": 1}, "control": {}, "error": None,
    }


def test_invoke_skill_result_with_fabricated_tool_activity_fails_without_its_content():
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    payload = json.loads(runtime_format_invoke_skill_result(
        "mcp_notion", _child_result(f"Created the page.\n{FAKE_CALL}"),
    ))

    assert payload["status"] == "failed"
    assert payload["error"] == "fabricated_tool_activity"
    assert payload["goal_status"] == "unverified"
    assert "<tool_call>" not in json.dumps(payload) and "Created the page" not in payload["content"]


def test_markup_after_real_tool_calls_is_left_to_the_loop():
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    payload = json.loads(runtime_format_invoke_skill_result(
        "mcp_notion", _child_result(f"Done.\n{FAKE_CALL}", tools_used=["mcp__notion__search"]),
    ))

    assert payload.get("error") != "fabricated_tool_activity"


def test_completed_skill_without_child_evidence_is_marked_unverified():
    from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result

    payload = json.loads(runtime_format_invoke_skill_result("writer", _child_result("Here is the draft.")))

    assert payload["status"] == "completed"
    assert payload["execution"]["goal_status"] == "unverified"


@pytest.mark.asyncio
async def test_fabricated_skill_result_is_a_no_effect_carrier_for_the_parent_ledger(monkeypatch):
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.ai.runtime.receipts import EffectState, runtime_tool_effect_state
    from packages.core.ai.runtime.skills import runtime_invoke_skill_action
    from packages.core.services import skill_service

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def get_skill(_db, _requested):
        return SimpleNamespace(id="01M36ABH8VXTSN9Q5CB6G59RHJ", slug="mcp_notion", name="mcp_notion",
                               status="active", entity_id=None, tools=["mcp__notion__search"])

    async def fake_invoke(db, skill_id, entity_id, input_text, **kwargs):
        return _child_result(f"Created.\n{FAKE_CALL}")

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(skill_service, "get_skill", get_skill)
    monkeypatch.setattr(skill_service, "invoke_skill", fake_invoke)
    context = SimpleNamespace(
        active_user_message="", agent_id=None, workspace_id=None, conversation_id=None,
        allowed_tool_names={"invoke_skill"}, tool_profile=None, manual_skill_selected=False,
    )

    result = await runtime_invoke_skill_action(entity_id="entity", skill_id="mcp_notion", runtime_context=context)

    assert json.loads(result)["error"] == "fabricated_tool_activity"
    assert runtime_tool_effect_state("invoke_skill", {"skill_id": "mcp_notion"}, result) is EffectState.NONE


async def _invoke_with_child_result(monkeypatch, child: dict):
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.ai.runtime.skills import runtime_invoke_skill_action
    from packages.core.services import skill_service

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def get_skill(_db, _requested):
        return SimpleNamespace(id="01M36ABH8VXTSN9Q5CB6G59RHJ", slug="mcp_notion", name="mcp_notion",
                               status="active", entity_id=None, tools=["mcp__notion__search"])

    async def fake_invoke(db, skill_id, entity_id, input_text, **kwargs):
        return child

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(skill_service, "get_skill", get_skill)
    monkeypatch.setattr(skill_service, "invoke_skill", fake_invoke)
    context = SimpleNamespace(
        active_user_message="", agent_id=None, workspace_id=None, conversation_id=None,
        allowed_tool_names={"invoke_skill"}, tool_profile=None, manual_skill_selected=False,
    )
    return await runtime_invoke_skill_action(entity_id="entity", skill_id="mcp_notion", runtime_context=context)


@pytest.mark.asyncio
async def test_child_loop_markup_refusal_reaches_the_parent_as_a_no_effect_failure(monkeypatch):
    """The real E8 shape: the child loop already caught the markup and dropped it."""
    from packages.core.ai.runtime.agentic_loop_prompts import PSEUDO_TOOL_MARKUP_FINAL_CONTENT
    from packages.core.ai.runtime.receipts import EffectState, runtime_tool_effect_state

    child = {**_child_result(PSEUDO_TOOL_MARKUP_FINAL_CONTENT, stop_reason="error"), "error": "pseudo_tool_markup"}

    result = await _invoke_with_child_result(monkeypatch, child)

    payload = json.loads(result)
    assert payload["error"] == "fabricated_tool_activity"
    assert payload["goal_status"] == "unverified"
    assert runtime_tool_effect_state("invoke_skill", {"skill_id": "mcp_notion"}, result) is EffectState.NONE


@pytest.mark.asyncio
async def test_child_markup_refusal_after_real_tool_calls_keeps_its_effect_state(monkeypatch):
    from packages.core.ai.runtime.agentic_loop_prompts import PSEUDO_TOOL_MARKUP_FINAL_CONTENT
    from packages.core.ai.runtime.receipts import EffectState, runtime_tool_effect_state

    child = {
        **_child_result(PSEUDO_TOOL_MARKUP_FINAL_CONTENT, stop_reason="error", tools_used=["mcp__notion__search"]),
        "error": "pseudo_tool_markup",
    }

    result = await _invoke_with_child_result(monkeypatch, child)

    assert json.loads(result).get("error") != "fabricated_tool_activity"
    assert runtime_tool_effect_state("invoke_skill", {"skill_id": "mcp_notion"}, result) is not EffectState.NONE


@pytest.mark.parametrize("final_text,expect_markup", [(FAKE_CALL, True), ("Summary of the research so far.", False)])
@pytest.mark.asyncio
async def test_max_rounds_final_summary_is_checked_for_markup(monkeypatch, final_text, expect_markup):
    from packages.core.ai.runtime.agentic_loop_prompts import PSEUDO_TOOL_MARKUP_FINAL_CONTENT

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    final_calls = []

    async def tool_round(messages, tools, **_kwargs):
        return "", [{"id": f"c{len(messages)}", "name": "web_search", "arguments": {"q": "x"}}], {"total": 1}

    async def final_round(messages, **_kwargs):
        final_calls.append(messages)
        return final_text, {"total": 1}

    async def executor(name, args):
        return '{"results": []}'

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", tool_round)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", final_round)

    result = await loop_module.agentic_loop(
        system_prompt="test", user_message="research",
        tools=[{"type": "function", "function": {"name": "web_search"}}],
        tool_executor=executor, max_rounds=1,
    )

    assert len(final_calls) == 1  # no retry on the final summary
    # max_rounds stays the stop reason: Chrome reports a blocked outcome from it.
    assert result.stop_reason == "max_rounds"
    if expect_markup:
        assert result.error == "pseudo_tool_markup"
        assert result.content == PSEUDO_TOOL_MARKUP_FINAL_CONTENT
        assert result.messages[-1]["content"] == PSEUDO_TOOL_MARKUP_FINAL_CONTENT
    else:
        assert result.error is None and result.content == final_text
