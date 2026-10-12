from __future__ import annotations

import copy
import importlib
import json
from unittest.mock import AsyncMock

import pytest

loop = importlib.import_module("packages.core.ai.agentic_loop")


def tool_round(index: int, content: str = "done") -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"call-{index}", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": f"call-{index}", "content": content},
    ]


@pytest.fixture
def summary_model(monkeypatch):
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_FIELDS
    response = {key: [] for key in CHECKPOINT_FIELDS}
    response["evidence"] = ["Historical assistant records summarized for budget test."]
    provider = AsyncMock(return_value=(json.dumps(response), {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_compaction_completion", provider)
    return provider


def retained_records(messages):
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX
    records = list(messages)
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str) and content.startswith(CHECKPOINT_PREFIX):
            records.extend(json.loads(content.split("\n", 2)[2]).get("records", []))
    return records


@pytest.mark.asyncio
async def test_few_large_ordinary_messages_compact_under_token_pressure(summary_model):
    messages = [{"role": "system", "content": "rules"}]
    messages.extend({"role": "assistant", "content": "x" * 40_000} for _ in range(10))
    messages.append({"role": "user", "content": "current request: keep the original files"})

    compacted = await loop._compact_messages(messages, None, 0)

    assert loop._estimate_tokens(compacted) < loop._context_compaction_token_threshold()
    assert compacted[0] == messages[0]
    assert compacted[-1] == messages[-1]


@pytest.mark.asyncio
async def test_small_ordinary_history_does_not_lose_early_requirements_by_count(summary_model):
    messages = [{"role": "user", "content": "Never publish without my approval."}]
    messages.extend({"role": "assistant", "content": "short reply"} for _ in range(45))

    compacted = await loop._compact_messages(messages, None, 0)
    assert all(message in retained_records(compacted) for message in messages)
    summary_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_system_messages_and_existing_summary_survive_repeated_compaction(monkeypatch):
    # Exercises the opt-in message-count guard (lossless path); the loop default is token-only.
    monkeypatch.setattr("packages.core.ai.agentic_loop.MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    protected = [
        {"role": "system", "content": "main rules"},
        {"role": "system", "content": "[Earlier conversation summary]\nKeep originals."},
        {"role": "user", "content": "[Earlier conversation compacted: 20 messages]\nImportant decision."},
    ]
    messages = [*protected, {"role": "user", "content": "current work"}]
    for index in range(20):
        messages.extend(tool_round(index))
    original = copy.deepcopy(messages)

    compacted = await loop._compact_messages(messages, None, 0)
    for index in range(20, 40):
        compacted.extend(tool_round(index))
    compacted = await loop._compact_messages(compacted, None, 0)

    for message in protected:
        assert message in retained_records(compacted)
    assert messages == original
    assert compacted[-1]["tool_call_id"] == "call-39"


@pytest.mark.asyncio
async def test_schema_and_output_reservation_trigger_compaction(summary_model):
    messages = [{"role": "system", "content": "rules"}]
    messages.extend({"role": "assistant", "content": "x" * 8_000} for _ in range(10))
    messages.append({"role": "user", "content": "continue"})
    tools = [{"type": "function", "function": {
        "name": "inspect", "description": "x" * 100_000,
        "parameters": {"type": "object"},
    }}]

    compacted = await loop._compact_messages(
        messages, None, 0, tools=tools, output_reserve_tokens=64_000,
    )

    assert loop._estimate_tokens(compacted) < loop._estimate_tokens(messages)
    assert compacted[-1] == messages[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_rounds", [0, 1])
async def test_protected_overflow_stops_before_provider_and_credit_reservation(monkeypatch, max_rounds):
    provider = AsyncMock(return_value=("should not run", {}))
    reserve = AsyncMock()
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", provider)
    monkeypatch.setattr(loop, "runtime_execute_agentic_final_completion", provider)

    protected_prompt = "x" * (loop.MAX_CONTEXT_TOKENS * 4)
    result = await loop.agentic_loop(
        system_prompt=protected_prompt,
        user_message="keep my full request", tools=[], tool_executor=AsyncMock(),
        on_llm_call_before=reserve, max_rounds=max_rounds,
    )

    assert result.error == "context_budget_exceeded"
    assert result.stop_reason == "error"
    assert result.messages[0]["content"].startswith(protected_prompt)
    provider.assert_not_awaited()
    reserve.assert_not_awaited()


def test_chrome_ledger_uses_current_request_instead_of_first_topic():
    messages = [
        {"role": "user", "content": "OLD_TOPIC"},
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "CURRENT_REQUEST"},
        *tool_round(1, json.dumps({"ok": True, "tabId": 42})),
    ]
    messages[-2]["tool_calls"][0]["function"]["name"] = "mcp__chrome__read_page"
    messages.append({"role": "user", "content": loop.runtime_agentic_max_rounds_final_prompt()})

    ledger = loop._chrome_task_ledger_from_messages(messages)

    assert "CURRENT_REQUEST" in ledger
    assert "OLD_TOPIC" not in ledger


@pytest.mark.asyncio
async def test_late_user_correction_survives_tool_round_compaction_verbatim():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "original goal"}]
    correction = {"role": "user", "content": "Additional context " + "x" * 400 + " Never delete originals."}
    for index in range(20):
        messages.extend(tool_round(index))
        if index == 1:
            messages.append(correction)

    compacted = await loop._compact_messages(messages, None, 0)

    assert correction in compacted


