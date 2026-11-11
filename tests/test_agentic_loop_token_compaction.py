from __future__ import annotations

import json
import importlib
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.agentic_loop import (
    DEFAULT_MAX_ROUNDS,
    _add_usage,
    _compact_messages,
    _compact_search_tools_result_for_context,
    PAGED_TEXT_RESULT_MAX_CHARS,
    SKILL_HANDOFF_RESULT_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    _compact_tool_result_for_context,
    _context_compaction_token_threshold,
    _estimate_context_attribution,
    _strip_inapplicable_integration_continuations,
    LOOP_COMPACT_RATIO,
    MAX_CONTEXT_TOKENS,
)
from packages.core.ai.llm_client import (
    _anthropic_messages_completion,
    _apply_prompt_cache,
    _openai_content_to_anthropic_blocks,
)
from packages.core.ai.runtime.tool_visibility import MASTER_ALWAYS_LOADED
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime import RUNTIME_AGENTIC_MAX_TOKENS
from packages.core.ai.tool_pool import ToolPool
from packages.core.services.usage_service import log_token_usage


@pytest.fixture
def compaction_model(monkeypatch):
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_FIELDS
    response = {key: [] for key in CHECKPOINT_FIELDS}
    response["evidence"] = ["Historical assistant records summarized for budget test."]
    model = AsyncMock(return_value=(json.dumps(response), {}))
    module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(module, "runtime_execute_agentic_compaction_completion", model)
    return model


def test_default_agentic_loop_budget_is_200_rounds() -> None:
    assert DEFAULT_MAX_ROUNDS == 200


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
        ],
    }

    compact = json.loads(
        _compact_search_tools_result_for_context(
            search_result,
            ["mcp__linkedin__get_profile"],
        )
    )

    options = {item["server_key"]: item for item in compact["mcp_options"]}
    assert options["linkedin"]["ready"] is False
    assert options["linkedin"]["authorization_method"] == "oauth"
    assert options["linkedin"]["execution_mode"] == "official_api"


def test_compact_search_tools_result_preserves_permission_failure():
    compact = json.loads(
        _compact_search_tools_result_for_context(
            {
                "error": "blocked_by_permission",
                "message": "The delegated agent's current tool binding has been revoked.",
                "matched_rule": "permission.agent_tool_binding",
                "action_key": "runtime.discovery",
                "capability_id": "runtime.discovery",
                "tool": "search_tools",
            },
            [],
        )
    )

    assert compact["error"] == "blocked_by_permission"
    assert compact["matched_rule"] == "permission.agent_tool_binding"
    assert compact["message"].endswith("revoked.")
    assert compact["tool"] == "search_tools"


def test_compact_search_tools_result_keeps_servers_and_bounded_suppressed_mcp():
    """Review #2 (tool_discovery_v2 B1): servers[] and suppressed_mcp used
    to be dropped entirely by this compaction step, silently discarding
    B1's model-facing value (which server groups matched, and why a
    strongly-related server's tools didn't load) before it ever reached
    the next turn's context."""
    search_result = {
        "query": "post a tweet",
        "matches": [
            {"name": "mcp__twitter_x__create_tweet", "available": False},
        ],
        "servers": [
            {
                "key": "twitter_x",
                "name": "Twitter / X",
                "matched_tools": 1,
                "top_tools": ["mcp__twitter_x__create_tweet"],
            },
        ],
        "suppressed_mcp": [
            {
                "server_key": f"provider_{i}",
                "reason": "outside_active_user_intent",
                "matched_tools": [f"mcp__provider_{i}__tool", "extra1", "extra2", "extra3"],
            }
            for i in range(5)
        ],
    }

    compact = json.loads(
        _compact_search_tools_result_for_context(search_result, [])
    )

    assert compact["servers"] == search_result["servers"]
    # bounded: at most 3 suppressed entries, each with matched_tools capped at 3
    assert len(compact["suppressed_mcp"]) == 3
    for item in compact["suppressed_mcp"]:
        assert set(item.keys()) == {"server_key", "reason", "matched_tools"}
        assert len(item["matched_tools"]) <= 3


