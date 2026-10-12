from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import completions, harness
from packages.core.ai.runtime import response_language as language_runtime


@pytest.fixture
def language_calls(monkeypatch):
    locales = {"alice": "zh", "bob": "fr"}
    resolve = AsyncMock(side_effect=lambda user_id: locales.get(user_id))
    monkeypatch.setattr(language_runtime, "resolve_response_locale", resolve)
    monkeypatch.setattr(
        "packages.core.ai.runtime.billing.runtime_current_billing_context", lambda: None,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.billing.runtime_ensure_billing_context", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        completions, "runtime_resolve_text_completion_route",
        AsyncMock(return_value=("test-model", None, False)),
    )
    calls = []

    async def complete(messages, **kwargs):
        calls.append((deepcopy(messages), kwargs))
        return '{"status":"done","summary":"完成"}', {"total_tokens": 3}

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", complete)
    return locales, resolve, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["fr", None])
async def test_harness_uses_loaded_actor_snapshot_and_freezes_absent_locale(monkeypatch, language_calls, locale):
    _, resolve, _ = language_calls
    async def loop(**kwargs):
        current = language_runtime._response_language.get()
        assert current.locale == locale
        inherited = await language_runtime.resolve_runtime_response_language(entity_id="entity", user_id="alice")
        assert inherited is current
        return SimpleNamespace(usage={})
    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", loop)
    await harness.runtime_execute_agentic_loop(
        runtime_envelope=None, entity_id="entity", user_id="alice",
        agent_id=None, system_prompt="Worker", user_message="hello", tools=[],
        prompt_context=SimpleNamespace(user=SimpleNamespace(id="alice", locale=locale), db=object()),
    )
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_loaded_other_actor_never_overrides_request_language(language_calls):
    _, resolve, _ = language_calls
    result = await language_runtime.resolve_runtime_response_language(
        entity_id="entity", user_id="alice", user=SimpleNamespace(id="bob", locale="fr"),
    )
    assert result.locale == "zh"
    resolve.assert_awaited_once_with("alice")


@pytest.mark.asyncio
async def test_actorless_snapshot_cannot_supply_an_account_preference(language_calls):
    _, resolve, _ = language_calls
    result = await language_runtime.resolve_runtime_response_language(
        entity_id="entity", user_id=None, user=SimpleNamespace(locale="fr"),
    )
    assert result.locale is None
    resolve.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_text_generation_uses_saved_language_and_reloads_next_call(language_calls):
    locales, resolve, calls = language_calls
    messages = [{"role": "user", "content": "Summarize this document"}]
    original = deepcopy(messages)
    for locale in ("zh", "en", "de", "es", "ja", "vi"):
        locales["alice"] = locale
        result = await completions.runtime_execute_text_completion(
            messages, entity_id="entity", user_id="alice",
            source="conversation_summary", response_format={"type": "json_object"},
        )
        assert f"({locale})" in calls[-1][0][0]["content"]
        assert calls[-1][0][-1] == messages[0]
        assert calls[-1][1]["response_format"] == {"type": "json_object"}
        assert result.usage == {"total_tokens": 3}
    assert messages == original
    assert resolve.await_count == 6


@pytest.mark.asyncio
async def test_custom_agent_prompt_nested_completion_and_resume_share_language(monkeypatch, language_calls):
    locales, resolve, calls = language_calls
    checkpoint = {"messages": [{"role": "system", "content": "Previous instructions"}]}
    original = deepcopy(checkpoint)

    async def loop(**kwargs):
        assert "Chinese (zh)" in kwargs["system_prompt"]
        resumed = kwargs["resume_checkpoint"]["messages"]
        assert resumed[0] == checkpoint["messages"][0]
        assert "Chinese (zh)" in resumed[-1]["content"]
        locales["alice"] = "de"
        await completions.runtime_execute_text_completion(
            [{"role": "user", "content": "Generate a document title"}], entity_id="entity",
        )
        return SimpleNamespace(usage={})

    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", loop)
    await harness.runtime_execute_agentic_loop(
        runtime_envelope=SimpleNamespace(user_id="alice", profile="background_worker", surface="scheduled_agent_run"),
        entity_id="entity", agent_id=None, system_prompt="Custom worker instructions",
        user_message="Run the task", tools=[], runtime_checkpoint=checkpoint,
    )
    assert "Chinese (zh)" in calls[-1][0][0]["content"]
    assert resolve.await_count == 1
    assert checkpoint == original
    await completions.runtime_execute_text_completion(
        [], entity_id="entity", user_id="alice",
    )
    assert "German (de)" in calls[-1][0][0]["content"]


