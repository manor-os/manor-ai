from __future__ import annotations

import json
import importlib

import pytest

from packages.core.ai.agentic_loop import (
    _add_usage,
    _compact_messages,
    _compact_search_tools_result_for_context,
    _compact_tool_result_for_context,
    _context_compaction_token_threshold,
    _estimate_context_attribution,
    LOOP_COMPACT_RATIO,
    MAX_CONTEXT_TOKENS,
)
from packages.core.ai.llm_client import (
    _anthropic_messages_completion,
    _apply_prompt_cache,
    _openai_content_to_anthropic_blocks,
)
from packages.core.ai.runtime.legacy_tool_surface import MASTER_ALWAYS_LOADED
from packages.core.ai.runtime import RUNTIME_AGENTIC_MAX_TOKENS
from packages.core.ai.tool_pool import ToolPool
from packages.core.services.usage_service import log_token_usage


def test_add_usage_preserves_cache_split_and_model():
    total = {"prompt": 0, "completion": 0, "total": 0, "model": None, "cost_usd": None}

    _add_usage(
        total,
        {
            "prompt": 100,
            "completion": 20,
            "total": 120,
            "cache_read": 80,
            "cache_creation": 10,
            "model": "anthropic/claude-sonnet-4.6",
            "provider": "anthropic",
            "cost_usd": 0.01,
        },
    )
    _add_usage(
        total,
        {
            "prompt": 50,
            "completion": 10,
            "total": 60,
            "cache_read": 40,
            "model": "anthropic/claude-sonnet-4.6",
            "cost_usd": 0.02,
        },
    )

    assert total["prompt"] == 150
    assert total["completion"] == 30
    assert total["total"] == 180
    assert total["cache_read"] == 120
    assert total["cache_creation"] == 10
    assert total["model"] == "anthropic/claude-sonnet-4.6"
    assert total["provider"] == "anthropic"
    assert round(total["cost_usd"], 6) == 0.03


def test_add_usage_preserves_byok_billing_source():
    total = {"prompt": 0, "completion": 0, "total": 0, "model": None, "cost_usd": None}

    _add_usage(
        total,
        {
            "prompt": 100,
            "completion": 20,
            "total": 120,
            "model": "gpt-5.5",
            "byok": True,
            "llm_billing_mode": "byok",
        },
    )
    _add_usage(
        total,
        {
            "prompt": 50,
            "completion": 10,
            "total": 60,
            "model": "gpt-5.5",
            "api_key_source": "byok",
        },
    )

    assert total["total"] == 180
    assert total["byok"] is True
    assert total["billing_mode"] == "byok"
    assert total["llm_billing_mode"] == "byok"
    assert total["llm_api_key_source"] == "byok"


def test_add_usage_aggregates_context_attribution():
    total = {"prompt": 0, "completion": 0, "total": 0}

    _add_usage(
        total,
        {
            "prompt": 100,
            "completion": 10,
            "total": 110,
            "context_attribution": {
                "system_tokens": 10,
                "tool_schema_tokens": 20,
                "message_count": 2,
                "tool_count": 1,
                "total_estimated_tokens": 30,
            },
        },
    )
    _add_usage(
        total,
        {
            "prompt": 50,
            "completion": 5,
            "total": 55,
            "context_attribution": {
                "system_tokens": 5,
                "tool_schema_tokens": 10,
                "message_count": 4,
                "tool_count": 2,
                "total_estimated_tokens": 15,
            },
        },
    )

    assert total["context_attribution_total"]["system_tokens"] == 15
    assert total["context_attribution_total"]["tool_schema_tokens"] == 30
    assert total["context_attribution_total"]["rounds"] == 2
    assert total["context_attribution_total"]["last_message_count"] == 4
    assert total["context_attribution_total"]["last_tool_count"] == 2