def test_compact_paged_read_keeps_pagination_honest():
    """A cut page must say where the visible text stops, not where the tool's
    page stopped: the docx run read 20 000 chars of SKILL.md, saw 1 680 after
    compaction, trusted next_offset=20000 and never saw the docx-js section."""
    raw = json.dumps(
        {
            "path": "/skill/SKILL.md",
            "content": "A" * 10_000,
            "size": 20_000,
            "offset": 0,
            "returned_chars": 10_000,
            "next_offset": 10_000,
            "truncated": True,
            "hint": "More content is available; call read_file again with offset=10000.",
        },
        ensure_ascii=False,
    )

    compact = _compact_tool_result_for_context("sandbox", raw, max_chars=1400)
    data = json.loads(compact)

    assert len(compact) <= 1400
    kept = len(data["content"])
    assert 0 < kept < 1400
    assert data["content"] == "A" * kept, "a clean prefix, so the continuation is exact"
    assert data["returned_chars"] == kept
    assert data["next_offset"] == kept
    assert data["truncated"] is True
    assert f"offset={kept}" in data["hint"]
    assert data["_tool_result_truncated"]["strategy"] == "paged_text"
    assert data["_tool_result_truncated"]["original_chars"] == len(raw)


def test_compact_paged_read_rewrites_web_fetch_and_host_read_shapes():
    fetched = json.dumps(
        {
            "url": "https://example.test",
            "offset": 500,
            "chars_returned": 6_000,
            "total_chars": 9_000,
            "next_offset": 6_500,
            "truncated": True,
            "hint": "continue",
            "content": "B" * 6_000,
        }
    )
    data = json.loads(_compact_tool_result_for_context("web_fetch", fetched, max_chars=1200))
    kept = len(data["content"])
    assert data["chars_returned"] == kept
    assert data["next_offset"] == 500 + kept
    assert f"offset={500 + kept}" in data["hint"]

    host = json.dumps(
        {
            "path": "notes.md",
            "mode": "lines",
            "offset": 0,
            "char_offset": 0,
            "lines_returned": 300,
            "next_offset": 300,
            "next_char_offset": None,
            "truncated": False,
            "char_truncated": False,
            "hint": None,
            "content": "C" * 8_000,
        }
    )
    data = json.loads(_compact_tool_result_for_context("read_file", host, max_chars=1500))
    kept = len(data["content"])
    assert data["next_char_offset"] == kept
    assert data["char_truncated"] is True
    assert data["next_offset"] is None, "a line-based continuation would skip the cut lines"
    assert f"char_offset={kept}" in data["hint"]


def test_paged_reads_get_the_expanded_budget_on_first_delivery():
    raw = json.dumps(
        {
            "path": "/skill/SKILL.md",
            "content": "D" * 9_000,
            "size": 9_000,
            "offset": 0,
            "returned_chars": 9_000,
            "next_offset": None,
            "truncated": False,
            "hint": None,
        }
    )
    assert _compact_tool_result_for_context("sandbox", raw) == raw
    assert PAGED_TEXT_RESULT_MAX_CHARS > len(raw) > TOOL_RESULT_MAX_CHARS


def _handoff(skill_md: str) -> str:
    content = "\n".join(
        [
            "✓ Sandbox ready for skill **docx**",
            "## Runtime Skill Execution Contract",
            "- The complete `/skill/SKILL.md` is provided below and is authoritative.",
            "",
            "## Skill Instructions",
            "The following is the complete `/skill/SKILL.md` loaded for this run. Follow it exactly.",
            "",
            skill_md,
        ]
    )
    return json.dumps(
        {
            "status": "sandbox_ready",
            "skill": "docx",
            "content": content,
            "sandbox_id": "sbx-1",
            "loaded_tools": ["sandbox"],
        },
        ensure_ascii=False,
    )