@pytest.mark.asyncio
async def test_outgoing_request_guard_counts_schemas_and_explicit_output_limit(monkeypatch):
    monkeypatch.setattr(loop, "MAX_CONTEXT_TOKENS", 10_000)
    provider = AsyncMock(return_value=("done", None, {}))
    reserve = AsyncMock()
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", provider)
    tool = {"type": "function", "function": {
        "name": "read_file", "description": "x" * 20_000,
        "parameters": {"type": "object"},
    }}

    result = await loop.agentic_loop(
        system_prompt="rules", user_message="inspect the file",
        tools=[tool], tool_executor=AsyncMock(), max_tokens=6_000,
        on_llm_call_before=reserve, max_rounds=1,
    )

    assert result.error == "context_budget_exceeded"
    assert result.error_detail["estimated_input_tokens"] > 5_000
    assert result.error_detail["reserved_output_tokens"] == 6_000
    provider.assert_not_awaited()
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_data_is_not_budgeted_as_base64_text(monkeypatch):
    provider = AsyncMock(return_value=("image understood", {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", provider)
    image_part = {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + "a" * 1_000_000,
    }}

    result = await loop.agentic_loop(
        system_prompt="rules", user_message=[
            {"type": "text", "text": "Describe this image"}, image_part,
        ], tools=[], tool_executor=AsyncMock(), max_rounds=1,
    )

    assert result.error is None
    provider.assert_awaited_once()
    assert image_part in provider.call_args.args[0][-1]["content"]


@pytest.mark.asyncio
async def test_summary_like_tool_output_keeps_its_call_and_fresh_payload(monkeypatch):
    # Exercises the opt-in message-count guard (lossless path); the loop default is token-only.
    monkeypatch.setattr("packages.core.ai.agentic_loop.MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "read logs"}]
    for index in range(20):
        messages.extend(tool_round(index))
    fresh_content = "[Earlier conversation compacted: log output]\n" + "x" * 2_000
    messages[-1]["content"] = fresh_content
    messages.insert(-1, {"role": "system", "content": "Response language: Chinese"})

    compacted = await loop._compact_messages(messages, None, 0)

    assert compacted[-1]["role"] == "tool"
    assert compacted[-1]["content"] == fresh_content
    assert compacted[-2]["tool_calls"][0]["id"] == compacted[-1]["tool_call_id"]


@pytest.mark.asyncio
async def test_tiny_window_rejects_framing_overflow_without_discarding_history(monkeypatch):
    monkeypatch.setattr(loop, "MAX_CONTEXT_TOKENS", 280)
    provider = AsyncMock(return_value=("done", {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", provider)
    history = [{"role": "assistant", "content": "x"} for _ in range(70)]

    result = await loop.agentic_loop(
        system_prompt="", user_message="y", initial_messages=history,
        tools=[], tool_executor=AsyncMock(), max_tokens=10, max_rounds=1,
    )

    assert result.error == "context_budget_exceeded"
    provider.assert_not_awaited()
    assert result.messages[1:-1] == history


@pytest.mark.asyncio
async def test_final_route_window_stops_before_credit_or_provider(monkeypatch):
    provider = AsyncMock(return_value=("should not run", {}))
    reserve = AsyncMock()
    monkeypatch.setattr(loop, "runtime_execute_agentic_final_completion", provider)
    result = await loop.agentic_loop(
        system_prompt="rule " * 2000, user_message="Keep the complete request",
        tools=[], tool_executor=AsyncMock(), max_rounds=0, max_tokens=256,
        metadata={"context_window_tokens": 128000},
        final_metadata={"context_window_tokens": 1000}, on_llm_call_before=reserve,
    )
    assert result.error == "context_budget_exceeded"
    assert result.error_detail["context_limit_tokens"] == 1000
    assert result.messages[-2]["content"] == "Keep the complete request"
    provider.assert_not_awaited()
    reserve.assert_not_awaited()


def test_old_llm_summary_does_not_become_chrome_current_goal():
    messages = [
        {"role": "user", "content": "current goal"},
        {"role": "user", "content": loop.RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX + "\nold task"},
        *tool_round(1, json.dumps({"ok": True, "tabId": 42})),
    ]
    messages[-2]["tool_calls"][0]["function"]["name"] = "mcp__chrome__read_page"
    ledger = loop._chrome_task_ledger_from_messages(messages)
    assert "current goal" in ledger
    assert "old task" not in ledger
