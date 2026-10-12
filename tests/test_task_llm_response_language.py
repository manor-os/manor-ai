"""Capture TaskRunner's real AIEngine-to-provider messages without DB or LLM I/O."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai import engine as engine_module
from packages.core.ai.engine import AIEngine, ChatMessage, LLMConfig
from packages.core.ai.response_language import RESPONSE_LANGUAGE_BLOCK_START, response_language_guidance
from packages.core.ai.runtime import response_language as language_runtime
from packages.core.ai.runtime import task_llm


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(locale="en", calls=[], fail=False)
    state.resolve = AsyncMock(side_effect=lambda _user_id: state.locale)
    monkeypatch.setattr(language_runtime, "resolve_response_locale", state.resolve)
    monkeypatch.setattr(
        task_llm, "runtime_current_billing_context",
        lambda: SimpleNamespace(entity_id="entity", user_id="requester"),
    )

    async def complete(messages, **kwargs):
        state.calls.append((deepcopy(messages), None, deepcopy(kwargs)))
        if state.fail:
            raise RuntimeError("provider unavailable")
        return '{"status":"done","summary":"Terminé"}', {"total": 3}

    async def complete_with_tools(messages, tools, **kwargs):
        state.calls.append((deepcopy(messages), deepcopy(tools), deepcopy(kwargs)))
        return "Ready", [{"id": "next", "name": "generate_file", "arguments": {"content": "Bonjour"}}], {"total": 5}

    monkeypatch.setattr(engine_module, "chat_completion", complete)
    monkeypatch.setattr(engine_module, "chat_completion_with_tools", complete_with_tools)
    state.engine = AIEngine(LLMConfig(model="primary-model", temperature=0.6, max_tokens=6000))
    return state


def resumed_history():
    return [
        ChatMessage(role="system", content=f"Keep the customer document in French.\n\n{response_language_guidance('zh')}"),
        ChatMessage(role="user", content="先前任务说明"),
        ChatMessage(role="assistant", content="历史中文答复。"),
        ChatMessage(role="system", content=response_language_guidance("de")),
        ChatMessage(role="user", content=[
            {"type": "text", "text": "继续这份文档，正文明确使用法语，周围说明按账户语言。"},
            {"type": "image_url", "image_url": {"url": "https://example.test/diagram.png"}},
        ]),
        ChatMessage(role="assistant", content="", tool_calls=[{
            "id": "write-1", "name": "generate_file",
            "arguments": {"kind": "document", "content": "Bonjour", "name": "draft.md"},
        }], usage={"total": 8}),
        ChatMessage(role="tool", content='{"document_id":"doc-1","content":"Bonjour"}', tool_calls=[{"id": "write-1"}]),
    ]


@pytest.mark.asyncio
async def test_task_agent_real_provider_boundary_preserves_history_tools_and_output_contract(provider):
    history = resumed_history()
    original = deepcopy(history)
    business = 'Return valid JSON with keys "status" and "summary". Preserve existing document text.'
    tools = [{"type": "function", "function": {"name": "generate_file", "parameters": {
        "type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"],
    }}}]
    result = await task_llm.runtime_execute_task_agent_chat(
        engine=provider.engine, messages=history, tools=tools,
        system_prompt=f"{business}\n\n{response_language_guidance('zh')}",
        metadata={"task_id": "task-1"}, temperature=0.25,
    )

    wire, sent_tools, kwargs = provider.calls[-1]
    assert wire[0] == {"role": "system", "content": business}
    last_user = max(index for index, message in enumerate(wire) if message["role"] == "user")
    assert "English (en)" in wire[last_user - 1]["content"]
    assert sum(RESPONSE_LANGUAGE_BLOCK_START in str(message["content"]) for message in wire) == 1
    assert "Keep the customer document in French." in wire[1]["content"]
    assert wire[last_user] == original[4].to_dict()
    assert wire[-2:] == [message.to_dict() for message in original[-2:]]
    assert wire[-1]["tool_call_id"] == wire[-2]["tool_calls"][0]["id"] == "write-1"
    assert sent_tools == tools
    assert kwargs == {"temperature": 0.25, "model": "primary-model", "metadata": {"task_id": "task-1"}}
    assert result.tool_calls[0]["arguments"] == {"content": "Bonjour"}
    assert result.usage == {"total": 5}
    assert history == original
    provider.resolve.assert_awaited_once_with("requester")


@pytest.mark.asyncio
@pytest.mark.parametrize("temperature, max_tokens", [(None, None), (0.15, 777)])
async def test_task_final_provider_payload_preserves_defaults_and_explicit_document_request(provider, temperature, max_tokens):
    provider.locale = "zh"
    messages = [ChatMessage(role="user", content="请生成法语文档，解释用中文。")]
    original = deepcopy(messages)
    result = await task_llm.runtime_execute_task_final_chat(
        engine=provider.engine, messages=messages, system_prompt="Use the existing evidence.",
        metadata={"phase": "final"}, temperature=temperature, max_tokens=max_tokens,
    )
    wire, tools, kwargs = provider.calls[-1]
    assert wire[0] == {"role": "system", "content": "Use the existing evidence."}
    assert "Chinese (zh)" in wire[-2]["content"]
    assert wire[-1] == original[0].to_dict()
    assert tools is None
    assert kwargs == {
        "temperature": 0.3 if temperature is None else temperature,
        "max_tokens": 2048 if max_tokens is None else max_tokens,
        "model": "primary-model", "metadata": {"phase": "final"},
    }
    assert result.content == '{"status":"done","summary":"Terminé"}'
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_task_supervisor_keeps_json_contract_and_restores_model_even_on_failure(provider, fail):
    provider.locale = "fr"
    provider.fail = fail
    operation = task_llm.runtime_execute_task_supervisor_chat(
        engine=provider.engine, prompt="Assess this task", worker_model="worker-model", metadata={"phase": "review"},
    )
    if fail:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await operation
    else:
        await operation
    wire, tools, kwargs = provider.calls[-1]
    assert wire[0] == {"role": "system", "content": task_llm.RUNTIME_TASK_SUPERVISOR_SYSTEM_PROMPT}
    assert "French (fr)" in wire[-2]["content"]
    assert wire[-1] == {"role": "user", "content": "Assess this task"}
    assert tools is None
    assert kwargs == {"temperature": 0.6, "max_tokens": 6000, "model": "worker-model", "metadata": {"phase": "review"}}
    assert provider.engine.config.model == "primary-model"


@pytest.mark.asyncio
async def test_resumed_task_refreshes_language_without_accumulating_owned_blocks(provider):
    messages = resumed_history()
    for locale in ("en", "zh", "en"):
        provider.locale = locale
        original = deepcopy(messages)
        await task_llm.runtime_execute_task_final_chat(
            engine=provider.engine, messages=messages, system_prompt="Task system",
        )
        wire, _, _ = provider.calls[-1]
        tagged = [message for message in wire if RESPONSE_LANGUAGE_BLOCK_START in str(message["content"])]
        assert len(tagged) == 1
        assert f"({locale})" in tagged[0]["content"]
        last_user = max(index for index, message in enumerate(wire) if message["role"] == "user")
        assert wire[last_user - 1] == tagged[0]
        assert wire[-2:] == [message.to_dict() for message in original[-2:]]
        assert messages == original
        # Feed a previously serialized checkpoint back into the next call.
        messages = [ChatMessage(**message) for message in wire[1:]]
    assert provider.resolve.await_count == 3


@pytest.mark.asyncio
async def test_task_external_scope_removes_old_account_blocks_but_keeps_recipient_contract(provider):
    messages = resumed_history()
    original = deepcopy(messages)
    scope = language_runtime.RuntimeResponseLanguage("entity", "requester", external_audience=True)
    with language_runtime.runtime_response_language_scope(scope):
        await task_llm.runtime_execute_task_final_chat(
            engine=provider.engine, messages=messages,
            system_prompt=f"Preserve the recipient's language.\n\n{response_language_guidance('en')}",
        )
    wire, _, _ = provider.calls[-1]
    assert all(RESPONSE_LANGUAGE_BLOCK_START not in str(message["content"]) for message in wire)
    assert wire[0]["content"].startswith("Preserve the recipient's language.")
    assert "Keep the customer document in French." in wire[1]["content"]
    assert wire[-3:] == [message.to_dict() for message in original[-3:]]
    assert messages == original
    provider.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_locale_keeps_the_existing_provider_payload(provider):
    provider.locale = None
    messages = [ChatMessage(role="user", content="Finish the task")]
    await task_llm.runtime_execute_task_final_chat(
        engine=provider.engine, messages=messages, system_prompt="Task system",
    )
    assert provider.calls[-1][0] == [{"role": "system", "content": "Task system"}, messages[0].to_dict()]