def test_skill_handoff_delivers_the_whole_skill_md():
    """The handoff declares SKILL.md authoritative; a 20 KB skill must reach
    the model whole instead of being compacted to the default budget."""
    raw = _handoff("# docx\n" + ("workflow line\n" * 1_400))
    assert TOOL_RESULT_MAX_CHARS < len(raw) < SKILL_HANDOFF_RESULT_MAX_CHARS
    assert _compact_tool_result_for_context("invoke_skill", raw) == raw


def test_skill_handoff_over_the_cap_is_cut_at_the_tail_with_a_skill_offset():
    skill_md = "# docx\n" + ("workflow line\n" * 4_000)
    raw = _handoff(skill_md)
    assert len(raw) > SKILL_HANDOFF_RESULT_MAX_CHARS

    data = json.loads(_compact_tool_result_for_context("invoke_skill", raw))
    content = data["content"]
    assert len(json.dumps(data, ensure_ascii=False)) <= SKILL_HANDOFF_RESULT_MAX_CHARS
    assert content.startswith("✓ Sandbox ready"), "the contract at the head survives"
    assert "## Skill Instructions" in content
    assert "/skill/SKILL.md is cut here at char" in content
    assert f"of {len(skill_md)}." in content
    # The marker's offset points at the first char the model did not see.
    import re

    offset = int(re.search(r"offset=(\d+)", content).group(1))
    shown = content.split("\n\n[Context limit")[0]
    skill_shown = shown.split("Follow it exactly.\n\n", 1)[1]
    assert skill_md[:offset] == skill_shown
    assert data["_tool_result_truncated"]["strategy"] == "skill_handoff_tail"


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


@pytest.mark.parametrize("budget", [1000, 1500])
def test_compact_successful_source_update_keeps_structured_contract_diagnostics(budget):
    from packages.core.ai.runtime.streams import runtime_tool_call_error

    result = {
        "status": "source_updated",
        "source_saved": True,
        "session_id": "session-123",
        "project_path": "video/project",
        "source_revision": 4,
        "contract_valid": False,
        "contract_violations": [{
            "file": "index.motion.json",
            "rule": "motion_assertions",
            "pointer": "/assertions/0",
            "expected": "appearsBy or keepsMoving",
            "repair_hint": "Replace the unsupported assertion kind",
        }],
        "authoring_contract": {"minimal_example": "x" * 4000},
        "events": [{"message": "verbose progress " * 400}],
    }
    raw = json.dumps(result)
    assert runtime_tool_call_error(raw) is None

    compact = _compact_tool_result_for_context("video_edit", raw, max_chars=budget)
    payload = json.loads(compact)

    assert len(compact) <= budget
    assert payload["status"] == "source_updated"
    assert payload["source_saved"] is True
    assert payload["session_id"] == "session-123"
    assert payload["contract_valid"] is False
    assert payload["contract_violations"][0]["file"] == "index.motion.json"
    assert payload["contract_violations"][0]["pointer"] == "/assertions/0"
    assert "preview" not in payload
    assert payload.get("contract") != "tool_error"
    assert runtime_tool_call_error(compact) is None
    assert _compact_tool_result_for_context("video_edit", compact, max_chars=budget) == compact


def test_compact_successful_diagnostics_omits_oversized_identity_without_partial_value():
    result = {
        "status": "source_updated",
        "source_saved": True,
        "session_id": "session-" + "s" * 1000,
        "project_path": "project/" + "p" * 1000,
        "source_revision": 2,
        "contract_valid": False,
        "contract_violations": [{"file": "index.motion.json", "pointer": "/assertions/0"}],
    }

    compact = _compact_tool_result_for_context("video_edit", json.dumps(result), max_chars=1000)
    payload = json.loads(compact)

    assert len(compact) <= 1000
    assert payload["omitted_identity_fields"] == ["session_id", "project_path"]
    assert "session_id" not in payload
    assert "project_path" not in payload
    assert payload["source_revision"] == 2
    assert payload["contract_violations"][0]["pointer"] == "/assertions/0"