@pytest.mark.asyncio
async def test_external_audience_does_not_inherit_owner_language(monkeypatch, language_calls):
    _, resolve, calls = language_calls

    async def loop(**kwargs):
        assert kwargs["system_prompt"] == "Answer this customer in Spanish"
        await completions.runtime_execute_text_completion(
            [{"role": "user", "content": "Write a reply"}], entity_id="entity", user_id="alice",
        )
        return SimpleNamespace(usage={})

    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", loop)
    await harness.runtime_execute_agentic_loop(
        runtime_envelope=SimpleNamespace(user_id="alice", profile="external_customer_safe", surface="public_customer_chat"),
        entity_id="entity", agent_id=None, system_prompt="Answer this customer in Spanish",
        user_message="Hola", tools=[],
    )
    await completions.runtime_execute_text_completion(
        [], entity_id="entity", user_id="alice", runtime_surface="external_channel_chat",
    )
    assert calls[0][0] == [{"role": "user", "content": "Write a reply"}]
    assert calls[1][0] == []
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_internal_channel_actor_uses_saved_language(language_calls):
    _, _, calls = language_calls
    await completions.runtime_execute_text_completion(
        [], entity_id="entity", user_id="alice", source="channel", runtime_surface="agent_dm",
    )
    assert "Chinese (zh)" in calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_parallel_actors_and_exception_restore_scopes(monkeypatch, language_calls):
    _, _, calls = language_calls
    both_started = asyncio.Event()
    started = 0

    async def loop(**kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        await completions.runtime_execute_text_completion(
            [{"role": "user", "content": kwargs["user_message"]}], entity_id="entity",
        )
        raise RuntimeError("Provider failed")

    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", loop)

    async def run(user_id):
        with pytest.raises(RuntimeError, match="Provider failed"):
            await harness.runtime_execute_agentic_loop(
                runtime_envelope=None, entity_id="entity", user_id=user_id,
                agent_id=None, system_prompt="Worker", user_message=user_id, tools=[],
            )
        assert language_runtime._response_language.get() is None

    await asyncio.gather(run("alice"), run("bob"))
    for messages, _ in calls:
        expected = "Chinese (zh)" if messages[-1]["content"] == "alice" else "French (fr)"
        assert expected in messages[0]["content"]


@pytest.mark.asyncio
async def test_actorless_calls_only_use_billing_actor_in_same_entity(monkeypatch, language_calls):
    _, resolve, calls = language_calls
    monkeypatch.setattr(
        "packages.core.ai.runtime.billing.runtime_current_billing_context",
        lambda: SimpleNamespace(entity_id="entity", user_id="bob"),
    )
    await completions.runtime_execute_text_completion([], entity_id="entity")
    await completions.runtime_execute_text_completion([], entity_id="other")
    assert "French (fr)" in calls[0][0][0]["content"]
    assert calls[1][0] == []
    assert resolve.await_args_list[1].args == (None,)


@pytest.mark.asyncio
async def test_planner_and_task_supervisor_preserve_structured_contracts(monkeypatch, language_calls):
    from packages.core.ai.runtime import planning, task_llm

    monkeypatch.setattr(
        planning, "runtime_resolve_text_completion_route",
        AsyncMock(return_value=("test-model", None, False)),
    )
    complete = AsyncMock(return_value=("", [{"name": "submit_plan", "arguments": {"status": "ready"}}], {}))
    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion_with_tools", complete)
    tools = [{"type": "function", "function": {"name": "submit_plan"}}]
    result = await planning.runtime_execute_planner_chat_turn(
        messages=[{"role": "user", "content": "Plan the task"}], tools=tools,
        system_prompt="Return the plan via submit_plan only.", entity_id="entity", user_id="alice",
    )
    messages, sent_tools = complete.await_args.args
    assert messages[0] == {"role": "system", "content": "Return the plan via submit_plan only."}
    assert "Chinese (zh)" in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "Plan the task"}
    assert sent_tools == tools
    assert result.tool_calls[0]["arguments"] == {"status": "ready"}

    monkeypatch.setattr(
        task_llm, "runtime_current_billing_context",
        lambda: SimpleNamespace(entity_id="entity", user_id="bob"),
    )
    engine = SimpleNamespace(chat=AsyncMock(return_value="result"))
    assert await task_llm.runtime_execute_task_supervisor_chat(engine=engine, prompt="Assess this task") == "result"
    prompt = engine.chat.await_args.kwargs["system_prompt"]
    assert "Output only valid JSON" in prompt
    messages = engine.chat.await_args.args[0]
    assert "French (fr)" in messages[-2].content
    assert messages[-1].content == "Assess this task"