def test_estimate_context_attribution_buckets_prompt_sources():
    attribution = _estimate_context_attribution(
        [
            {"role": "system", "content": "stable system prompt"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file"}}]},
            {"role": "tool", "content": "large tool result"},
            {"role": "user", "content": "[Image: cat.png → /api/v1/fs/e/uploads/chat/cat.png]"},
            {"role": "user", "content": "fresh user request"},
        ],
        [{"type": "function", "function": {"name": "search_tools", "parameters": {"type": "object"}}}],
    )

    assert attribution["system_tokens"] > 0
    assert attribution["history_tokens"] > 0
    assert attribution["tool_call_tokens"] > 0
    assert attribution["tool_result_tokens"] > 0
    assert attribution["file_tokens"] > 0
    assert attribution["user_input_tokens"] > 0
    assert attribution["tool_schema_tokens"] > 0


def test_compact_search_tools_result_removes_schemas_from_context():
    search_result = {
        "query": "video",
        "matches": [
            {
                "name": "generate_file",
                "schema": {
                    "type": "function",
                    "function": {
                        "name": "generate_file",
                        "description": "x" * 4000,
                        "parameters": {"type": "object"},
                    },
                },
            }
        ],
    }

    compact = json.loads(_compact_search_tools_result_for_context(search_result, ["generate_file"]))

    assert compact["matched_tools"] == ["generate_file"]
    assert compact["loaded_tools"] == ["generate_file"]
    assert "schema" not in json.dumps(compact)
    assert len(json.dumps(compact)) < 200


def test_compact_search_tools_result_preserves_mcp_option_status():
    search_result = {
        "query": "linkedin",
        "matches": [
            {"name": "mcp__linkedin__get_profile", "available": False},
            {"name": "mcp__linkedin_browser__search_people", "available": True},
        ],
        "mcp_options": [
            {
                "server_key": "linkedin",
                "name": "LinkedIn (Posting & Analytics)",
                "ready": False,
                "authorization_method": "oauth",
                "execution_mode": "official_api",
                "reason": "Connect LinkedIn",
                "matched_tools": ["mcp__linkedin__get_profile"],
            },
            {
                "server_key": "linkedin_browser",
                "name": "LinkedIn (Search & Messaging)",
                "ready": True,
                "authorization_method": "browser_session",
                "execution_mode": "browser_automation",
                "reason": "Using entity-level linkedin_browser credentials.",
                "matched_tools": ["mcp__linkedin_browser__search_people"],
            },
        ],
    }

    compact = json.loads(
        _compact_search_tools_result_for_context(
            search_result,
            ["mcp__linkedin_browser__search_people"],
        )
    )

    options = {item["server_key"]: item for item in compact["mcp_options"]}
    assert options["linkedin"]["ready"] is False
    assert options["linkedin"]["authorization_method"] == "oauth"
    assert options["linkedin_browser"]["ready"] is True
    assert options["linkedin_browser"]["execution_mode"] == "browser_automation"


def test_compact_tool_result_preserves_json_metadata():
    raw = json.dumps(
        {
            "path": "big.md",
            "content": "A" * 10_000,
            "next_offset": 120,
            "hint": "Call read_file with offset=next_offset.",
        },
        ensure_ascii=False,
    )

    compact = _compact_tool_result_for_context("read_file", raw, max_chars=1400)
    data = json.loads(compact)

    assert len(compact) < 1400
    assert data["path"] == "big.md"
    assert data["next_offset"] == 120
    assert data["hint"] == "Call read_file with offset=next_offset."
    assert len(data["content"]) < 1200
    assert data["_tool_result_truncated"]["tool"] == "read_file"
    assert data["_tool_result_truncated"]["original_chars"] == len(raw)


def test_compact_tool_result_hides_internal_truncation_marker_for_paginated_document_lists():
    raw = json.dumps(
        {
            "total": 100,
            "count": 50,
            "limit": 50,
            "offset": 0,
            "next_offset": 50,
            "has_more": True,
            "documents": [
                {
                    "id": f"doc_{index}",
                    "name": f"document-{index}.md",
                    "file_type": "md",
                    "file_size": index,
                    "description": "A" * 300,
                }
                for index in range(80)
            ],
        },
        ensure_ascii=False,
    )

    compact = _compact_tool_result_for_context("manor", raw, max_chars=1400)
    data = json.loads(compact)

    assert len(compact) <= 1400
    assert data["total"] == 100
    assert data["count"] == 50
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert data["next_offset"] == 50
    assert data["has_more"] is True
    assert "_tool_result_truncated" not in data
    assert "truncated" not in compact.lower()
    assert "not_shown" not in compact
    assert "omitted" not in compact.lower()
    assert len(data["documents"]) < 80


def test_compact_paginated_document_lists_preserves_full_returned_page_names():
    raw = json.dumps(
        {
            "total": 100,
            "count": 50,
            "limit": 50,
            "offset": 0,
            "next_offset": 50,
            "has_more": True,
            "documents": [
                {
                    "id": f"01KT{index:04d}",
                    "name": f"document-{index}.md",
                    "file_type": "md",
                    "file_size": index * 100,
                    "description": "large metadata " * 80,
                }
                for index in range(50)
            ],
        },
        ensure_ascii=False,
    )

    compact = _compact_tool_result_for_context("manor", raw, max_chars=4000)
    data = json.loads(compact)

    assert data["count"] == 50
    assert data["limit"] == 50
    assert len(data["documents"]) == 50
    assert data["documents"][0] == {
        "name": "document-0.md",
        "file_type": "md",
        "file_size": 0,
    }
    assert data["documents"][-1]["name"] == "document-49.md"
    assert "description" not in data["documents"][0]
    assert "id" not in data["documents"][0]
    assert "_tool_result_truncated" not in data


def test_compact_tool_result_truncates_plain_text_with_digest():
    compact = _compact_tool_result_for_context("bash", "x" * 5000, max_chars=1000)

    assert len(compact) <= 1000
    assert "sha256=" in compact
    assert "truncated 5000 chars" in compact


def test_context_compaction_threshold_reserves_output_tokens():
    base_threshold = int(MAX_CONTEXT_TOKENS * LOOP_COMPACT_RATIO)

    assert _context_compaction_token_threshold() == base_threshold - RUNTIME_AGENTIC_MAX_TOKENS
    assert _context_compaction_token_threshold(output_reserve_tokens=500) == base_threshold - 500
    assert _context_compaction_token_threshold(output_reserve_tokens=base_threshold + 1000) == 1


@pytest.mark.asyncio
async def test_compact_messages_summarizes_long_non_tool_history():
    messages = [{"role": "system", "content": "system"}]
    for index in range(25):
        messages.append({"role": "user", "content": f"old question {index}"})
        messages.append({"role": "assistant", "content": f"old answer {index}"})
    messages.append({"role": "user", "content": "fresh request"})

    compacted = await _compact_messages(messages, model=None, temperature=0)

    assert len(compacted) < len(messages)
    assert compacted[0]["role"] == "system"
    assert compacted[1]["content"] == "old question 0"
    assert any("[Earlier conversation compacted" in str(message.get("content", "")) for message in compacted)
    assert compacted[-1]["content"] == "fresh request"


@pytest.mark.asyncio
async def test_agentic_loop_compacts_initial_history_before_first_llm_call(monkeypatch):
    observed_message_count = 0
    observed_compacted = False

    async def fake_chat_completion(messages, **kwargs):
        nonlocal observed_message_count, observed_compacted
        observed_message_count = len(messages)
        observed_compacted = any(
            "[Earlier conversation compacted" in str(message.get("content", "")) for message in messages
        )
        return "done", {"prompt": 10, "completion": 1, "total": 11}

    async def fake_executor(name, args):
        return "unused"

    initial_messages = []
    for index in range(25):
        initial_messages.append({"role": "user", "content": f"history question {index}"})
        initial_messages.append({"role": "assistant", "content": f"history answer {index}"})

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", fake_chat_completion)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="latest request",
        tools=[],
        tool_executor=fake_executor,
        initial_messages=initial_messages,
        max_rounds=1,
    )

    assert result.content == "done"
    assert observed_message_count < len(initial_messages) + 2
    assert observed_compacted is True