def test_context_compaction_threshold_reserves_output_tokens():
    base_threshold = int(MAX_CONTEXT_TOKENS * LOOP_COMPACT_RATIO)

    assert _context_compaction_token_threshold() == base_threshold - RUNTIME_AGENTIC_MAX_TOKENS
    assert _context_compaction_token_threshold(output_reserve_tokens=500) == base_threshold - 500
    assert _context_compaction_token_threshold(output_reserve_tokens=base_threshold + 1000) == 1


@pytest.mark.asyncio
async def test_compact_messages_accounts_for_tool_schemas_below_message_limit(monkeypatch):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "MAX_CONTEXT_TOKENS", 10_000)
    messages = [{"role": "system", "content": "system"}]
    for index in range(9):
        messages.extend([
            {"role": "user", "content": f"question {index}"},
            {"role": "assistant", "content": "answer " * 150},
        ])
    messages.append({"role": "user", "content": "Keep working on this request."})
    assert await _compact_messages(
        messages, model=None, temperature=0, output_reserve_tokens=500,
    ) == messages
    async def summarize(prompt, **kwargs):
        from packages.core.ai.runtime.context_compaction import CHECKPOINT_FIELDS
        return json.dumps({field: ["Prior conversation"] if field == "objective" else [] for field in CHECKPOINT_FIELDS}), {}
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_compaction_completion", summarize)
    compacted = await _compact_messages(
        messages, model=None, temperature=0, output_reserve_tokens=500,
        tools=[{"type": "function", "function": {
            "name": "large_schema", "description": "schema " * 4000,
        }}],
    )
    assert len(compacted) < len(messages)
    assert compacted[0] == messages[0]
    assert compacted[-1] == messages[-1]
    assert any("[Earlier conversation compacted" in str(m.get("content")) for m in compacted)


@pytest.mark.asyncio
async def test_compaction_preserves_followup_request_after_prior_tool_turns(monkeypatch):
    # Exercises the opt-in message-count guard (lossless path); the loop default is token-only.
    monkeypatch.setattr("packages.core.ai.agentic_loop.MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    current_request = "Follow up on the saved finding. " * 30
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "Old task"}]
    for index in range(20):
        if index == 4:
            messages.append({"role": "user", "content": current_request})
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call_{index}", "function": {"name": "lookup", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": f"call_{index}", "content": "result"},
            {"role": "assistant", "content": "checked"},
        ])
    compacted = await _compact_messages(
        messages, model=None, temperature=0, active_user_message=current_request,
    )
    assert len(compacted) < len(messages)
    assert any(m.get("content") == current_request for m in compacted)


@pytest.mark.asyncio
async def test_compact_messages_summarizes_long_non_tool_history(compaction_model):
    messages = [{"role": "system", "content": "system"}]
    for index in range(25):
        messages.append({"role": "user", "content": f"old question {index}"})
        messages.append({"role": "assistant", "content": f"old answer {index}" + "x" * 14_000})
    messages.append({"role": "user", "content": "fresh request"})

    compacted = await _compact_messages(messages, model=None, temperature=0)

    assert len(compacted) < len(messages)
    assert compacted[0]["role"] == "system"
    # The stale first question must NOT be promoted as the active task.
    assert compacted[1]["content"] != "old question 0"
    assert any("[Earlier conversation compacted" in str(message.get("content", "")) for message in compacted)
    assert compacted[-1]["content"] == "fresh request"


@pytest.mark.asyncio
async def test_compact_messages_anchors_on_current_user_message_not_first(monkeypatch):
    """Regression: with long chat history plus >3 tool rounds, compaction must
    preserve the *current* turn's user message verbatim — not resurrect the
    conversation's first (stale) question as the active task."""
    # Exercises the opt-in message-count guard (lossless path); the loop default is token-only.
    monkeypatch.setattr("packages.core.ai.agentic_loop.MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    messages = [{"role": "system", "content": "system"}]
    for index in range(30):
        messages.append({"role": "user", "content": f"stale topic {index}"})
        messages.append({"role": "assistant", "content": f"stale answer {index}"})
    messages.append({"role": "user", "content": "migrate the files one by one"})
    for round_index in range(5):
        call_id = f"call_{round_index}"
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "function": {"name": "manor", "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"ok": True, "round": round_index}),
        })

    compacted = await _compact_messages(messages, model=None, temperature=0)

    standalone_contents = [str(m.get("content", "")) for m in compacted]
    assert "migrate the files one by one" in standalone_contents
    assert "stale topic 0" not in standalone_contents


