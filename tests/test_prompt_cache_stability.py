"""The request prefix (tools, system prompt, history) must not change between turns.

Providers cache a prompt from its first token: one changed character in the
system prompt and every history token after it is billed again. Staging on
2026-10-02 measured 0 cached tokens on follow-up turns because the system
prompt carried per-message sections. Split builds send that content after the
history instead.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from packages.core.ai.runtime.agentic_loop_prompts import (
    runtime_host_feedback_message,
    runtime_is_turn_context_message,
    runtime_with_turn_context,
)
from packages.core.ai.runtime.prompt_adapter import ChatContext, PromptBuilder, build_default_prompt_builder
from packages.core.ai.runtime.prompt_sections import runtime_turn_scoped_prompt_section_names

# Messages that switch on as many message-dependent sections as possible.
MESSAGES = (
    "hello",
    "你好，帮我总结一下这周的进展",
    "做一个 10 页的 PPT 介绍我们的产品",
    "用 Claude Code 在本地仓库里修一下这个 bug",
    "APPROVED: continue the approved operation",
    "帮我写一个 python 脚本把 csv 转成 json",
    "把这份报告保存到 workspace 的知识库里",
    "给小红书写一篇草稿，先别发布",
    "这个任务进行到哪了？更新一下进度",
)


async def _build(message: str, *, split: bool) -> tuple[str, ChatContext]:
    ctx = ChatContext(entity_id="entity-1", user_id="user-1", active_user_message=message)
    prompt = await build_default_prompt_builder().build(ctx, split_turn_context=split)
    return prompt, ctx


def test_split_system_prompt_is_identical_for_every_message():
    async def run():
        return [await _build(message, split=True) for message in MESSAGES]

    built = asyncio.run(run())
    prompts = {prompt for prompt, _ in built}
    assert len(prompts) == 1, "a section that depends on the message is not marked turn_scoped"
    # The per-message content still reaches the model, after the history.
    assert len({ctx.runtime_turn_context for _, ctx in built}) > 1
    assert all("Runtime: current_time=" in ctx.runtime_turn_context for _, ctx in built)


def test_unsplit_builds_keep_every_section_in_the_system_prompt():
    async def run():
        return await _build("你好，帮我总结一下这周的进展", split=False)

    prompt, ctx = asyncio.run(run())
    assert "Runtime: current_time=" in prompt
    assert ctx.runtime_turn_context == ""


def test_split_moves_only_turn_scoped_sections_and_budgets_them_separately():
    memory = {"text": "remember this " * 50}

    async def run():
        builder = PromptBuilder()
        builder.add(lambda _: "Stable rules.", name="rules")
        builder.add(lambda _: memory["text"], name="agent_memories", turn_scoped=True)
        ctx = ChatContext(entity_id="entity-1", active_user_message="anything")
        return await builder.build(ctx, split_turn_context=True), ctx

    prompt, ctx = asyncio.run(run())
    assert prompt == "Stable rules."
    assert ctx.runtime_turn_context.startswith("remember this")
    assert [section["key"] for section in ctx.runtime_context_sections] == ["rules"]
    assert [section["key"] for section in ctx.runtime_turn_context_sections] == ["agent_memories"]
    # A much larger per-turn memory does not touch the stable prompt.
    memory["text"] = "remember this " * 5000
    assert asyncio.run(run())[0] == "Stable rules."


def test_every_turn_scoped_section_has_a_renderer_and_core_identity_stays_stable():
    from packages.core.ai.runtime.prompt_sections import runtime_prompt_section_renderers

    turn = runtime_turn_scoped_prompt_section_names()
    assert turn <= set(runtime_prompt_section_renderers())
    assert {"agent_memories", "available_skills_section", "runtime_context", "extra_context_section"} <= turn
    assert not turn & {"agent_identity", "output_discipline", "tool_usage_guidance", "workspace_operating_memory"}


def test_turn_context_sits_right_before_the_current_request_in_every_round():
    history = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "earlier request"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "user", "content": "current request"},
    ]
    first_round = runtime_with_turn_context(history, "memories for this message")
    later_round = runtime_with_turn_context([
        *history,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "results"},
        runtime_host_feedback_message("retry with a narrower query"),
    ], "memories for this message")

    for request in (first_round, later_round):
        index = next(i for i, m in enumerate(request) if runtime_is_turn_context_message(m))
        assert request[index + 1]["content"] == "current request"
        assert request[:index] == history[:3]
    assert runtime_with_turn_context(history, None) == history


def test_loop_keeps_the_system_prompt_identical_across_rounds_and_never_stores_the_turn_context(monkeypatch):
    import importlib

    loop = importlib.import_module("packages.core.ai.agentic_loop")
    sent: list[list[dict]] = []

    async def complete(messages, tools, **kwargs):
        sent.append(messages)
        if len(sent) == 1:
            return "", [{"id": "call_1", "name": "lookup", "arguments": {}}], {"total": 1}
        return "done", [], {"total": 1}

    async def execute(name, args):
        return "found it"

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    result = asyncio.run(loop.agentic_loop(
        system_prompt="stable system", user_message="find it",
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        tool_executor=execute, max_rounds=3, turn_context="memories picked for this message",
        answer_reminder="REPLY-PREFERENCES",
    ))

    assert sent[0][0]["content"] == sent[1][0]["content"]
    assert sent[0][0]["content"].startswith("stable system")
    for request in sent:
        context = [m for m in request if runtime_is_turn_context_message(m)]
        assert len(context) == 1 and "memories picked for this message" in context[0]["content"]
    # After a tool call the final-answer marker joins the turn context, not the system prompt.
    assert "Final Response Stream Marker" in next(m for m in sent[1] if runtime_is_turn_context_message(m))["content"]
    assert not any(runtime_is_turn_context_message(m) for m in result.messages)
    # The speaker's preferences follow the tool result, so the prefix before it still matches.
    assert not any("REPLY-PREFERENCES" in str(m.get("content")) for m in sent[0])
    assert "REPLY-PREFERENCES" in sent[1][-1]["content"]
    assert not any("REPLY-PREFERENCES" in str(m.get("content")) for m in result.messages)


@pytest.mark.parametrize("model,stays_system", [
    ("openai/gpt-5.6-terra", True),
    ("gpt-5.6-terra", True),
    ("anthropic/claude-opus-5-5", False),
    ("google/gemini-3-pro", False),
])
def test_late_system_context_stays_after_the_history_for_every_provider(model, stays_system):
    from packages.core.ai.llm_client import _portable_late_system_messages

    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "reply"},
        {"role": "system", "content": "<turn-context>\nper turn\n</turn-context>"},
        {"role": "user", "content": "current"},
    ]
    converted = _portable_late_system_messages(messages, model=model)
    assert converted[0] == messages[0]
    late = converted[3]
    if stays_system:
        assert late == messages[3]
    else:
        assert late["role"] == "user" and late["content"].startswith("<system-reminder>")
        assert "per turn" in late["content"]


def test_native_anthropic_keeps_late_system_context_out_of_the_system_prompt():
    from packages.core.ai.llm_client import _anthropic_messages_from_openai

    system, messages = _anthropic_messages_from_openai([
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "reply"},
        {"role": "system", "content": "per turn"},
        {"role": "user", "content": "current"},
    ])
    assert system == "stable"
    assert messages[2]["role"] == "user"
    assert "<system-reminder>" in messages[2]["content"][0]["text"] and "per turn" in messages[2]["content"][0]["text"]
    assert messages[3]["content"][0]["text"] == "current"


@pytest.mark.parametrize("model,expected", [("openai/gpt-5.6-terra", True), ("anthropic/claude-opus-5-5", False)])
def test_prompt_cache_key_goes_only_to_openai_family_models(model, expected):
    from packages.core.ai.llm_client import _apply_prompt_cache_key, llm_prompt_cache_key_scope

    payload: dict = {}
    with llm_prompt_cache_key_scope("manor-abc"):
        _apply_prompt_cache_key(payload, model=model, base_url="https://gateway.example/v1")
    assert ("prompt_cache_key" in payload) is expected
    outside: dict = {}
    _apply_prompt_cache_key(outside, model=model, base_url="https://gateway.example/v1")
    assert "prompt_cache_key" not in outside


def test_a_route_that_rejects_prompt_cache_key_is_retried_without_it_and_remembered(monkeypatch):
    from packages.core.ai import llm_client

    base_url = "https://strict-gateway.example/v1"
    llm_client._PROMPT_CACHE_KEY_REJECTED_BASE_URLS.discard(base_url)
    request = httpx.Request("POST", f"{base_url}/chat/completions")
    rejected = httpx.Response(400, request=request, text='{"error": "Unrecognized request argument: prompt_cache_key"}')
    ok = httpx.Response(200, request=request, json={"choices": []})
    payloads = []

    async def post(url, headers, payload):
        payloads.append(payload)
        if "prompt_cache_key" in payload:
            raise httpx.HTTPStatusError("bad request", request=request, response=rejected)
        return ok

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    response = asyncio.run(llm_client._post_chat_with_reasoning_retry(
        f"{base_url}/chat/completions", {}, {"model": "gpt-5.6-terra", "messages": [], "prompt_cache_key": "manor-abc"},
        call_type="test",
    ))
    assert response is ok
    assert ["prompt_cache_key" in p for p in payloads] == [True, False]
    later: dict = {}
    with llm_client.llm_prompt_cache_key_scope("manor-abc"):
        llm_client._apply_prompt_cache_key(later, model="gpt-5.6-terra", base_url=base_url)
    assert "prompt_cache_key" not in later
    llm_client._PROMPT_CACHE_KEY_REJECTED_BASE_URLS.discard(base_url)


def test_harness_keys_the_prompt_cache_per_conversation_stream():
    from packages.core.ai.runtime.harness import _prompt_cache_key

    key = _prompt_cache_key(entity_id="e", conversation_id="c", user_id="u", agent_id=None, workspace_id=None)
    assert key and key.startswith("manor-") and len(key) == len("manor-") + 32
    assert key == _prompt_cache_key(entity_id="e", conversation_id="c", user_id="u", agent_id=None, workspace_id=None)
    assert key != _prompt_cache_key(entity_id="e", conversation_id="c", user_id="u2", agent_id=None, workspace_id=None)
    assert _prompt_cache_key(entity_id="e", conversation_id=None, user_id="u", agent_id=None, workspace_id=None) is None


TOOLS = [
    {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
    for name in ("search_tools", "web_search", "web_fetch", "invoke_skill", "read_file", "generate_file", "bash")
]


def _capture_assembly(monkeypatch):
    from packages.core.services import runtime_chat_context as module

    calls = []

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=kwargs.get("workspace_id"), tool_profile=None, is_master=True, task_id=None,
            thread_ref_kind=None, thread_ref_id=None, bound_tool_names=None, mcp_allowed_names=set(), extra_context=None,
        )

    async def fake_assemble(_db, *, request, **kwargs):
        calls.append(kwargs)
        visible = kwargs.get("visible_tool_names")
        tools = [t for t in TOOLS if visible is None or t["function"]["name"] in set(visible)]
        ctx = ChatContext(db=_db, entity_id=request.entity_id, user_id=request.user_id,
                          workspace_id=request.workspace_id, conversation_id=request.conversation_id)
        ctx.runtime_turn_context = "Runtime: current_time=2026-10-02 10:00 UTC" if kwargs.get("split_turn_context") else ""
        prompt = "stable system prompt"
        envelope = SimpleNamespace(metadata={}, principal=None)
        return SimpleNamespace(context=ctx, tool_schemas=tools, prompt=prompt, envelope=envelope)

    monkeypatch.setattr("packages.core.services.workspace_runtime.resolve_workspace_runtime", fake_resolve_workspace_runtime)
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble)
    monkeypatch.setattr(module, "runtime_auto_skill_forced_tool_calls", AsyncMock(return_value=[]))
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_model_for_user", AsyncMock(return_value="openai/gpt-5.6-terra"))
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_llm_metadata_for_user", AsyncMock(return_value=None))
    return calls


@pytest.mark.parametrize("split", [True, False])
def test_split_turns_keep_the_full_tool_list_and_move_intent_and_focus_to_the_turn_context(monkeypatch, split):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import runtime_chat_context as module

    calls = _capture_assembly(monkeypatch)
    metadata = {
        "general_intent_prompt": "Classifier: this turn may create a Workspace.",
        "turn_execution_plan": {"tool_catalog_mode": "web_research", "reason": "test"},
    }
    prompt, tools, _history, ctx = asyncio.run(module.resolve_runtime_chat_context(
        None, "搜一下湾区房车营地的月租价格", entity_id="entity-1", user_id="user-1",
        runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT, runtime_metadata=metadata, split_turn_context=split,
    ))
    names = [t["function"]["name"] for t in tools]
    if split:
        assert calls[0]["visible_tool_names"] is None and calls[0]["split_turn_context"] is True
        assert names == [t["function"]["name"] for t in TOOLS]
        assert prompt == "stable system prompt"
        assert ctx.runtime_turn_context.startswith("Classifier: this turn may create a Workspace.")
        assert "## Tools for this request" in ctx.runtime_turn_context
        assert "`web_search`" in ctx.runtime_turn_context and "`bash`" not in ctx.runtime_turn_context
    else:
        assert set(names) == {"search_tools", "web_search", "web_fetch", "invoke_skill"}
        assert prompt.startswith("Classifier: this turn may create a Workspace.")
        assert ctx.runtime_turn_context == ""


def test_a_route_that_rejects_reasoning_effort_is_retried_without_it_and_remembered(monkeypatch):
    from packages.core.ai import llm_client

    base_url = "https://byok-gateway.example/v1"
    route = (base_url, "grok-4.5")
    llm_client._REASONING_EFFORT_REJECTED_ROUTES.discard(route)
    request = httpx.Request("POST", f"{base_url}/chat/completions")
    rejected = httpx.Response(400, request=request, text='{"error": "Upstream error: 400"}')
    ok = httpx.Response(200, request=request, json={"choices": []})
    payloads = []

    async def post(url, headers, payload):
        payloads.append(payload)
        if "reasoning_effort" in payload:
            raise httpx.HTTPStatusError("bad request", request=request, response=rejected)
        return ok

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    response = asyncio.run(llm_client._post_chat_with_reasoning_retry(
        f"{base_url}/chat/completions", {},
        {"model": "grok-4.5", "messages": [], "reasoning_effort": "none", "response_format": {"type": "json_object"}},
        call_type="test",
    ))
    assert response is ok
    assert ["reasoning_effort" in p for p in payloads] == [True, False]
    assert payloads[1]["response_format"] == {"type": "json_object"}
    later = {"model": "grok-4.5", "reasoning_effort": "none"}
    llm_client._apply_reasoning_effort_route(later, base_url=base_url)
    assert "reasoning_effort" not in later
    other_model = {"model": "gpt-5.6-terra", "reasoning_effort": "none"}
    llm_client._apply_reasoning_effort_route(other_model, base_url=base_url)
    assert other_model["reasoning_effort"] == "none"
    llm_client._REASONING_EFFORT_REJECTED_ROUTES.discard(route)


@pytest.mark.parametrize("status,retried", [(400, True), (500, False)])
def test_reasoning_effort_is_kept_when_dropping_it_does_not_help(monkeypatch, status, retried):
    from packages.core.ai import llm_client

    base_url = "https://strict.example/v1"
    request = httpx.Request("POST", f"{base_url}/chat/completions")
    failing = httpx.Response(status, request=request, text='{"error": "invalid messages"}')
    without = httpx.Response(400, request=request, text='{"error": "tools require reasoning_effort none"}')
    payloads = []

    async def post(url, headers, payload):
        payloads.append(payload)
        response = failing if "reasoning_effort" in payload else without
        raise httpx.HTTPStatusError("failed", request=request, response=response)

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(llm_client._post_chat_with_reasoning_retry(
            f"{base_url}/chat/completions", {},
            {"model": "gpt-5.6-terra", "messages": [], "reasoning_effort": "none"}, call_type="test",
        ))
    # The original error is raised and the route keeps sending reasoning_effort.
    assert raised.value.response is failing and "reasoning_effort" in payloads[0]
    assert len(payloads) == (2 if retried else 1)
    assert (base_url, "gpt-5.6-terra") not in llm_client._REASONING_EFFORT_REJECTED_ROUTES