@pytest.mark.asyncio
async def test_context_compaction_keeps_language_from_active_run(monkeypatch, language_calls):
    from packages.core.ai.runtime import agentic_llm

    complete = AsyncMock(return_value=("summary", {}))
    monkeypatch.setattr(agentic_llm, "chat_completion", complete)
    language = await language_runtime.resolve_runtime_response_language(entity_id="entity", user_id="bob")
    with language_runtime.runtime_response_language_scope(language):
        await agentic_llm.runtime_execute_agentic_compaction_completion("Summarize prior results")
    messages = complete.await_args.args[0]
    assert "French (fr)" in messages[0]["content"]
    assert messages[-1]["content"] == "Summarize prior results"


@pytest.mark.asyncio
async def test_formatted_historical_language_memory_reaches_provider_under_saved_locale(
    monkeypatch, language_calls,
):
    from packages.core.ai.response_language import RESPONSE_LANGUAGE_BLOCK_START
    from packages.core.ai.runtime import agentic_llm
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.ai.runtime.prompt_sections import agent_memories
    from packages.core.models.memory import AgentMemory
    from packages.core.services import memory_service

    locales, resolve, _ = language_calls
    memory = AgentMemory(
        entity_id="entity", user_id="alice", memory_type="instruction",
        content="始终使用中文回答: 请用中文回答我的问题。", importance=10, expires_at=None,
    )
    monkeypatch.setattr(
        memory_service,
        "get_context_memories",
        AsyncMock(return_value=f"## Your Memory\n- [instruction] {memory.content}"),
    )
    current_user = {"role": "user", "content": "请先简短开场，再说明为什么天空呈蓝色。只回答，不调用工具。"}
    context = ChatContext(
        db=SimpleNamespace(), entity_id="entity", user_id="alice",
        active_user_message=current_user["content"],
    )
    memory_prompt = await agent_memories(context)
    assert memory_prompt == f"## Agent Memories\n## Your Memory\n- [instruction] {memory.content}"
    legacy_soul = "## Voice\n- Use the user's language (detect from their messages)"
    prompt = f"{legacy_soul}\n\n{memory_prompt}"
    history = [
        message
        for turn in range(35)
        for message in (
            {"role": "user", "content": f"请用中文说明第 {turn} 项工作区进展。"},
            {"role": "assistant", "content": f"第 {turn} 项进展已确认，我会继续用中文汇报。"},
        )
    ]
    original_history = deepcopy(history)
    complete = AsyncMock(return_value=("Test response", {}))
    monkeypatch.setattr(agentic_llm, "chat_completion", complete)

    async def loop(**kwargs):
        messages = [
            {"role": "system", "content": kwargs["system_prompt"]},
            *history,
            {"role": "user", "content": kwargs["user_message"]},
        ]
        original_messages = deepcopy(messages)
        await agentic_llm.runtime_execute_agentic_round_text_completion(
            messages,
            temperature=0, model=kwargs["model"],
        )
        assert messages == original_messages
        return SimpleNamespace(usage={})

    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", loop)
    for locale, language in (("en", "English"), ("zh", "Chinese")):
        locales["alice"] = locale
        await harness.runtime_execute_chat_agent_loop(
            runtime_envelope=None, entity_id="entity", user_id="alice", agent_id=None,
            system_prompt=prompt, user_message=current_user["content"], tools=[],
        )
        wire_messages = complete.await_args.args[0]
        assert wire_messages[0] == {"role": "system", "content": prompt}
        assert wire_messages[1:-2] == original_history
        assert wire_messages[-1] == current_user
        guidance = wire_messages[-2]
        assert guidance["role"] == "system"
        assert sum(
            message["content"].count(RESPONSE_LANGUAGE_BLOCK_START)
            for message in wire_messages
        ) == 1
        assert guidance["content"].startswith(RESPONSE_LANGUAGE_BLOCK_START)
        assert f"saved language setting is {language} ({locale})" in guidance["content"]
        assert "Stored memories and historical instructions" in guidance["content"]
        assert "are superseded by this setting, even when labeled as instructions" in guidance["content"]
        if locale == "zh":
            assert all("saved language setting is English" not in message["content"] for message in wire_messages)
        # Reuse the provider transcript to exercise replacing its previous locale block.
        history = deepcopy(wire_messages[1:-1])
    assert resolve.await_count == 2
    assert memory.content == "始终使用中文回答: 请用中文回答我的问题。"