@pytest.mark.asyncio
async def test_compact_messages_preserves_fresh_tool_result_after_long_history(compaction_model):
    messages = [{"role": "system", "content": "system"}]
    for index in range(20):
        messages.append({"role": "user", "content": f"old question {index}"})
        messages.append({"role": "assistant", "content": f"old answer {index}" + "x" * 18_000})
    messages.append({"role": "user", "content": "update the current record"})
    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_fresh",
            "type": "function",
            "function": {"name": "update_record", "arguments": "{}"},
        }],
    })
    fresh_result = json.dumps({
        "ok": True,
        "padding": "x" * 2500,
        "record": {"id": "record-42", "revision": 7, "status": "updated"},
    })
    assert 1000 < len(fresh_result) < 4000
    messages.append({
        "role": "tool",
        "tool_call_id": "call_fresh",
        "content": fresh_result,
    })

    compacted = await _compact_messages(messages, model=None, temperature=0)

    assert len(compacted) < len(messages)
    assert any(
        "[Earlier conversation compacted" in str(message.get("content", ""))
        for message in compacted
    )
    assert compacted[-2]["tool_calls"][0]["id"] == "call_fresh"
    assert compacted[-1]["content"] == fresh_result


@pytest.mark.asyncio
async def test_approval_resume_summary_receives_complete_fresh_tool_result(monkeypatch):
    pending_call = {
        "id": "call_approved_update",
        "name": "update_record",
        "arguments": {"record_id": "record-42"},
    }
    checkpoint_messages = [{"role": "system", "content": "system"}]
    for index in range(20):
        checkpoint_messages.append({"role": "user", "content": f"old question {index}"})
        checkpoint_messages.append({"role": "assistant", "content": f"old answer {index}"})
    checkpoint_messages.extend([
        {"role": "user", "content": "approve the record update"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": pending_call["id"],
                "type": "function",
                "function": {
                    "name": pending_call["name"],
                    "arguments": json.dumps(pending_call["arguments"]),
                },
            }],
        },
    ])
    checkpoint = RuntimeAgentCheckpoint(
        messages=checkpoint_messages,
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call=pending_call,
        disable_followup_tools=True,
    )
    fresh_result = json.dumps({
        "ok": True,
        "padding": "x" * 2500,
        "record": {"id": "record-42", "revision": 7, "status": "updated"},
    })
    observed_tool_result = ""

    async def tool_executor(name, args):
        assert name == "update_record"
        assert args["record_id"] == "record-42"
        return fresh_result

    async def fake_text_completion(messages, **kwargs):
        nonlocal observed_tool_result
        del kwargs
        observed_tool_result = next(
            message["content"]
            for message in reversed(messages)
            if message.get("role") == "tool"
        )
        payload = json.loads(observed_tool_result)
        record = payload["record"]
        return (
            f"Record {record['id']} updated to revision {record['revision']}.",
            {"prompt": 10, "completion": 5, "total": 15},
        )

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    tool_completion = AsyncMock()
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_text_completion",
        fake_text_completion,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        tool_completion,
    )

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="approve the record update",
        tools=[{
            "type": "function",
            "function": {
                "name": "update_record",
                "parameters": {"type": "object"},
            },
        }],
        tool_executor=tool_executor,
        resume_checkpoint=checkpoint.to_dict(),
        max_rounds=3,
    )

    assert observed_tool_result == fresh_result
    assert result.content == "Record record-42 updated to revision 7."
    tool_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_agentic_loop_compacts_initial_history_before_first_llm_call(monkeypatch, compaction_model):
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
        initial_messages.append({"role": "assistant", "content": f"history answer {index}" + "x" * 14_000})

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
async def test_agentic_loop_compacts_before_max_rounds_summary(monkeypatch, compaction_model):
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
    # Token pressure is the only trigger; a small window makes three reads enough.
    monkeypatch.setattr(loop_module, "MAX_CONTEXT_TOKENS", 24_000)
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
async def test_agentic_loop_model_selects_recommended_tool_after_observing_result(monkeypatch):
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
        if llm_calls == 2:
            assert any(
                message.get("role") == "tool"
                and "recommended_next_calls" in str(message.get("content") or "")
                for message in messages
            )
            return (
                "",
                [{"id": "call_model_selected", "name": "mcp__chrome__click_ref", "arguments": {"ref": "btn_1"}}],
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
    assert [name for name, _ in executed] == ["mcp__chrome__observe", "mcp__chrome__click_ref"]
    assert executed[1][1]["ref"] == "btn_1"


@pytest.mark.asyncio
async def test_agentic_loop_does_not_force_provider_recommended_calls(monkeypatch):
    executed: list[str] = []
    llm_calls = 0

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal llm_calls
        llm_calls += 1
        if llm_calls == 1:
            return (
                "",
                [{"id": "call_observe", "name": "mcp__chrome__observe", "arguments": {}}],
                {"prompt": 10, "completion": 1, "total": 11},
            )
        assert any(
            message.get("role") == "tool"
            and "recommended_next_calls" in str(message.get("content") or "")
            for message in messages
        )
        return "No further action needed.", None, {"prompt": 10, "completion": 1, "total": 11}

    async def fake_executor(name, args):
        executed.append(name)
        return json.dumps({
            "status": "recovery_allowed",
            "recovery_allowed": True,
            "recommended_next_calls": [
                {"name": "mcp__chrome__click_ref", "arguments": {"ref": "btn_1"}},
            ],
        })

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="inspect browser",
        tools=[
            {"type": "function", "function": {"name": "mcp__chrome__observe", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "mcp__chrome__click_ref", "parameters": {"type": "object"}}},
        ],
        tool_executor=fake_executor,
        max_rounds=3,
    )

    assert result.content == "No further action needed."
    assert executed == ["mcp__chrome__observe"]
    assert llm_calls == 2


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