@pytest.mark.asyncio
async def test_agentic_loop_compacts_before_max_rounds_summary(monkeypatch):
    tool_calls_done = 0
    summary_observed_compacted = False

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal tool_calls_done
        tool_calls_done += 1
        return (
            "",
            [
                {
                    "id": f"call_{tool_calls_done}",
                    "name": "read_file",
                    "arguments": {"path": f"file_{tool_calls_done}.md"},
                }
            ],
            {"prompt": 10, "completion": 1, "total": 11},
        )

    async def fake_chat_completion(messages, **kwargs):
        nonlocal summary_observed_compacted
        summary_observed_compacted = any(
            "[Context compacted" in str(message.get("content", ""))
            or "[Earlier conversation compacted" in str(message.get("content", ""))
            for message in messages
        )
        return "summary", {"prompt": 10, "completion": 1, "total": 11}

    async def fake_executor(name, args):
        return json.dumps({"path": args["path"], "content": "x" * 2000})

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "MESSAGE_COUNT_COMPACT_THRESHOLD", 8)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", fake_chat_completion)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="keep using tools",
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=fake_executor,
        max_rounds=4,
    )

    assert result.stop_reason == "max_rounds"
    assert result.content == "summary"
    assert summary_observed_compacted is True


@pytest.mark.asyncio
async def test_agentic_loop_auto_executes_recommended_next_calls(monkeypatch):
    llm_calls = 0
    executed: list[tuple[str, dict]] = []

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal llm_calls
        llm_calls += 1
        if llm_calls == 1:
            return (
                "",
                [
                    {
                        "id": "call_prepare",
                        "name": "mcp__chrome__observe",
                        "arguments": {"diagnostic": True},
                    }
                ],
                {"prompt": 10, "completion": 1, "total": 11},
            )
        return "done", None, {"prompt": 10, "completion": 1, "total": 11}

    async def fake_executor(name, args):
        executed.append((name, args))
        if len(executed) == 1:
            return json.dumps(
                {
                    "status": "recovery_allowed",
                    "recovery_allowed": True,
                    "recommended_next_calls": [
                        {
                            "name": "mcp__chrome__click_ref",
                            "arguments": {"ref": "btn_1"},
                        },
                        {
                            "name": "mcp__chrome__observe",
                            "arguments": {"diagnostic": True},
                        },
                    ],
                }
            )
        return json.dumps({"status": "complete", "result": {"command": args.get("command")}})

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    tools = [
        {"type": "function", "function": {"name": "mcp__chrome__observe", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "mcp__chrome__click_ref", "parameters": {"type": "object"}}},
    ]
    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="save browser artifact",
        tools=tools,
        tool_executor=fake_executor,
        max_rounds=5,
    )

    assert result.content == "done"
    assert result.rounds == 3
    assert [name for name, _ in executed] == [
        "mcp__chrome__observe",
        "mcp__chrome__click_ref",
        "mcp__chrome__observe",
    ]
    assert executed[1][1]["ref"] == "btn_1"
    assert executed[2][1]["diagnostic"] is True