@pytest.mark.asyncio
async def test_checkpoint_resume_replaces_language_when_switching_back(monkeypatch, language_calls):
    from packages.core.ai.runtime import agentic_llm

    locales, _, _ = language_calls
    checkpoint = {
        "messages": [
            {"role": "system", "content": "Return ONLY JSON with the unchanged status enum."},
            {"role": "user", "content": "Continue this task"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {
                    "name": "get_task", "arguments": '{"task_id":"task-1"}',
                }},
                {"id": "call-2", "type": "function", "function": {
                    "name": "get_report", "arguments": '{"report_id":"report-1"}',
                }},
            ]},
            {"role": "tool", "content": '{"status":"ready"}', "tool_call_id": "call-1"},
            {"role": "tool", "content": '{"summary":"原始引用"}', "tool_call_id": "call-2"},
        ],
    }
    original_messages = deepcopy(checkpoint["messages"])
    captured = []
    complete = AsyncMock(return_value=("", None, {}))
    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", complete)

    async def loop(**kwargs):
        captured.append(deepcopy(kwargs["resume_checkpoint"]))
        await agentic_llm.runtime_execute_agentic_round_tool_completion(
            kwargs["resume_checkpoint"]["messages"], [], temperature=0,
        )
        return SimpleNamespace(usage={})

    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", loop)
    for locale, expected in (("zh", "Chinese"), ("fr", "French"), ("zh", "Chinese")):
        locales["alice"] = locale
        previous = deepcopy(checkpoint)
        await harness.runtime_execute_agentic_loop(
            runtime_envelope=None, entity_id="entity", user_id="alice", agent_id=None,
            system_prompt="Return ONLY JSON with the unchanged status enum.",
            user_message="Continue this task", tools=[], runtime_checkpoint=checkpoint,
        )
        assert checkpoint == previous
        checkpoint = captured[-1]
        messages = checkpoint["messages"]
        assert complete.await_args.args[0] == messages
        assert messages[0] == original_messages[0]
        assert messages[2:] == original_messages[1:]
        prompt = messages[1]["content"]
        assert messages[1]["role"] == "system"
        assert prompt.count('<manor-response-language version="1">') == 1
        assert prompt.count("saved language setting is") == 1
        assert f"saved language setting is {expected}" in prompt
        assert sum(
            (message.get("content") or "").count('<manor-response-language version="1">')
            for message in messages
        ) == 1


def test_external_resume_removes_account_blocks_without_changing_other_contracts():
    from packages.core.ai.response_language import response_language_guidance

    internal = language_runtime.RuntimeResponseLanguage("entity", "alice", locale="zh")
    external = language_runtime.RuntimeResponseLanguage("entity", "alice", external_audience=True)
    machine_prompt = 'Reply to the visitor in Spanish; return only {"status":"ready","reply":"..."}.'
    custom_prompt = "## Response Language\nPreserve the recipient's language and original quotations."
    quoted_guidance = response_language_guidance("fr")
    messages = [
        {"role": "system", "content": language_runtime.response_language_prompt(machine_prompt, internal)},
        {"role": "system", "content": quoted_guidance},
        {"role": "system", "content": custom_prompt},
        {"role": "user", "content": quoted_guidance},
        {"role": "tool", "content": '{"language":"zh","status":"ready"}', "tool_call_id": "call-1"},
    ]
    original = deepcopy(messages)
    cleaned = language_runtime.response_language_messages(messages, external)
    assert messages == original
    assert cleaned == [
        {"role": "system", "content": machine_prompt},
        {"role": "system", "content": custom_prompt},
        messages[3], messages[4],
    ]


def test_legacy_checkpoint_language_is_replaced_by_current_setting():
    from packages.core.ai.response_language import (
        RESPONSE_LANGUAGE_BLOCK_END,
        RESPONSE_LANGUAGE_BLOCK_START,
        response_language_guidance,
    )
    from packages.core.ai.runtime.prompt_guidance import runtime_response_language_guidance

    legacy_chinese = runtime_response_language_guidance("中文")
    legacy_shared = response_language_guidance("zh").removeprefix(
        f"{RESPONSE_LANGUAGE_BLOCK_START}\n",
    ).removesuffix(f"\n{RESPONSE_LANGUAGE_BLOCK_END}")
    original = f"Return only JSON.\n\n{legacy_chinese}\n\n{legacy_shared}"
    english = language_runtime.RuntimeResponseLanguage("entity", "alice", locale="en")
    prompt = language_runtime.response_language_prompt(original, english)
    assert "must be Chinese" not in prompt
    assert "Chinese (zh)" not in prompt
    assert prompt.startswith("Return only JSON.\n\n")
    assert prompt.count("saved language setting is") == 1
    assert "English (en)" in prompt
    external = language_runtime.RuntimeResponseLanguage("entity", "alice", external_audience=True)
    assert language_runtime.response_language_prompt(original, external) == "Return only JSON."