@pytest.mark.asyncio
async def test_agentic_loop_tells_a_run_repeating_known_results_once(monkeypatch):
    """Rounds that only re-read what the run already had are a loop; after
    three in a row the run is told so, once, and a new result ends the streak."""
    seen: list[list[dict]] = []

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        seen.append(list(messages))
        if len(seen) <= 6:
            return "", [{"id": f"call_{len(seen)}", "name": "read_file", "arguments": {"path": "same.md"}}], {}
        return "done", None, {}

    async def fake_executor(name, args):
        return json.dumps({"path": args["path"], "content": "same result"})

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="read",
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=fake_executor,
        max_rounds=10,
    )

    def notices(messages):
        return [m for m in messages if m.get("role") == "user" and '"no_progress"' in str(m.get("content"))]

    assert result.content == "done"
    # Round 1 is new; rounds 2-4 repeat it, so the notice precedes the 5th call.
    assert not notices(seen[3]) and len(notices(seen[4])) == 1
    assert len(notices(seen[-1])) == 1
    calls = 0
    observed_tool_messages: list[dict] = []

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal calls, observed_tool_messages
        calls += 1
        if calls in (1, 2):
            return (
                "",
                [{"id": f"call_{calls}", "name": "video_edit", "arguments": {"operation": "save"}}],
                {"prompt": 10, "completion": 1, "total": 11},
            )
        observed_tool_messages = [message for message in messages if message.get("role") == "tool"]
        return "done", None, {"prompt": 10, "completion": 1, "total": 11}

    async def fake_executor(_name, _args):
        return json.dumps({
            "status": "source_updated",
            "source_saved": True,
            "session_id": "session-123",
            "contract_valid": False,
            "contract_violations": [{"file": "index.motion.json", "pointer": "/assertions/0"}],
            "authoring_contract": {"minimal_example": "x" * 4000},
        })

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_chat_completion_with_tools)

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="save video source",
        tools=[{"type": "function", "function": {"name": "video_edit", "parameters": {"type": "object"}}}],
        tool_executor=fake_executor,
        max_rounds=5,
    )

    assert result.content == "done"
    assert len(observed_tool_messages) == 2
    for message in observed_tool_messages:
        payload = json.loads(message["content"])
        assert payload["status"] == "source_updated"
        assert payload["contract_valid"] is False
        assert payload["contract_violations"][0]["pointer"] == "/assertions/0"
        assert "repeated_tool_result" not in payload