@pytest.mark.asyncio
async def test_agentic_loop_deduplicates_identical_tool_results(monkeypatch):
    calls = 0
    observed_tool_messages: list[dict] = []

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal calls, observed_tool_messages
        calls += 1
        if calls in (1, 2):
            return (
                "",
                [
                    {
                        "id": f"call_{calls}",
                        "name": "read_file",
                        "arguments": {"path": "same.md"},
                    }
                ],
                {"prompt": 10, "completion": 1, "total": 11},
            )
        observed_tool_messages = [m for m in messages if m.get("role") == "tool"]
        return "done", None, {"prompt": 10, "completion": 1, "total": 11}

    async def fake_executor(name, args):
        return json.dumps({"path": args["path"], "content": "same result" * 200})

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="read twice",
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=fake_executor,
        max_rounds=5,
    )

    assert result.content == "done"
    assert len(observed_tool_messages) == 2
    duplicate_notice = json.loads(observed_tool_messages[1]["content"])
    assert duplicate_notice["repeated_tool_result"] is True
    assert duplicate_notice["tool"] == "read_file"


def test_prompt_cache_counts_reusable_history_prefix():
    payload = {
        "model": "anthropic/claude-sonnet-4.6",
        "messages": [
            {"role": "system", "content": "short stable system"},
            {"role": "user", "content": "historical question " + ("x" * 5000)},
            {"role": "assistant", "content": "historical answer " + ("y" * 5000)},
            {"role": "user", "content": "fresh turn should not decide cacheability"},
        ],
    }

    assert _apply_prompt_cache(payload) is True

    system_blocks = payload["messages"][0]["content"]
    assistant_blocks = payload["messages"][2]["content"]
    assert system_blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert assistant_blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_block_conversion_preserves_cache_control():
    blocks = _openai_content_to_anthropic_blocks(
        [
            {
                "type": "text",
                "text": "cached text",
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )

    assert blocks == [
        {
            "type": "text",
            "text": "cached text",
            "cache_control": {"type": "ephemeral"},
        }
    ]


@pytest.mark.asyncio
async def test_anthropic_native_completion_adds_request_cache_control(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def json(self):
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
                "model": "claude-sonnet-4-6",
            }

    async def fake_post_with_retry(url, headers, payload):
        captured["url"] = url
        captured["payload"] = payload
        return FakeResponse()

    monkeypatch.setattr(
        "packages.core.ai.llm_client._post_with_retry",
        fake_post_with_retry,
    )

    content, tool_calls, usage = await _anthropic_messages_completion(
        api_key="sk-ant-api03-test-key-1234567890",
        base_url="https://api.anthropic.com/v1",
        model="anthropic/claude-sonnet-4.6",
        messages=[{"role": "user", "content": "hello"}],
        temperature=0,
        max_tokens=16,
        prompt_cache=True,
    )

    assert content == "ok"
    assert tool_calls is None
    assert usage["total"] == 2
    assert captured["payload"]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_anthropic_native_tool_completion_streams_text_before_tool_use(monkeypatch):
    events: list[tuple[str, dict]] = []

    class FakeResponse:
        def json(self):
            return {
                "content": [
                    {"type": "text", "text": "我先查看一下当前任务。"},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "manor",
                        "input": {"action": "list_tasks"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "tool_use",
                "model": "claude-sonnet-4-6",
            }

    async def fake_post_with_retry(url, headers, payload):
        return FakeResponse()

    async def stream_handler(event_name: str, payload: dict):
        events.append((event_name, payload))

    monkeypatch.setattr(
        "packages.core.ai.llm_client._post_with_retry",
        fake_post_with_retry,
    )

    content, tool_calls, usage = await _anthropic_messages_completion(
        api_key="sk-ant-api03-test-key-1234567890",
        base_url="https://api.anthropic.com/v1",
        model="anthropic/claude-sonnet-4.6",
        messages=[{"role": "user", "content": "查看 todo"}],
        temperature=0,
        max_tokens=16,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "manor",
                    "description": "Manor tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        stream_handler=stream_handler,
    )

    assert content == "我先查看一下当前任务。"
    assert tool_calls == [{"id": "toolu_123", "name": "manor", "arguments": {"action": "list_tasks"}}]
    assert usage["finish_reason"] == "tool_use"
    assert events == [("text_delta", {"content": "我先查看一下当前任务。"})]


@pytest.mark.asyncio
async def test_search_tools_caps_manifest_results():
    pool = ToolPool()
    for i in range(20):
        pool.register(
            f"video_tool_{i}",
            {
                "type": "function",
                "function": {
                    "name": f"video_tool_{i}",
                    "description": "video helper",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            handler=lambda **_: "ok",
        )
    pool._register_search_tools()

    result = json.loads(await pool.get("search_tools")["handler"](query="video", max_results=100))

    assert len(result["matches"]) == 8
    assert result["loaded_tools"] == [f"video_tool_{i}" for i in range(8)]
    assert all("schema" not in match for match in result["matches"])
    assert all("description" in match for match in result["matches"])


def test_code_tool_is_deferred_but_manor_stays_always_loaded():
    assert "manor" in MASTER_ALWAYS_LOADED
    assert "code" not in MASTER_ALWAYS_LOADED


@pytest.mark.asyncio
async def test_log_token_usage_persists_context_breakdown():
    class FakeDb:
        entry = None

        def add(self, entry):
            self.entry = entry

        async def flush(self):
            return None

    db = FakeDb()
    breakdown = {
        "system_tokens": 10,
        "tool_schema_tokens": 20,
        "tool_result_tokens": 30,
    }

    entry = await log_token_usage(
        db,
        entity_id="ent_1",
        model="anthropic/claude-sonnet-4.6",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        context_breakdown=breakdown,
    )

    assert db.entry is entry
    assert entry.context_breakdown == breakdown