@pytest.mark.asyncio
async def test_agentic_loop_preserves_executor_failure_in_model_context_for_recovery(monkeypatch):
    calls = 0
    observed_tool_content = ""

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal calls, observed_tool_content
        calls += 1
        if calls == 1:
            return (
                "",
                [{
                    "id": "call_private",
                    "name": "read_file",
                    "arguments": {"path": "project/script.md"},
                }],
                {"prompt": 10, "completion": 1, "total": 11},
            )
        observed_tool_content = next(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "tool"
        )
        return "done", None, {"prompt": 10, "completion": 1, "total": 11}

    async def failing_executor(_name, _args):
        raise RuntimeError(
            "Tool key read_file does not match registered handler test_reader"
        )

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_chat_completion_with_tools,
    )

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="run the operation",
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object"},
            },
        }],
        tool_executor=failing_executor,
        max_rounds=3,
    )

    assert result.content == "done"
    error = json.loads(observed_tool_content)
    assert error["contract"] == "tool_error"
    assert error["tool"] == "read_file"
    assert "Tool error (read_file):" in error["message"]
    assert "test_reader" in observed_tool_content
    assert "registered handler" in observed_tool_content


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"integration_account_continuation": "invented"}, {}),
        (
            {
                "integration_account_selection": "all",
                "integration_account_continuation": "/",
            },
            {
                "integration_account_selection": "all",
            },
        ),
    ],
)
def test_agentic_loop_normalizes_model_integration_continuations(arguments, expected):
    calls = [{"name": "mcp__email__list_folders", "arguments": arguments}]

    _strip_inapplicable_integration_continuations(calls)

    assert calls[0]["arguments"] == expected


@pytest.mark.asyncio
async def test_agentic_loop_replays_all_account_continuation_without_model_token_access(
    monkeypatch,
):
    model_calls = 0
    final_model_messages = ""

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        nonlocal model_calls, final_model_messages
        model_calls += 1
        if model_calls == 1:
            return (
                "",
                [{
                    "id": "email-folders",
                    "name": "mcp__email__list_folders",
                    "arguments": {
                        "integration_account_selection": "all",
                        "integration_account_continuation": "/",
                    },
                }],
                {"prompt": 10, "completion": 1, "total": 11},
            )
        final_model_messages = json.dumps(messages, ensure_ascii=False)
        return "done", None, {"prompt": 10, "completion": 1, "total": 11}

    executor_calls: list[tuple[str, dict]] = []
    observed_results: list[str] = []

    async def fake_executor(name, arguments):
        executor_calls.append((name, dict(arguments)))
        if len(executor_calls) == 1:
            return json.dumps({
                "server": "email",
                "tool": "list_folders",
                "integration_account_selection": "all",
                "status": "partial",
                "omitted_account_count": 1,
                "continuation": {
                    "mode": "all",
                    "argument": "integration_account_continuation",
                    "token": "runtime-signed-token",
                    "instruction": "Send this token back.",
                },
                "results": [{"integration_account_id": "account-1", "ok": True}],
            })
        return json.dumps({
            "server": "email",
            "tool": "list_folders",
            "integration_account_selection": "all",
            "status": "complete",
            "omitted_account_count": 0,
            "continuation": None,
            "results": [{"integration_account_id": "account-2", "ok": True}],
        })

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_chat_completion_with_tools,
    )

    result = await loop_module.agentic_loop(
        system_prompt="test",
        user_message="list folders across all connected email accounts",
        tools=[{
            "type": "function",
            "function": {
                "name": "mcp__email__list_folders",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "integration_account_selection": {"type": "string"},
                        "integration_account_continuation": {"type": "string"},
                    },
                },
            },
        }],
        tool_executor=fake_executor,
        on_tool_end=lambda _name, result, *_args: observed_results.append(str(result)),
        max_rounds=5,
    )

    assert result.content == "done"
    assert result.tool_calls_made == [
        "mcp__email__list_folders",
        "mcp__email__list_folders",
    ]
    call_ids = [arguments["_runtime_tool_call_id_from_context"] for _, arguments in executor_calls]
    assert call_ids[0] == "email-folders"
    assert call_ids[1].startswith("auto_2_0_")
    assert call_ids[1] != call_ids[0]
    assert [
        (name, {key: value for key, value in arguments.items() if key != "_runtime_tool_call_id_from_context"})
        for name, arguments in executor_calls
    ] == [
        (
            "mcp__email__list_folders",
            {"integration_account_selection": "all"},
        ),
        (
            "mcp__email__list_folders",
            {
                "integration_account_selection": "all",
                "integration_account_continuation": "runtime-signed-token",
            },
        ),
    ]
    assert model_calls == 2
    assert "runtime-signed-token" not in final_model_messages
    assert "_runtime_tool_call_id_from_context" not in final_model_messages
    assert "runtime-signed-token" not in "".join(observed_results)
    assert "runtime_managed" in final_model_messages


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
    captured: dict = {}

    stream_lines = [
        'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":0}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"我先查看"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"一下当前任务。"}}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_123","name":"manor","input":{}}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"action\\":"}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"list_tasks\\"}"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}',
        'data: {"type":"message_stop"}',
    ]

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in stream_lines:
                yield line

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def stream(self, method, url, *, headers, json):
            captured.update(method=method, url=url, headers=headers, payload=json)
            return FakeStreamContext()

    async def fake_get_llm_client():
        return FakeClient()

    async def stream_handler(event_name: str, payload: dict):
        events.append((event_name, payload))

    monkeypatch.setattr(
        "packages.core.ai.llm_client.get_llm_client",
        fake_get_llm_client,
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
    assert usage["total"] == 15
    assert captured["payload"]["stream"] is True
    assert [event for event in events if event[0] == "text_delta"] == [
        ("text_delta", {"content": "我先查看"}),
        ("text_delta", {"content": "一下当前任务。"}),
    ]
    # The tool call is announced (display only) once its name streams in.
    assert [payload["name"] for kind, payload in events if kind == "tool_call_draft"] == ["manor"]


@pytest.mark.asyncio
async def test_anthropic_native_stream_resets_text_before_buffered_fallback(monkeypatch):
    events: list[tuple[str, dict]] = []

    class FailingStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"old"}}'
            raise TimeoutError("stream stalled")

    class FakeStreamContext:
        async def __aenter__(self):
            return FailingStreamResponse()

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            return FakeStreamContext()

    async def fake_get_llm_client():
        return FakeClient()

    async def fake_post_with_retry(url, headers, payload):
        del url, headers, payload
        raise AssertionError("buffered fallback must not run after a stalled stream")

    async def stream_handler(event_name: str, payload: dict):
        events.append((event_name, payload))

    monkeypatch.setattr(
        "packages.core.ai.llm_client.get_llm_client",
        fake_get_llm_client,
    )
    monkeypatch.setattr(
        "packages.core.ai.llm_client._post_with_retry",
        fake_post_with_retry,
    )

    with pytest.raises(TimeoutError, match="stalled"):
        await _anthropic_messages_completion(
            api_key="sk-ant-api03-test-key-1234567890",
            base_url="https://api.anthropic.com/v1",
            model="anthropic/claude-sonnet-4.6",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0,
            max_tokens=16,
            stream_handler=stream_handler,
        )

    assert events == [
        ("text_delta", {"content": "old"}),
        ("text_reset", {}),
    ]


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
