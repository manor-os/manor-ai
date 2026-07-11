"""
Low-level agentic loop primitive: the core "call LLM -> execute tools -> loop"
pattern extracted so agents, tools, sub-agents, goal steps, and document
generators can iterate with tools until the job is done.

Modelled after Claude Code's query.ts: a single while-loop that gives the LLM
agency over which tools to use and when to stop, instead of a developer-defined
fixed pipeline.

Production Manor entrypoints should call ``runtime_execute_agentic_loop()`` so
tool execution, deferred schema loading, skills, policy, trace, and subagent
constraints stay Runtime Harness-owned. Direct use of this module is reserved
for the Runtime Harness itself and low-level tests.

Low-level usage:
    from packages.core.ai.agentic_loop import agentic_loop

    result = await agentic_loop(
        system_prompt="You are a document writer. ...",
        user_message="Write a market analysis for Q4 2025",
        tools=[knowledge_search_schema, web_search_schema, ...],
        tool_executor=my_tool_executor,   # async (name, args) -> str
        model=model,
        max_rounds=20,
    )
    # result.content  -- final LLM text
    # result.messages -- full conversation history
    # result.usage    -- accumulated token usage
    # result.rounds   -- how many LLM calls it took
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from packages.core.ai.llm_client import (
    EMPTY_USAGE,
    CreditExhaustedError,
    mark_byok_from_metadata,
    _preflight_credit_check,
)
from packages.core.ai.runtime.agentic_llm import (
    RUNTIME_AGENTIC_MAX_TOKENS,
    runtime_execute_agentic_compaction_completion,
    runtime_execute_agentic_final_completion,
    runtime_execute_agentic_round_text_completion,
    runtime_execute_agentic_round_tool_completion,
)
from packages.core.ai.runtime.agentic_loop_prompts import (
    runtime_agentic_auto_next_calls_message,
    runtime_agentic_empty_response_retry_message,
    runtime_agentic_is_structural_compaction_message,
    runtime_agentic_llm_compaction_prompt,
    runtime_agentic_llm_compaction_replacement,
    runtime_agentic_max_rounds_final_prompt,
    runtime_agentic_structural_compaction_message,
    runtime_agentic_truncated_tool_call_retry_message,
)

logger = logging.getLogger(__name__)

# Type for the tool executor callback
ToolExecutor = Callable[[str, Dict[str, Any]], Awaitable[str]]

DEFAULT_MAX_ROUNDS = 100
TOOL_RESULT_MAX_CHARS = 4000
LOOP_COMPACT_RATIO = 0.75
MAX_CONTEXT_TOKENS = 128_000
MAX_CONSECUTIVE_TRUNCATIONS = 3
MAX_EMPTY_LLM_RESPONSES = 2
_REASONING_USAGE_KEY = "_reasoning_content"
_ATTRIBUTION_CHAR_TO_TOKEN_RATIO = 4
_DUPLICATE_TOOL_RESULT_MAX_PREVIEW = 240
_SERIAL_BROWSER_TOOL_PREFIXES = (
    "mcp__chrome__",
    "mcp__local_browser__",
)
RECENT_CHAT_MESSAGES_TO_KEEP = int(
    os.environ.get("LOOP_RECENT_CHAT_MESSAGES_TO_KEEP", "12")
)
COMPACTED_HISTORY_MAX_ITEMS = 24
COMPACTED_HISTORY_PREVIEW_CHARS = 180
FINAL_RESPONSE_SENTINEL = "<manor-final-response>"
FINAL_RESPONSE_SENTINELS = (
    FINAL_RESPONSE_SENTINEL,
    "</manor-final-response>",
)


def _strip_final_response_sentinel(text: str | None) -> str:
    """Remove the final-response stream marker the model is told to emit.

    Everything before the last marker is progress narration; the user-facing
    answer follows it. Without this the raw ``<manor-final-response>`` marker
    leaks into persisted task/plan output (the streaming chat path strips it
    separately). Safe no-op when no marker is present."""
    if not text:
        return ""
    stripped = str(text)
    idx = stripped.rfind(FINAL_RESPONSE_SENTINEL)
    if idx != -1:
        stripped = stripped[idx + len(FINAL_RESPONSE_SENTINEL):]
    for sentinel in FINAL_RESPONSE_SENTINELS:
        stripped = stripped.replace(sentinel, "")
    return stripped.strip()


def _with_final_response_sentinel_guidance(system_prompt: str) -> str:
    prompt = str(system_prompt or "")
    return (
        f"{prompt.rstrip()}\n\n"
        "## Final Response Stream Marker\n"
        f"- After all required tool calls are complete, output `{FINAL_RESPONSE_SENTINEL}` "
        "immediately before your final user-facing answer.\n"
        "- Do not output this marker before progress notes or before any tool call.\n"
        "- Do not mention, quote, translate, wrap, or explain the marker."
    ).strip()


async def _emit_stream_event(
    handler: Optional[Callable],
    event_name: str,
    payload: Dict[str, Any],
) -> None:
    if handler is None:
        return
    maybe_result = handler(event_name, payload)
    if inspect.isawaitable(maybe_result):
        await maybe_result


TOOL_RESULT_JSON_FIELD_MAX_CHARS = 1800
TOOL_RESULT_JSON_LIST_MAX_ITEMS = 20
TOOL_RESULT_JSON_OBJECT_MAX_KEYS = 40

# Compaction also fires when message count grows beyond this threshold,
# even if total tokens are under the ratio cap. Long conversations with
# small messages still poison new-question intent: a 5-token user
# question competes against 80+ messages of unrelated prior context.
# Keeping ``MESSAGE_COUNT_COMPACT_THRESHOLD`` low forces summarization
# of stale tool rounds before the noise drowns the signal.
MESSAGE_COUNT_COMPACT_THRESHOLD = int(
    os.environ.get("LOOP_MESSAGE_COMPACT_THRESHOLD", "40")
)

SKILL_TERMINAL_STOP_REASON = "skill_terminal"
MEDIA_GENERATION_TERMINAL_STOP_REASON = "media_generation_tool_result"
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_MEDIA_GENERATION_KINDS = {"image", "video", "audio"}


@dataclass
class AgenticResult:
    """Result from an agentic loop run."""
    content: str                           # Final text response from the LLM
    messages: List[Dict[str, Any]]         # Full conversation history
    usage: Dict[str, Any]                  # Accumulated token usage
    rounds: int                            # Number of LLM calls made
    tool_calls_made: List[str] = field(default_factory=list)  # Names of all tools called
    stop_reason: str = "completed"         # completed | max_rounds | error | credit_exhausted | skill_terminal
    error: Optional[str] = None            # Error detail when stop_reason != completed
    error_detail: Optional[Dict[str, Any]] = None
    control: Optional[Dict[str, Any]] = None


def _message_text(value: "str | list[dict] | Any") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text is not None:
                parts.append(str(text))
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and image_url.get("url"):
                parts.append(str(image_url.get("url"))[:32])
        return " ".join(parts).strip()
    return str(value or "")


def _forced_tool_call_preface(name: str, args: Dict[str, Any], user_message: "str | list[dict]") -> str:
    kind = str(args.get("kind") or "").strip().lower()
    prefers_zh = bool(_CJK_RE.search(_message_text(user_message)))
    if name == "generate_file":
        if kind == "code":
            return "我先生成这个代码文件。" if prefers_zh else "I'll generate the code file first."
        if kind in {"image", "video", "audio"}:
            return "我先启动媒体生成。" if prefers_zh else "I'll start the media generation first."
        if kind in {"document", "presentation", "spreadsheet", "diagram"}:
            return "我先生成这个文件。" if prefers_zh else "I'll generate the file first."
        return "我先生成文件。" if prefers_zh else "I'll generate the file first."
    if name == "invoke_skill":
        return "我先调用相关技能处理。" if prefers_zh else "I'll invoke the relevant skill first."
    return "我先执行必要的工具调用。" if prefers_zh else "I'll run the required tool call first."


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    return str(tool_call.get("name") or "").strip()


def _tool_call_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    args = tool_call.get("arguments")
    return args if isinstance(args, dict) else {}


def _json_object_from_tool_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    text = str(result or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)]


def _localized_policy_text(value: Any, user_message: "str | list[dict]") -> str:
    if isinstance(value, dict):
        key = "zh" if _CJK_RE.search(_message_text(user_message)) else "en"
        return str(value.get(key) or value.get("default") or value.get("en") or value.get("zh") or "")
    return str(value or "")


def _terminal_tool_rules(policy: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(policy, dict):
        return []
    rules = policy.get("terminal_tool_results")
    if rules is None:
        rules = policy.get("rules")
    if isinstance(rules, dict):
        return [rules]
    if isinstance(rules, list):
        return [rule for rule in rules if isinstance(rule, dict)]
    return []


def _tool_call_matches_terminal_rule(tool_call: dict[str, Any], rule: dict[str, Any]) -> bool:
    name = _tool_call_name(tool_call)
    if not name:
        return False
    names = {item.strip() for item in _string_list(rule.get("tool_names") or rule.get("tools"))}
    if names and name not in names:
        return False
    prefixes = tuple(item.strip() for item in _string_list(rule.get("tool_prefixes")) if item.strip())
    if prefixes and not name.startswith(prefixes):
        return False
    suffixes = tuple(item.strip() for item in _string_list(rule.get("tool_suffixes")) if item.strip())
    if suffixes and not name.endswith(suffixes):
        return False
    return bool(names or prefixes or suffixes)


def _json_rule_matches(result: Any, rule: dict[str, Any]) -> bool:
    statuses = {item.lower() for item in _string_list(rule.get("statuses") or rule.get("status"))}
    equals = rule.get("json_equals") or {}
    if statuses or equals:
        parsed = _json_object_from_tool_result(result)
        if not parsed:
            return False
        if statuses and str(parsed.get("status") or "").strip().lower() not in statuses:
            return False
        if isinstance(equals, dict):
            for key, expected in equals.items():
                allowed = {item.lower() for item in _string_list(expected)}
                actual = str(parsed.get(str(key)) or "").strip().lower()
                if allowed and actual not in allowed:
                    return False
    return True


def _terminal_control_from_rule(
    rule: dict[str, Any],
    user_message: "str | list[dict]",
) -> dict[str, Any]:
    content = _localized_policy_text(rule.get("notice") or rule.get("content"), user_message)
    control = {
        "terminal": True,
        "content": content,
        "stop_reason": str(rule.get("stop_reason") or SKILL_TERMINAL_STOP_REASON),
        "stop_parent": bool(rule.get("stop_parent")),
        "notice_key": rule.get("notice_key"),
        "replace_visible_text": bool(rule.get("replace_visible_text", True)),
    }
    return {key: value for key, value in control.items() if value is not None}


def _detect_terminal_tool_result(
    results: list[tuple[dict[str, Any], Any]],
    policy: dict[str, Any] | None,
    user_message: "str | list[dict]",
) -> dict[str, Any] | None:
    for rule in _terminal_tool_rules(policy):
        for tool_call, result in results:
            if _tool_call_matches_terminal_rule(tool_call, rule) and _json_rule_matches(result, rule):
                return _terminal_control_from_rule(rule, user_message)
    return None


def _detect_stop_parent_tool_result(
    results: list[tuple[dict[str, Any], Any]],
    user_message: "str | list[dict]",
) -> dict[str, Any] | None:
    for tool_call, result in results:
        parsed = _json_object_from_tool_result(result)
        if not parsed or not parsed.get("stop_parent"):
            continue
        content = str(parsed.get("content") or parsed.get("message") or "")
        if not content:
            content = _localized_policy_text(parsed.get("notice"), user_message)
        control = {
            "terminal": True,
            "content": content,
            "stop_reason": str(parsed.get("stop_reason") or SKILL_TERMINAL_STOP_REASON),
            "stop_parent": True,
            "notice_key": parsed.get("notice_key"),
            "replace_visible_text": bool(parsed.get("replace_visible_text", True)),
            "source_tool": _tool_call_name(tool_call),
        }
        return {key: value for key, value in control.items() if value is not None}
    return None


def _media_generation_default_notice(kind: str, user_message: "str | list[dict]") -> str:
    prefers_zh = bool(_CJK_RE.search(_message_text(user_message)))
    if kind == "image":
        return "图片已生成。" if prefers_zh else "Image generated."
    if kind == "audio":
        return "音频已生成。" if prefers_zh else "Audio generated."
    return "视频生成已开始。" if prefers_zh else "Video generation started."


def _detect_forced_media_generation_result(
    results: list[tuple[dict[str, Any], Any]],
    user_message: "str | list[dict]",
) -> dict[str, Any] | None:
    if not results:
        return None

    kinds: list[str] = []
    notices: list[str] = []
    for tool_call, result in results:
        if _tool_call_name(tool_call) != "generate_file":
            return None

        args = _tool_call_args(tool_call)
        parsed = _json_object_from_tool_result(result)
        kind = str(
            (parsed or {}).get("kind")
            or args.get("kind")
            or ""
        ).strip().lower()
        if kind not in _MEDIA_GENERATION_KINDS:
            return None

        # A pending async job (video) is not done yet — do NOT end the turn on
        # its "started" placeholder. Returning None lets the loop chain the
        # forced wait_media_jobs and report the real completed/failed outcome.
        if parsed and str(parsed.get("status") or "") == "pending" and parsed.get("job_id"):
            return None
        kinds.append(kind)

        if parsed:
            message = str(parsed.get("message") or "").strip()
            error = str(parsed.get("error") or "").strip()
            if error:
                notices.append(f"{kind.title()} generation failed: {error}"[:500])
            elif message:
                notices.append(message[:500])

    content = "\n".join(notices).strip()
    if not content:
        content = _media_generation_default_notice(kinds[-1] if kinds else "video", user_message)
    return {
        "terminal": True,
        "content": content,
        "stop_reason": MEDIA_GENERATION_TERMINAL_STOP_REASON,
        "replace_visible_text": False,
        "source_tool": "generate_file",
    }


def _requires_serial_tool_execution(tool_name: str) -> bool:
    """Stateful local browser tools share one visible page and must not race."""
    name = str(tool_name or "")
    return name.startswith(_SERIAL_BROWSER_TOOL_PREFIXES) or name.endswith("__browser_action")


def _usage_indicates_byok(usage: Dict[str, Any]) -> bool:
    if bool(usage.get("byok")):
        return True
    for key in ("billing_mode", "llm_billing_mode", "api_key_source", "llm_api_key_source"):
        if str(usage.get(key) or "").lower() == "byok":
            return True
    return False


def _usage_has_tokens(usage: Dict[str, Any]) -> bool:
    return any(
        int(usage.get(key) or 0) > 0
        for key in ("prompt", "prompt_tokens", "completion", "completion_tokens", "total", "total_tokens")
    )


def _merge_usage_billing_source(total: Dict[str, Any], new: Dict[str, Any]) -> None:
    if not _usage_has_tokens(new):
        return

    current_mode = str(total.get("llm_billing_mode") or total.get("billing_mode") or "").lower()
    if _usage_indicates_byok(new):
        if current_mode in ("", "byok"):
            total["byok"] = True
            total["billing_mode"] = "byok"
            total["llm_billing_mode"] = "byok"
            total["api_key_source"] = "byok"
            total["llm_api_key_source"] = "byok"
        else:
            total["byok"] = False
            total["billing_mode"] = "mixed"
            total["llm_billing_mode"] = "mixed"
        return

    if current_mode == "byok":
        total["byok"] = False
        total["billing_mode"] = "mixed"
        total["llm_billing_mode"] = "mixed"
    elif current_mode == "":
        total["byok"] = False
        total["billing_mode"] = "platform"
        total["llm_billing_mode"] = "platform"


def _add_usage(total: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Accumulate token usage dicts in place."""
    for key in (
        "prompt", "prompt_tokens", "completion", "completion_tokens", "total", "total_tokens",
        "cache_read", "cache_read_input_tokens", "cache_creation", "cache_creation_input_tokens",
    ):
        if key in new:
            total[key] = total.get(key, 0) + int(new.get(key) or 0)
    for key in ("model", "provider"):
        if new.get(key) and not total.get(key):
            total[key] = new[key]
    _merge_usage_billing_source(total, new)
    if new.get("cost_usd") is not None:
        try:
            total["cost_usd"] = float(total.get("cost_usd") or 0) + float(new.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    attribution = new.get("context_attribution")
    if isinstance(attribution, dict):
        total["context_attribution_last"] = attribution
        aggregate = dict(total.get("context_attribution_total") or {})
        _merge_context_attribution(aggregate, attribution)
        total["context_attribution_total"] = aggregate


def _credit_exhausted_result(
    exc: CreditExhaustedError,
    *,
    messages: List[Dict[str, Any]],
    usage: Dict[str, Any],
    rounds: int,
    tool_calls_made: List[str],
) -> AgenticResult:
    last_content = ""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            last_content = str(m["content"])
            break
    return AgenticResult(
        content=last_content or f"[Credits exhausted: {exc}]",
        messages=messages,
        usage=usage,
        rounds=rounds,
        tool_calls_made=tool_calls_made,
        stop_reason="credit_exhausted",
        error=str(exc),
        error_detail={
            "message": str(exc),
            "plan": exc.plan,
            "limit": exc.limit,
            "current": exc.current,
        },
    )


def _usage_total(usage: Dict[str, Any] | None) -> int:
    if not usage:
        return 0
    return int(usage.get("total") or usage.get("total_tokens") or 0)


def _auto_tool_calls_from_result(tool_result: dict[str, Any], loaded_tool_names: set[str | None]) -> list[dict[str, Any]]:
    status = str(tool_result.get("status") or "")
    # A pending video job is async; force wait_media_jobs in the same turn so the
    # agent reports the real outcome (completed artifact or failure) instead of
    # ending on a "started" placeholder that hides a later failure.
    #
    # No `loaded_tool_names` gate here: wait_media_jobs is a deferred tool (not in
    # the eager surface), so in free-form chat it is never pre-loaded. Forced
    # auto-calls execute directly against the tool pool, where wait_media_jobs is
    # always registered alongside generate_file — so the wait must fire regardless
    # of what the LLM currently sees, otherwise the turn ends on the placeholder.
    if (
        str(tool_result.get("kind") or "") == "video"
        and status == "pending"
        and tool_result.get("job_id")
    ):
        return [{
            "name": "wait_media_jobs",
            "arguments": {"job_ids": [str(tool_result["job_id"])]},
        }]
    mode = str(tool_result.get("tool_mode") or "")
    ai_guided_recovery = (
        mode == "ai_guided_local_browser"
        or status == "recovery_allowed"
        or tool_result.get("recovery_allowed") is True
    )
    if not ai_guided_recovery:
        return []
    next_calls = tool_result.get("recommended_next_calls") or []
    calls: list[dict[str, Any]] = []
    for call in next_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or call.get("tool_name") or "").strip()
        args = call.get("arguments") or call.get("args") or {}
        if not name.startswith("mcp__") or name not in loaded_tool_names:
            continue
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name, "arguments": args})
    return calls


def _dedupe_auto_tool_calls(calls: list[dict[str, Any]], *, seen: set[str] | None = None) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    local_seen: set[str] = set()
    global_seen = seen if seen is not None else set()
    for call in calls:
        name = str(call.get("name") or "")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        key = name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        if name and key not in local_seen and key not in global_seen:
            deduped.append({"name": name, "arguments": args})
            local_seen.add(key)
            global_seen.add(key)
    return deduped


def _is_empty_llm_response(content: Any, tool_calls: Any, usage: Dict[str, Any] | None) -> bool:
    return (
        not tool_calls
        and not str(content or "").strip()
        and not str((usage or {}).get("finish_reason") or "").strip()
        and not (usage or {}).get("error")
        and _usage_total(usage) == 0
    )


def _llm_call_failed_result(
    error: str,
    *,
    messages: List[Dict[str, Any]],
    usage: Dict[str, Any],
    rounds: int,
    tool_calls_made: List[str],
) -> "AgenticResult":
    detail = (error or "Unknown LLM provider error").strip()
    return AgenticResult(
        content=(
            "Sorry, the request failed before the model could respond. "
            "Please check the selected model and API key configuration.\n\n"
            f"Error detail: {detail}"
        ),
        messages=messages,
        usage=usage,
        rounds=rounds,
        tool_calls_made=tool_calls_made,
        stop_reason="error",
        error="llm_call_failed",
    )


def _pop_reasoning_content(usage: Dict[str, Any] | None) -> str | None:
    if not isinstance(usage, dict):
        return None
    value = usage.pop(_REASONING_USAGE_KEY, None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _attach_reasoning_content(message: Dict[str, Any], reasoning_content: str | None) -> None:
    if reasoning_content:
        message["reasoning_content"] = reasoning_content


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: 4 chars ≈ 1 token."""
    total = 0
    for m in messages:
        total += len(str(m.get("content", "")))
        if m.get("reasoning_content"):
            total += len(str(m.get("reasoning_content", "")))
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return total // 4


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _looks_like_file_context(message: dict, text: str) -> bool:
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"image_url", "input_audio"}:
                return True
    markers = (
        "<attached_files",
        "[Image:",
        "[Image from KB:",
        "[Attachment notes:",
        "[Context truncated:",
        "## File:",
        "## Document:",
        "## Audio:",
        "path=uploads/",
        "document_id=",
    )
    return any(marker in text for marker in markers)


def _add_attribution_chars(stats: dict[str, int], bucket: str, chars: int) -> None:
    if chars <= 0:
        return
    stats[f"{bucket}_chars"] = stats.get(f"{bucket}_chars", 0) + chars
    stats[f"{bucket}_tokens"] = (
        stats.get(f"{bucket}_tokens", 0)
        + max(1, chars // _ATTRIBUTION_CHAR_TO_TOKEN_RATIO)
    )


def _estimate_context_attribution(messages: list[dict], tools: list[dict] | None) -> dict[str, int]:
    """Roughly attribute prompt input to stable buckets.

    This is intentionally heuristic: provider tokenizers differ, and the
    final source of truth remains the provider-reported prompt token count.
    The breakdown answers "what is dominating the prompt?" for admin/debug UI.
    """
    stats: dict[str, int] = {
        "system_chars": 0,
        "system_tokens": 0,
        "history_chars": 0,
        "history_tokens": 0,
        "user_input_chars": 0,
        "user_input_tokens": 0,
        "file_chars": 0,
        "file_tokens": 0,
        "tool_result_chars": 0,
        "tool_result_tokens": 0,
        "tool_schema_chars": 0,
        "tool_schema_tokens": 0,
        "tool_call_chars": 0,
        "tool_call_tokens": 0,
    }
    last_user_idx = -1
    for idx, message in enumerate(messages):
        if message.get("role") == "user":
            last_user_idx = idx

    for idx, message in enumerate(messages):
        role = message.get("role")
        text = _content_to_text(message.get("content", ""))
        content_chars = len(text)
        if role == "system":
            _add_attribution_chars(stats, "system", content_chars)
        elif role == "tool":
            _add_attribution_chars(stats, "tool_result", content_chars)
        elif role == "assistant":
            if content_chars:
                _add_attribution_chars(stats, "history", content_chars)
            if message.get("tool_calls"):
                try:
                    tool_call_chars = len(json.dumps(message["tool_calls"], ensure_ascii=False, default=str))
                except Exception:
                    tool_call_chars = len(str(message["tool_calls"]))
                _add_attribution_chars(stats, "tool_call", tool_call_chars)
        elif role == "user":
            if _looks_like_file_context(message, text):
                _add_attribution_chars(stats, "file", content_chars)
            elif idx == last_user_idx:
                _add_attribution_chars(stats, "user_input", content_chars)
            else:
                _add_attribution_chars(stats, "history", content_chars)
        elif content_chars:
            _add_attribution_chars(stats, "history", content_chars)

        if message.get("reasoning_content"):
            _add_attribution_chars(
                stats,
                "history",
                len(str(message.get("reasoning_content") or "")),
            )

    if tools:
        try:
            tool_schema_chars = len(json.dumps(tools, ensure_ascii=False, default=str))
        except Exception:
            tool_schema_chars = len(str(tools))
        _add_attribution_chars(stats, "tool_schema", tool_schema_chars)

    token_total = sum(
        value for key, value in stats.items()
        if key.endswith("_tokens")
    )
    stats["total_estimated_tokens"] = token_total
    stats["message_count"] = len(messages)
    stats["tool_count"] = len(tools or [])
    return stats


def _merge_context_attribution(aggregate: dict[str, Any], attribution: dict[str, Any]) -> None:
    for key, value in attribution.items():
        if key.endswith("_tokens") or key.endswith("_chars"):
            try:
                aggregate[key] = int(aggregate.get(key) or 0) + int(value or 0)
            except (TypeError, ValueError):
                continue
    aggregate["rounds"] = int(aggregate.get("rounds") or 0) + 1
    aggregate["last_message_count"] = int(attribution.get("message_count") or 0)
    aggregate["last_tool_count"] = int(attribution.get("tool_count") or 0)
    aggregate["last_total_estimated_tokens"] = int(attribution.get("total_estimated_tokens") or 0)


def _message_compaction_preview(message: dict) -> str:
    role = str(message.get("role") or "message")
    if message.get("tool_calls"):
        names = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", tc) if isinstance(tc, dict) else {}
            names.append(str(fn.get("name") or "?"))
        return f"{role}: called tools {', '.join(names)}"
    text = _content_to_text(message.get("content", "")).replace("\n", " ").strip()
    if len(text) > COMPACTED_HISTORY_PREVIEW_CHARS:
        text = text[:COMPACTED_HISTORY_PREVIEW_CHARS] + "..."
    return f"{role}: {text}"


def _compact_non_tool_history(
    *,
    messages: list[dict],
    system_msg: dict | None,
    non_system: list[dict],
    user_msg_idx: int | None,
    estimated_tokens: int,
) -> list[dict]:
    """Structurally summarize stale ordinary chat history.

    Tool-round compaction below handles assistant/tool-call transcripts. This
    path covers long human/assistant chat history with no tool rounds, which
    otherwise used to pass through untouched on the first LLM call.
    """
    if len(non_system) <= RECENT_CHAT_MESSAGES_TO_KEEP + 2:
        return messages

    tail_start = max(0, len(non_system) - RECENT_CHAT_MESSAGES_TO_KEEP)
    keep_first_user = user_msg_idx is not None and user_msg_idx < tail_start
    dropped = [
        message
        for idx, message in enumerate(non_system[:tail_start])
        if not (keep_first_user and idx == user_msg_idx)
    ]
    if not dropped:
        return messages

    summary_lines = [
        _message_compaction_preview(message)
        for message in dropped[:COMPACTED_HISTORY_MAX_ITEMS]
    ]
    omitted = len(dropped) - len(summary_lines)
    if omitted > 0:
        summary_lines.append(f"... {omitted} older messages omitted")

    summary_msg = {
        "role": "user",
        "content": (
            f"[Earlier conversation compacted: {len(dropped)} messages, "
            f"est. {estimated_tokens} tokens before compaction]\n"
            + "\n".join(summary_lines)
        ),
    }

    compacted: list[dict] = []
    if system_msg:
        compacted.append(system_msg)
    if keep_first_user and user_msg_idx is not None:
        compacted.append(non_system[user_msg_idx])
    compacted.append(summary_msg)
    compacted.extend(non_system[tail_start:])

    logger.info(
        "[agentic_loop] Compacted non-tool history: %d msgs -> %d msgs",
        len(messages), len(compacted),
    )
    return compacted


def _context_compaction_token_threshold(
    *,
    output_reserve_tokens: int = RUNTIME_AGENTIC_MAX_TOKENS,
) -> int:
    base_threshold = int(MAX_CONTEXT_TOKENS * LOOP_COMPACT_RATIO)
    try:
        reserve = max(0, int(output_reserve_tokens or 0))
    except (TypeError, ValueError):
        reserve = 0
    return max(1, base_threshold - reserve)


async def _compact_messages(messages: list[dict], model: str | None, temperature: float) -> list[dict]:
    """4-phase context compression when context approaches the limit.

    Phase 1: Truncate large tool results (keep first/last 200 chars)
    Phase 2: Drop tool result content from older rounds (keep tool names only)
    Phase 3: Summarize early rounds into a single user message
    Phase 4: LLM-generated summary of dropped content (if still over budget)

    Always keeps: system prompt, original user message, last 3 complete tool rounds.
    """
    estimated_tokens = _estimate_tokens(messages)
    token_threshold = _context_compaction_token_threshold()
    over_tokens = estimated_tokens >= token_threshold
    over_messages = len(messages) >= MESSAGE_COUNT_COMPACT_THRESHOLD

    if not over_tokens and not over_messages:
        return messages  # No compaction needed
    if over_messages and not over_tokens:
        logger.info(
            "[agentic_loop] Compaction triggered by message count (%d ≥ %d threshold)",
            len(messages), MESSAGE_COUNT_COMPACT_THRESHOLD,
        )

    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    non_system = messages[1:] if system_msg else messages

    # Find the original user message (first user message)
    user_msg = None
    user_msg_idx = None
    for i, m in enumerate(non_system):
        if m.get("role") == "user":
            user_msg = m
            user_msg_idx = i
            break

    # Find tool round start indices
    round_starts = []
    for i, m in enumerate(non_system):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            round_starts.append(i)

    if not round_starts and over_messages:
        return _compact_non_tool_history(
            messages=messages,
            system_msg=system_msg,
            non_system=non_system,
            user_msg_idx=user_msg_idx,
            estimated_tokens=estimated_tokens,
        )

    if len(round_starts) <= 3:
        # Phase 1 only: truncate large tool results in-place
        for m in non_system:
            if m.get("role") == "tool":
                content = str(m.get("content", ""))
                if len(content) > 1000:
                    m["content"] = content[:400] + "\n...[truncated]...\n" + content[-200:]
        logger.info("[agentic_loop] Phase 1 compaction: truncated large tool results")
        return messages

    # Split: keep last 3 tool rounds intact
    keep_from = round_starts[-3]
    early = non_system[:keep_from]
    recent = non_system[keep_from:]

    # Phase 1: Truncate tool results in recent rounds
    for m in recent:
        if m.get("role") == "tool":
            content = str(m.get("content", ""))
            if len(content) > 1500:
                m["content"] = content[:500] + "\n...[truncated]...\n" + content[-300:]

    # Phase 2: Build lightweight summary of early rounds
    # Keep user messages fully, compress tool rounds to tool-name + brief result
    summary_parts = []
    for m in early:
        if m.get("role") == "user":
            summary_parts.append(f"User: {m['content'][:300]}")
        elif m.get("role") == "assistant":
            if m.get("tool_calls"):
                tc_list = m["tool_calls"]
                names = []
                for tc in tc_list:
                    fn = tc.get("function", tc)
                    names.append(fn.get("name", "?"))
                summary_parts.append(f"Called tools: {', '.join(names)}")
            elif m.get("content"):
                summary_parts.append(f"Assistant: {m['content'][:200]}")
        elif m.get("role") == "tool":
            content = str(m.get("content", ""))[:100]
            summary_parts.append(f"  → result: {content}")

    summary_text = runtime_agentic_structural_compaction_message(
        "\n".join(summary_parts[:30])
    )

    compacted = []
    if system_msg:
        compacted.append(system_msg)
    # Preserve original user message if it was in the early section
    if user_msg and user_msg_idx is not None and user_msg_idx < keep_from:
        compacted.append(user_msg)
    compacted.append({"role": "user", "content": summary_text})
    compacted.extend(recent)

    new_tokens = _estimate_tokens(compacted)
    logger.info(
        "[agentic_loop] Compacted context: %d msgs → %d msgs (est. %d → %d tokens)",
        len(messages), len(compacted), estimated_tokens, new_tokens,
    )

    # Phase 3: If still over budget after structural compaction, use LLM to summarize.
    # Force the cheap "worker" model — summarizing 80K-token contexts on the
    # primary (Sonnet/Opus) is the single biggest avoidable cost. Worker tier
    # (Haiku / GPT-4o-mini) handles 3-5 sentence summaries fine.
    if new_tokens > token_threshold:
        try:
            summary_prompt = runtime_agentic_llm_compaction_prompt(summary_text)
            llm_summary, _ = await runtime_execute_agentic_compaction_completion(summary_prompt)
            if llm_summary and llm_summary.strip():
                # Replace the structural summary with the LLM summary
                for i, m in enumerate(compacted):
                    if runtime_agentic_is_structural_compaction_message(
                        m.get("content", "")
                    ):
                        compacted[i] = {
                            "role": "user",
                            "content": runtime_agentic_llm_compaction_replacement(
                                llm_summary
                            ),
                        }
                        break
                logger.info("[agentic_loop] Phase 3: LLM-generated summary applied")
        except Exception:
            logger.warning("[agentic_loop] Phase 3 LLM summary failed", exc_info=True)

    return compacted


def _compact_search_tools_result_for_context(search_result: dict, loaded_tool_names: list[str]) -> str:
    """Keep search_tools schemas out of the next LLM prompt.

    The full schemas are already appended to the ``tools`` parameter for the
    next round. Replaying them again as tool-result text duplicates thousands
    of prompt tokens, especially when search_tools returns 5+ matches.
    """
    matches = search_result.get("matches") or []
    matched_names = [
        str(match.get("name") or "")
        for match in matches
        if isinstance(match, dict) and match.get("name")
    ]
    compact: dict[str, Any] = {
        "query": search_result.get("query"),
        "matched_tools": matched_names[:20],
        "loaded_tools": loaded_tool_names[:20],
    }
    unavailable = search_result.get("unavailable_mcp") or []
    if unavailable:
        compact["unavailable_mcp"] = unavailable[:10]
    mcp_options = search_result.get("mcp_options") or []
    if mcp_options:
        compact["mcp_options"] = [
            {
                "server_key": option.get("server_key"),
                "name": option.get("name"),
                "ready": bool(option.get("ready")),
                "authorization_method": option.get("authorization_method"),
                "execution_mode": option.get("execution_mode"),
                "reason": option.get("reason"),
                "matched_tools": (option.get("matched_tools") or [])[:3],
            }
            for option in mcp_options[:8]
            if isinstance(option, dict)
        ]
    if search_result.get("hint"):
        compact["hint"] = search_result["hint"]
    return json.dumps(compact, ensure_ascii=False)


def _tool_call_dedupe_key(tc: dict) -> str:
    """Stable key for repeated tool calls with identical arguments."""
    name = str(tc.get("name") or "")
    args = tc.get("arguments")
    try:
        args_blob = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        args_blob = str(args)
    digest = hashlib.sha256(args_blob.encode("utf-8")).hexdigest()[:16]
    return f"{name}:{digest}"


def _tool_result_digest(result: str) -> str:
    return hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest()[:16]


def _duplicate_tool_result_notice(tool_name: str, original: str, result_digest: str) -> str:
    preview = original.replace("\n", " ")[:_DUPLICATE_TOOL_RESULT_MAX_PREVIEW]
    return json.dumps(
        {
            "repeated_tool_result": True,
            "tool": tool_name,
            "result_digest": result_digest,
            "message": "Identical to an earlier result for the same tool call arguments; full duplicate omitted from LLM context.",
            "preview": preview,
        },
        ensure_ascii=False,
    )


def _truncate_text_for_context(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = (
        f"\n... (truncated {len(text)} chars, "
        f"sha256={_tool_result_digest(text)}) ...\n"
    )
    remaining = max(0, max_chars - len(marker))
    head = max(0, int(remaining * 0.7))
    tail = max(0, remaining - head)
    return text[:head] + marker + (text[-tail:] if tail else "")


def _compact_json_value_for_context(
    value: Any,
    key: str = "",
    *,
    string_max_chars: int = TOOL_RESULT_JSON_FIELD_MAX_CHARS,
    large_string_max_chars: int | None = None,
) -> Any:
    if isinstance(value, str):
        field_budget = TOOL_RESULT_JSON_FIELD_MAX_CHARS
        if key in {"content", "stdout", "stderr", "text", "body", "html"}:
            field_budget = large_string_max_chars or field_budget
        else:
            field_budget = string_max_chars
        return _truncate_text_for_context(value, field_budget)
    if isinstance(value, list):
        compacted = [
            _compact_json_value_for_context(
                item,
                string_max_chars=string_max_chars,
                large_string_max_chars=large_string_max_chars,
            )
            for item in value[:TOOL_RESULT_JSON_LIST_MAX_ITEMS]
        ]
        omitted = len(value) - len(compacted)
        if omitted > 0:
            compacted.append({"_omitted_items": omitted})
        return compacted
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        items = list(value.items())
        for child_key, child_value in items[:TOOL_RESULT_JSON_OBJECT_MAX_KEYS]:
            compacted[str(child_key)] = _compact_json_value_for_context(
                child_value,
                str(child_key),
                string_max_chars=string_max_chars,
                large_string_max_chars=large_string_max_chars,
            )
        omitted = len(items) - len(compacted)
        if omitted > 0:
            compacted["_omitted_keys"] = omitted
        return compacted
    return value


def _compact_paginated_list_result_for_context(parsed: Any, max_chars: int) -> str | None:
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("documents"), list):
        return None
    if not {"total", "count", "limit", "offset", "has_more"}.issubset(parsed.keys()):
        return None

    compacted: dict[str, Any] = {
        "total": parsed.get("total"),
        "count": parsed.get("count"),
        "limit": parsed.get("limit"),
        "offset": parsed.get("offset"),
        "next_offset": parsed.get("next_offset"),
        "has_more": parsed.get("has_more"),
        "documents": [],
    }
    preferred_key_sets = (
        ("name", "file_type", "file_size"),
        ("name", "file_type"),
        ("name",),
    )
    documents = [doc for doc in (parsed.get("documents") or []) if isinstance(doc, dict)]
    for optional_keys in preferred_key_sets:
        compacted["documents"] = []
        for document in documents:
            compact_doc = {
                key: value
                for key, value in document.items()
                if key in optional_keys and value not in (None, "")
            }
            candidate = {**compacted, "documents": [*compacted["documents"], compact_doc]}
            candidate_str = json.dumps(candidate, ensure_ascii=False, default=str)
            if len(candidate_str) > max_chars:
                break
            compacted["documents"].append(compact_doc)
        if len(compacted["documents"]) >= min(len(documents), int(parsed.get("count") or len(documents))):
            compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
            return compacted_str if len(compacted_str) <= max_chars else None

    compacted["documents"] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        compact_doc = {
            key: document.get(key)
            for key in ("name",)
            if document.get(key) not in (None, "")
        }
        candidate = {**compacted, "documents": [*compacted["documents"], compact_doc]}
        candidate_str = json.dumps(candidate, ensure_ascii=False, default=str)
        if len(candidate_str) > max_chars:
            break
        compacted["documents"].append(compact_doc)

    compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
    return compacted_str if len(compacted_str) <= max_chars else None


_CHROME_RESULT_TOP_KEYS = (
    "ok",
    "status",
    "driver",
    "snapshot_contract",
    "tab",
    "tabId",
    "url",
    "title",
    "page_kind",
    "page_status",
    "status_flags",
    "page_blockers",
    "viewport",
    "refs_count",
    "editable_refs_count",
    "snapshot_required",
    "next_required_tool",
    "target_tab_id",
    "acted_tab_id",
    "action",
    "key",
    "navigation",
    "state_hint",
    "tool",
    "error",
    "reason",
    "missing_parameter",
    "recommended_next_action",
    "candidate_sources",
    "recovery",
)
_CHROME_CANDIDATE_LIST_KEYS = (
    "result_candidates",
    "input_candidates",
    "choice_candidates",
    "upload_candidates",
    "form_candidates",
    "dialog_candidates",
    "submit_candidates",
    "actionable_refs",
    "node_candidates",
    "next_actions",
    "content_links",
    "search_refinement_candidates",
    "search_discovery_candidates",
)
_CHROME_CANDIDATE_KEYS = (
    "rank",
    "kind",
    "candidate_kind",
    "blocker_kind",
    "severity",
    "tool",
    "action",
    "ref",
    "node_id",
    "selector",
    "label",
    "title",
    "text",
    "headline",
    "author",
    "publisher",
    "source",
    "published_time",
    "relative_time",
    "snippet",
    "evidence_text",
    "role",
    "tag",
    "type",
    "name",
    "value",
    "message",
    "required",
    "read_only",
    "autocomplete",
    "placeholder",
    "input_mode",
    "min",
    "max",
    "step",
    "pattern",
    "min_length",
    "max_length",
    "description",
    "valid",
    "validation_message",
    "validity_flags",
    "interaction",
    "submitted_form",
    "default_prevented",
    "href",
    "target",
    "target_attribute",
    "url",
    "context",
    "reason",
    "enter_recovery",
    "recommended_next_action",
    "submit_candidate_ref",
    "submit_candidate_node_id",
    "submit_candidate_label",
    "submit_candidate_selector",
    "form",
    "form_selector",
    "form_label",
    "supported",
    "disabled",
    "in_viewport",
    "invalid_fields_count",
    "invalid_fields",
    "required_fields_count",
    "completed_required_fields_count",
    "missing_required_fields_count",
    "missing_required_fields",
    "submit_ready",
    "form_progress",
    "bounds",
)
_CHROME_FORM_NESTED_KEYS = (
    "fields",
    "uploads",
    "upload_targets",
    "submit_candidates",
    "option_candidates",
)


def _looks_like_chrome_browser_result(tool_name: str, parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    if tool_name.startswith("mcp__chrome__") or tool_name.startswith("browser_"):
        return True
    return (
        parsed.get("driver") == "chrome-extension"
        or parsed.get("snapshot_contract") == "codex_style_dom_snapshot_v1"
        or parsed.get("status") in {"snapshot", "visible_dom", "matched", "timeout", "clicked", "filled", "uploaded"}
    )


def _compact_chrome_browser_result_for_context(
    tool_name: str,
    parsed: Any,
    *,
    max_chars: int,
    digest: str,
) -> str | None:
    if not _looks_like_chrome_browser_result(tool_name, parsed):
        return None

    attempts = (
        {"text": 700, "summary": 700, "block_count": 8, "candidate_count": 8},
        {"text": 420, "summary": 420, "block_count": 5, "candidate_count": 5},
        {"text": 220, "summary": 260, "block_count": 2, "candidate_count": 2},
        {"text": 80, "summary": 120, "block_count": 1, "candidate_count": 1},
        {"text": 0, "summary": 0, "block_count": 0, "candidate_count": 1},
    )
    for attempt in attempts:
        compacted = _build_compact_chrome_browser_result(
            tool_name,
            parsed,
            digest=digest,
            text_chars=int(attempt["text"]),
            summary_chars=int(attempt["summary"]),
            block_count=int(attempt["block_count"]),
            candidate_count=int(attempt["candidate_count"]),
        )
        compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
        if len(compacted_str) <= max_chars:
            return compacted_str
    compacted = _build_minimal_chrome_action_result(tool_name, parsed, digest=digest)
    if compacted is not None:
        compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
        if len(compacted_str) <= max_chars:
            return compacted_str
    compacted = _build_minimal_chrome_blocker_result(tool_name, parsed, digest=digest)
    if compacted is not None:
        compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
        if len(compacted_str) <= max_chars:
            return compacted_str
    compacted = _build_minimal_chrome_browser_result(tool_name, parsed, digest=digest)
    compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
    if len(compacted_str) <= max_chars:
        return compacted_str
    compacted = _build_ultra_minimal_chrome_action_result(tool_name, parsed, digest=digest)
    if compacted is not None:
        compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
        if len(compacted_str) <= max_chars:
            return compacted_str
    return None


def _build_compact_chrome_browser_result(
    tool_name: str,
    parsed: dict[str, Any],
    *,
    digest: str,
    text_chars: int,
    summary_chars: int,
    block_count: int,
    candidate_count: int,
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in _CHROME_RESULT_TOP_KEYS:
        if key in parsed:
            compacted[key] = _compact_chrome_value(parsed.get(key), string_chars=240, nested_count=4)

    for key in ("dom_snapshot", "visible_text", "page_text", "page_text_sample"):
        value = parsed.get(key)
        if isinstance(value, str) and text_chars > 0:
            compacted[key] = _truncate_text_for_context(value, text_chars)
        elif key in parsed and key in {"visible_text", "page_text_sample"}:
            compacted[key] = ""

    summary = parsed.get("content_summary")
    if isinstance(summary, dict):
        compacted["content_summary"] = _compact_chrome_content_summary(
            summary,
            string_chars=summary_chars,
            block_count=block_count,
        )

    if isinstance(parsed.get("visible_text_blocks"), list) and block_count > 0:
        compacted["visible_text_blocks"] = [
            _compact_chrome_candidate(block, string_chars=180, nested_count=2)
            for block in parsed.get("visible_text_blocks", [])[:block_count]
            if isinstance(block, dict)
        ]

    for key in _CHROME_CANDIDATE_LIST_KEYS:
        value = parsed.get(key)
        if isinstance(value, list):
            compacted[key] = [
                _compact_chrome_candidate(item, string_chars=220, nested_count=4)
                for item in value[:candidate_count]
                if isinstance(item, dict)
            ]

    if "action_policy" in parsed:
        compacted["action_policy"] = _compact_chrome_value(parsed.get("action_policy"), string_chars=260, nested_count=2)

    compacted["_tool_result_truncated"] = {
        "tool": tool_name,
        "original_chars": len(json.dumps(parsed, ensure_ascii=False, default=str)),
        "sha256": digest,
        "strategy": "chrome_browser_context",
    }
    return compacted


def _build_minimal_chrome_browser_result(
    tool_name: str,
    parsed: dict[str, Any],
    *,
    digest: str,
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in (
        "ok",
        "status",
        "driver",
        "tabId",
        "url",
        "title",
        "page_kind",
        "page_status",
        "status_flags",
        "page_blockers",
        "next_required_tool",
        "target_tab_id",
        "acted_tab_id",
        "tool",
        "reason",
        "missing_parameter",
        "recommended_next_action",
        "candidate_sources",
    ):
        if key in parsed:
            compacted[key] = _compact_chrome_value(
                parsed.get(key),
                string_chars=120,
                nested_count=6 if key in {"candidate_sources", "page_blockers"} else 1,
            )

    state_hint = parsed.get("state_hint")
    if isinstance(state_hint, dict):
        compacted["state_hint"] = _compact_chrome_action_semantics(state_hint)

    for key in (
        "result_candidates",
        "search_refinement_candidates",
        "search_discovery_candidates",
        "input_candidates",
        "upload_candidates",
        "form_candidates",
        "dialog_candidates",
        "submit_candidates",
        "node_candidates",
        "next_actions",
    ):
        value = parsed.get(key)
        if isinstance(value, list):
            compacted[key] = [
                _compact_minimal_chrome_candidate_for_list(key, item)
                for item in value[:1]
                if isinstance(item, dict)
            ]

    compacted["_tool_result_truncated"] = {
        "tool": tool_name,
        "original_chars": len(json.dumps(parsed, ensure_ascii=False, default=str)),
        "sha256": digest,
        "strategy": "chrome_browser_minimal_context",
    }
    return compacted


def _compact_minimal_chrome_candidate_for_list(list_key: str, candidate: dict[str, Any]) -> dict[str, Any]:
    if list_key == "result_candidates":
        return _compact_chrome_candidate_with_keys(
            candidate,
            (
                "node_id",
                "candidate_kind",
                "title",
                "source",
                "published_time",
                "relative_time",
                "snippet",
                "evidence_text",
                "href",
            ),
            string_chars=90,
        )
    if list_key in {"search_refinement_candidates", "search_discovery_candidates"}:
        return _compact_chrome_candidate_with_keys(
            candidate,
            ("node_id", "candidate_kind", "title", "href"),
            string_chars=90,
        )
    if list_key == "input_candidates":
        return _compact_chrome_candidate_with_keys(
            candidate,
            (
                "node_id",
                "label",
                "role",
                "name",
                "value",
                "required",
                "pattern",
                "max_length",
                "description",
                "valid",
                "validation_message",
                "validity_flags",
            ),
            string_chars=120,
        )
    if list_key == "form_candidates":
        return _compact_minimal_chrome_form_candidate(candidate)
    if list_key == "dialog_candidates":
        return _compact_minimal_chrome_dialog_candidate(candidate)
    if list_key == "upload_candidates":
        return _compact_chrome_candidate_with_keys(candidate, ("node_id", "selector", "label", "supported"), string_chars=80)
    if list_key == "submit_candidates":
        return _compact_chrome_candidate_with_keys(candidate, ("node_id", "label", "disabled"), string_chars=80)
    if list_key == "node_candidates":
        return _compact_chrome_candidate_with_keys(candidate, ("kind", "node_id", "label"), string_chars=80)
    if list_key == "next_actions":
        return _compact_chrome_candidate_with_keys(
            candidate,
            (
                "rank",
                "tool",
                "action",
                "candidate_kind",
                "blocker_kind",
                "node_id",
                "selector",
                "label",
                "href",
                "url",
                "submit_ready",
                "missing_required_fields",
                "missing_required_fields_count",
                "reason",
                "recommended_next_action",
            ),
            string_chars=80,
        )
    return _compact_chrome_candidate(candidate, string_chars=90, nested_count=1)


def _compact_minimal_chrome_form_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    compacted = _compact_chrome_candidate_with_keys(
        candidate,
        (
            "selector",
            "invalid_fields_count",
            "required_fields_count",
            "completed_required_fields_count",
            "missing_required_fields_count",
            "submit_ready",
        ),
        string_chars=90,
    )
    invalid_fields = candidate.get("invalid_fields")
    if isinstance(invalid_fields, list):
        compacted["invalid_fields"] = [
            _compact_chrome_candidate_with_keys(
                item,
                ("kind", "label", "node_id", "name", "validation_message", "validity_flags"),
                string_chars=120,
            )
            for item in invalid_fields[:1]
            if isinstance(item, dict)
        ]
    missing_required_fields = candidate.get("missing_required_fields")
    if isinstance(missing_required_fields, list):
        compacted["missing_required_fields"] = [
            _compact_chrome_candidate_with_keys(item, ("kind", "label", "selector", "node_id", "name"), string_chars=90)
            for item in missing_required_fields[:1]
            if isinstance(item, dict)
        ]
    form_progress = candidate.get("form_progress")
    if isinstance(form_progress, dict):
        compacted["form_progress"] = _compact_chrome_candidate_with_keys(
            form_progress,
            ("required", "completed", "missing", "submit_ready"),
            string_chars=40,
        )
    fields = candidate.get("fields")
    if isinstance(fields, list):
        compacted["fields"] = [
            _compact_chrome_candidate_with_keys(
                item,
                (
                    "node_id",
                    "label",
                    "name",
                    "value",
                    "required",
                    "min_length",
                    "max_length",
                    "description",
                    "valid",
                    "validation_message",
                    "validity_flags",
                ),
                string_chars=120,
            )
            for item in fields[:1]
            if isinstance(item, dict)
        ]
    return compacted


def _compact_minimal_chrome_dialog_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    compacted = _compact_chrome_candidate_with_keys(
        candidate,
        (
            "selector",
            "label",
            "role",
            "modal",
            "field_refs",
            "submit_refs",
            "reason",
        ),
        string_chars=100,
    )
    upload_targets = candidate.get("upload_targets")
    if isinstance(upload_targets, list):
        compacted["upload_targets"] = [
            _compact_chrome_candidate_with_keys(item, ("selector", "node_id", "label", "supported", "required"), string_chars=100)
            for item in upload_targets[:2]
            if isinstance(item, dict)
        ]
    next_actions = candidate.get("next_actions")
    if isinstance(next_actions, list):
        compacted["next_actions"] = [
            _compact_chrome_candidate_with_keys(
                item,
                ("rank", "tool", "action", "candidate_kind", "node_id", "selector", "label", "reason"),
                string_chars=100,
            )
            for item in next_actions[:3]
            if isinstance(item, dict)
        ]
    form_candidates = candidate.get("form_candidates")
    if isinstance(form_candidates, list):
        compacted["form_candidates"] = [
            _compact_minimal_chrome_form_candidate(item)
            for item in form_candidates[:1]
            if isinstance(item, dict)
        ]
    return compacted


def _compact_chrome_candidate_with_keys(
    candidate: dict[str, Any],
    keys: tuple[str, ...],
    *,
    string_chars: int,
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in keys:
        if key in candidate:
            compacted[key] = _compact_chrome_value(candidate.get(key), string_chars=string_chars, nested_count=2)
    return compacted


def _compact_chrome_content_summary(
    summary: dict[str, Any],
    *,
    string_chars: int,
    block_count: int,
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in ("title", "description", "canonical_url", "published_time", "byline"):
        if key in summary:
            compacted[key] = _compact_chrome_value(summary.get(key), string_chars=240, nested_count=2)
    if isinstance(summary.get("main_content_text"), str) and string_chars > 0:
        compacted["main_content_text"] = _truncate_text_for_context(str(summary.get("main_content_text")), string_chars)
    for key in ("main_content_blocks", "metadata_blocks"):
        value = summary.get(key)
        if isinstance(value, list) and block_count > 0:
            compacted[key] = [
                _compact_chrome_candidate(item, string_chars=180, nested_count=2)
                for item in value[:block_count]
                if isinstance(item, dict)
            ]
    structured_data = summary.get("structured_data")
    if isinstance(structured_data, list):
        compacted["structured_data"] = [
            _compact_chrome_candidate(item, string_chars=180, nested_count=2)
            for item in structured_data[:2]
            if isinstance(item, dict)
        ]
    return compacted


def _compact_chrome_candidate(
    candidate: dict[str, Any],
    *,
    string_chars: int,
    nested_count: int,
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in _CHROME_CANDIDATE_KEYS:
        if key in candidate:
            compacted[key] = _compact_chrome_value(candidate.get(key), string_chars=string_chars, nested_count=nested_count)
    for key in _CHROME_FORM_NESTED_KEYS:
        value = candidate.get(key)
        if isinstance(value, list) and nested_count > 0:
            compacted[key] = [
                _compact_chrome_candidate(item, string_chars=max(80, string_chars // 2), nested_count=1)
                for item in value[:nested_count]
                if isinstance(item, dict)
            ]
    return compacted


def _build_minimal_chrome_blocker_result(
    tool_name: str,
    parsed: dict[str, Any],
    *,
    digest: str,
) -> dict[str, Any] | None:
    page_blockers = parsed.get("page_blockers")
    page_status = parsed.get("page_status")
    if page_status != "blocked" and not isinstance(page_blockers, list):
        return None

    compacted: dict[str, Any] = {}
    for key in (
        "ok",
        "status",
        "tabId",
        "url",
        "title",
        "page_status",
        "status_flags",
    ):
        if key in parsed:
            compacted[key] = _compact_chrome_value(parsed.get(key), string_chars=80, nested_count=2)

    if isinstance(page_blockers, list):
        compacted["page_blockers"] = [
            _compact_chrome_candidate_with_keys(
                blocker,
                ("kind", "severity", "message", "recommended_next_action"),
                string_chars=70,
            )
            for blocker in page_blockers[:1]
            if isinstance(blocker, dict)
        ]

    next_actions = parsed.get("next_actions")
    if isinstance(next_actions, list):
        compacted["next_actions"] = [
            _compact_chrome_candidate_with_keys(
                action,
                ("rank", "action", "tool", "blocker_kind", "recommended_next_action"),
                string_chars=70,
            )
            for action in next_actions[:1]
            if isinstance(action, dict)
        ]

    compacted["_tool_result_truncated"] = {
        "tool": tool_name,
        "original_chars": len(json.dumps(parsed, ensure_ascii=False, default=str)),
        "sha256": digest,
        "strategy": "chrome_browser_blocker_context",
    }
    return compacted


def _build_minimal_chrome_action_result(
    tool_name: str,
    parsed: dict[str, Any],
    *,
    digest: str,
) -> dict[str, Any] | None:
    source_key = "key" if isinstance(parsed.get("key"), dict) else "action" if isinstance(parsed.get("action"), dict) else None
    state_hint = parsed.get("state_hint") if isinstance(parsed.get("state_hint"), dict) else None
    if source_key is None and state_hint is None:
        return None

    compacted: dict[str, Any] = {}
    for key in (
        "ok",
        "status",
        "tabId",
        "target_tab_id",
        "acted_tab_id",
        "next_required_tool",
        "tool",
        "reason",
        "missing_parameter",
        "recommended_next_action",
        "candidate_sources",
        "recovery",
    ):
        if key in parsed:
            compacted[key] = _compact_chrome_value(parsed.get(key), string_chars=160, nested_count=6)

    if source_key is not None:
        compacted[source_key] = _compact_chrome_action_semantics(parsed[source_key])
    if state_hint is not None:
        compacted["state_hint"] = _compact_chrome_action_semantics(state_hint, include_enter_recovery=source_key is None)
    compacted["_tool_result_truncated"] = {
        "tool": tool_name,
        "original_chars": len(json.dumps(parsed, ensure_ascii=False, default=str)),
        "sha256": digest,
        "strategy": "chrome_action_context",
    }
    return compacted


def _build_ultra_minimal_chrome_action_result(
    tool_name: str,
    parsed: dict[str, Any],
    *,
    digest: str,
) -> dict[str, Any] | None:
    state_hint = parsed.get("state_hint") if isinstance(parsed.get("state_hint"), dict) else None
    has_recovery = any(
        key in parsed
        for key in (
            "reason",
            "missing_parameter",
            "recommended_next_action",
            "candidate_sources",
        )
    )
    if state_hint is None and not has_recovery:
        return None

    compacted: dict[str, Any] = {}
    for key in (
        "ok",
        "tool",
        "reason",
        "missing_parameter",
        "recommended_next_action",
        "candidate_sources",
        "next_required_tool",
    ):
        if key in parsed:
            compacted[key] = _compact_chrome_value(parsed.get(key), string_chars=80, nested_count=6 if key == "candidate_sources" else 1)
    if state_hint is not None and "tool" not in compacted and "action" in state_hint:
        compacted["tool"] = _compact_chrome_value(state_hint.get("action"), string_chars=80, nested_count=1)
    compacted["_tool_result_truncated"] = {
        "tool": tool_name,
        "original_chars": len(json.dumps(parsed, ensure_ascii=False, default=str)),
        "sha256": digest,
        "strategy": "chrome_action_ultra_minimal_context",
    }
    return compacted


def _compact_chrome_action_semantics(
    value: dict[str, Any],
    *,
    include_enter_recovery: bool = True,
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in (
        "ok",
        "key",
        "action",
        "target",
        "ref",
        "node_id",
        "selector",
        "label",
        "role",
        "interaction",
        "submitted_form",
        "default_prevented",
        "href",
        "target",
        "target_attribute",
        "form_selector",
        "form_label",
        "reason",
        "missing_parameter",
        "recommended_next_action",
        "candidate_sources",
        "wait_tool",
        "wait_state",
        "after_wait",
        "wait_reason",
        "recovery",
        "next",
    ):
        if key in value:
            compacted[key] = _compact_chrome_value(
                value.get(key),
                string_chars=160 if key != "wait_reason" else 120,
                nested_count=6 if key == "candidate_sources" else 1,
            )
    recovery = value.get("enter_recovery")
    if include_enter_recovery and isinstance(recovery, dict):
        compacted["enter_recovery"] = _compact_chrome_enter_recovery(recovery)
    return compacted


def _compact_chrome_enter_recovery(recovery: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in (
    "recommended_next_action",
    "blocker_kind",
    "submit_candidate_ref",
        "submit_candidate_node_id",
        "submit_candidate_label",
        "submit_candidate_selector",
        "form_selector",
        "form_label",
        "reason",
    ):
        if key in recovery:
            compacted[key] = _compact_chrome_value(recovery.get(key), string_chars=80 if key == "reason" else 140, nested_count=1)
    return compacted


def _compact_chrome_value(value: Any, *, string_chars: int, nested_count: int) -> Any:
    if isinstance(value, str):
        return _truncate_text_for_context(value, string_chars)
    if isinstance(value, list):
        return [
            _compact_chrome_value(item, string_chars=string_chars, nested_count=max(0, nested_count - 1))
            for item in value[:nested_count]
        ]
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, child in list(value.items())[:max(1, nested_count * 3)]:
            compacted[str(key)] = _compact_chrome_value(child, string_chars=string_chars, nested_count=max(0, nested_count - 1))
        return compacted
    return value


def _compact_tool_result_for_context(
    tool_name: str,
    result_str: str,
    max_chars: int = TOOL_RESULT_MAX_CHARS,
) -> str:
    """Compact oversized tool results without corrupting JSON payloads."""
    if len(result_str) <= max_chars:
        return result_str

    digest = _tool_result_digest(result_str)
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return _truncate_text_for_context(result_str, max_chars)

    chrome_result = _compact_chrome_browser_result_for_context(
        tool_name,
        parsed,
        max_chars=max_chars,
        digest=digest,
    )
    if chrome_result is not None:
        return chrome_result

    paginated_list = _compact_paginated_list_result_for_context(parsed, max_chars)
    if paginated_list is not None:
        return paginated_list

    compacted = _compact_json_value_for_context(
        parsed,
        string_max_chars=max(240, int(max_chars * 0.18)),
        large_string_max_chars=max(320, int(max_chars * 0.42)),
    )
    if isinstance(compacted, dict):
        compacted["_tool_result_truncated"] = {
            "tool": tool_name,
            "original_chars": len(result_str),
            "sha256": digest,
        }
    else:
        compacted = {
            "tool_result": compacted,
            "_tool_result_truncated": {
                "tool": tool_name,
                "original_chars": len(result_str),
                "sha256": digest,
            },
        }

    compacted_str = json.dumps(compacted, ensure_ascii=False, default=str)
    if len(compacted_str) <= max_chars:
        return compacted_str

    fallback = {
        "_tool_result_truncated": {
            "tool": tool_name,
            "original_chars": len(result_str),
            "sha256": digest,
            "reason": "Result stayed large after JSON field compaction.",
        },
        "preview": _truncate_text_for_context(result_str, max(200, max_chars - 260)),
    }
    return json.dumps(fallback, ensure_ascii=False, default=str)


async def agentic_loop(
    *,
    system_prompt: str,
    user_message: "str | list[dict]",
    tools: List[Dict[str, Any]],
    tool_executor: ToolExecutor,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    initial_messages: Optional[List[Dict[str, Any]]] = None,
    on_tool_start: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    on_tool_end: Optional[Callable[..., Any]] = None,
    on_llm_call: Optional[Callable] = None,
    stream_handler: Optional[Callable] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tool_schema_resolver: Optional[Callable[[str], Optional[dict]]] = None,
    forced_tool_calls: Optional[List[Dict[str, Any]]] = None,
    terminal_tool_result_policy: Optional[Dict[str, Any]] = None,
) -> AgenticResult:
    """
    Run an agentic loop: call the LLM with tools, execute any requested tools,
    feed results back, and repeat until the LLM returns a final text response
    (no more tool calls) or max_rounds is reached.

    Parameters
    ----------
    system_prompt : str
        System prompt that defines the agent's role and capabilities.
    user_message : str
        The user's request / task description.
    tools : list
        OpenAI function-calling tool schemas available to the LLM.
    tool_executor : async callable (name, args) -> str
        Callback that executes a tool by name with the given arguments dict
        and returns a string result. The caller wires this to their own tool
        dispatch (subsystem registry, file I/O, web search, etc.).
    model : str, optional
        LLM model override. Defaults to env config.
    temperature : float
        LLM temperature. Default 0.7.
    max_rounds : int
        Safety cap on LLM call rounds. Default 100.
    initial_messages : list, optional
        Prior conversation messages to prepend (for context continuity).
    on_tool_start : callable, optional
        Hook called when a tool is about to execute: (tool_name, args).
    on_tool_end : callable, optional
        Hook called when a tool finishes: (tool_name, result_str).
    on_llm_call : callable, optional
        Hook called after each LLM API call with per-round telemetry:
        (round_num, duration_ms, usage, tool_calls_requested, finish_reason).
    stream_handler : callable, optional
        Async callback for per-token streaming from LLM. Passed through to
        Runtime-owned agentic LLM helpers.
    tool_schema_resolver : callable, optional
        Internal lazy-loader used after ``search_tools`` returns tool names.
        This keeps full schemas out of the tool result while still making
        discovered tools callable in the next LLM round.

    Returns
    -------
    AgenticResult
        Final text, full message history, token usage, round count.
    """
    if metadata and model:
        metadata = dict(metadata)
        metadata.setdefault("_resolved_model", model)

    # Build initial message list
    messages: List[Dict[str, Any]] = []
    messages.append({"role": "system", "content": system_prompt})
    if initial_messages:
        messages.extend(initial_messages)
    messages.append({"role": "user", "content": user_message})

    total_usage: Dict[str, Any] = dict(EMPTY_USAGE)
    tool_calls_made: List[str] = []
    rounds = 0
    consecutive_truncations = 0
    consecutive_tool_errors = 0
    consecutive_empty_llm_responses = 0
    seen_tool_result_digests: dict[str, str] = {}
    seen_auto_tool_calls: set[str] = set()
    MAX_CONSECUTIVE_TOOL_ERRORS = 10

    try:
        mark_byok_from_metadata(metadata)
        await _preflight_credit_check()
    except CreditExhaustedError as exc:
        logger.warning("Credits exhausted before agentic loop start: %s", exc)
        return _credit_exhausted_result(
            exc,
            messages=messages,
            usage=total_usage,
            rounds=rounds,
            tool_calls_made=tool_calls_made,
        )

    while rounds < max_rounds:
        rounds += 1

        if forced_tool_calls:
            assistant_tool_calls = []
            forced_results = []
            for idx, forced in enumerate(forced_tool_calls):
                name = str(forced.get("name") or "").strip()
                args = forced.get("arguments") or forced.get("args") or {}
                if not name:
                    continue
                if not isinstance(args, dict):
                    args = {}
                tool_call = {
                    "id": f"auto_{rounds}_{idx}_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:10]}",
                    "name": name,
                    "arguments": args,
                }
                assistant_tool_calls.append({
                    "id": tool_call["id"],
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                preface = (
                    str(forced.get("preface") or "").strip()
                    or _forced_tool_call_preface(name, args, user_message)
                )
                if preface:
                    try:
                        await _emit_stream_event(
                            stream_handler,
                            "process_note",
                            {"content": preface, "synthetic": True},
                        )
                    except Exception:
                        pass
                if on_tool_start:
                    try:
                        on_tool_start(name, args)
                    except Exception:
                        pass
                t0 = time.perf_counter()
                try:
                    result = await tool_executor(name, args)
                except Exception as exc:
                    result = f"Tool error ({name}): {exc}"
                    logger.warning("[agentic_loop] Auto-followup tool %s raised: %s", name, exc)
                duration_ms = (time.perf_counter() - t0) * 1000
                if on_tool_end:
                    try:
                        try:
                            on_tool_end(name, str(result), duration_ms, args)
                        except TypeError:
                            on_tool_end(name, str(result))
                    except Exception:
                        pass
                tool_calls_made.append(name)
                forced_results.append((tool_call, str(result) if result is not None else ""))
            forced_tool_calls = None
            if assistant_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": assistant_tool_calls,
                })
                pending_auto_tool_calls: list[dict[str, Any]] = []
                loaded_tool_names = {t.get("function", {}).get("name") for t in tools}
                for tool_call, result_str in forced_results:
                    if result_str.lstrip().startswith("{"):
                        try:
                            tool_result = json.loads(result_str.lstrip())
                            if isinstance(tool_result, dict):
                                pending_auto_tool_calls.extend(_auto_tool_calls_from_result(tool_result, loaded_tool_names))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    result_str = _compact_tool_result_for_context(
                        tool_call["name"],
                        result_str,
                        TOOL_RESULT_MAX_CHARS,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result_str,
                    })
                terminal_control = (
                    _detect_forced_media_generation_result(forced_results, user_message)
                    or _detect_terminal_tool_result(forced_results, terminal_tool_result_policy, user_message)
                    or _detect_stop_parent_tool_result(forced_results, user_message)
                )
                if terminal_control:
                    final_content = str(terminal_control.get("content") or "")
                    messages.append({
                        "role": "assistant",
                        "content": final_content,
                    })
                    logger.info(
                        "[agentic_loop] Stopping after terminal tool result from forced tools: %s",
                        terminal_control.get("stop_reason"),
                    )
                    return AgenticResult(
                        content=final_content,
                        messages=messages,
                        usage=total_usage,
                        rounds=rounds,
                        tool_calls_made=tool_calls_made,
                        stop_reason=str(terminal_control.get("stop_reason") or SKILL_TERMINAL_STOP_REASON),
                        control=terminal_control,
                    )
                auto_calls = _dedupe_auto_tool_calls(pending_auto_tool_calls, seen=seen_auto_tool_calls)
                if auto_calls:
                    forced_tool_calls = auto_calls
                continue

        # Compact before the LLM call as well as after tool results. This
        # protects the first round when initial_messages already contains a
        # long chat transcript.
        messages = await _compact_messages(messages, model, temperature)

        # -- Call LLM with tools --
        _llm_start = time.time()
        try:
            llm_messages = messages
            if tool_calls_made and messages and messages[0].get("role") == "system":
                llm_messages = [dict(messages[0]), *messages[1:]]
                llm_messages[0]["content"] = _with_final_response_sentinel_guidance(
                    str(llm_messages[0].get("content") or "")
                )
            if tools:
                content, tool_calls, usage = await runtime_execute_agentic_round_tool_completion(
                    llm_messages,
                    tools,
                    temperature=temperature,
                    model=model,
                    stream_handler=stream_handler,
                    metadata=metadata,
                )
            else:
                content, usage = await runtime_execute_agentic_round_text_completion(
                    llm_messages,
                    temperature=temperature,
                    model=model,
                    stream_handler=stream_handler,
                    metadata=metadata,
                )
                tool_calls = None
        except CreditExhaustedError as exc:
            # Credits exhausted mid-loop — return partial result instead
            # of crashing. The caller (chat, worker, planner) sees the
            # error in result.error and can surface it to the user.
            logger.warning("Credits exhausted during agentic loop round %d: %s", rounds, exc)
            return _credit_exhausted_result(
                exc,
                messages=messages,
                usage=total_usage,
                rounds=rounds,
                tool_calls_made=tool_calls_made,
            )
        _llm_duration_ms = (time.time() - _llm_start) * 1000
        usage = dict(usage or EMPTY_USAGE)
        usage["context_attribution"] = _estimate_context_attribution(messages, tools)
        reasoning_content = _pop_reasoning_content(usage)

        _add_usage(total_usage, usage)

        # Report per-round telemetry
        if on_llm_call:
            try:
                _tc_names = [tc["name"] for tc in tool_calls] if tool_calls else []
                _finish = "tool_calls" if tool_calls else usage.get("finish_reason", "stop")
                on_llm_call(rounds, _llm_duration_ms, usage, _tc_names, _finish)
            except Exception:
                pass

        # -- Truncation detection: finish_reason='length' with tool_calls --
        # When max_tokens truncates a tool call, the arguments JSON is incomplete
        # and the tool will fail. Detect this and circuit-break.
        finish_reason = usage.get("finish_reason", "")
        usage_error = str(usage.get("error") or "").strip()
        if usage_error and not tool_calls:
            logger.warning(
                "[agentic_loop] Round %d: LLM call failed before tool execution: %s",
                rounds, usage_error[:300],
            )
            return _llm_call_failed_result(
                usage_error,
                messages=messages,
                usage=total_usage,
                rounds=rounds,
                tool_calls_made=tool_calls_made,
            )

        if _is_empty_llm_response(content, tool_calls, usage):
            consecutive_empty_llm_responses += 1
            logger.warning(
                "[agentic_loop] Round %d: provider returned an empty response "
                "(no content/tool_calls/usage). Consecutive: %d/%d",
                rounds, consecutive_empty_llm_responses, MAX_EMPTY_LLM_RESPONSES,
            )
            if consecutive_empty_llm_responses >= MAX_EMPTY_LLM_RESPONSES:
                return AgenticResult(
                    content=(
                        "Sorry, the model returned an empty response. Please try again; "
                        "if this repeats, switch the selected model or reset your API key."
                    ),
                    messages=messages,
                    usage=total_usage,
                    rounds=rounds,
                    tool_calls_made=tool_calls_made,
                    stop_reason="error",
                    error="empty_llm_response",
                )
            messages.append({
                "role": "user",
                "content": runtime_agentic_empty_response_retry_message(
                    has_tools=bool(tools),
                ),
            })
            continue
        else:
            consecutive_empty_llm_responses = 0

        if tool_calls and finish_reason == "length":
            consecutive_truncations += 1
            logger.warning(
                "[agentic_loop] Round %d: tool call truncated (finish_reason=length). "
                "Consecutive: %d/%d",
                rounds, consecutive_truncations, MAX_CONSECUTIVE_TRUNCATIONS,
            )
            if consecutive_truncations >= MAX_CONSECUTIVE_TRUNCATIONS:
                logger.error(
                    "[agentic_loop] Circuit breaker: %d consecutive truncations. "
                    "Aborting loop. Consider using edit_file instead of write_file for large files.",
                    consecutive_truncations,
                )
                return AgenticResult(
                    content=content or "[Error: Tool calls repeatedly truncated due to output size limit. Use edit_file for targeted changes instead of rewriting entire files.]",
                    messages=messages,
                    usage=total_usage,
                    rounds=rounds,
                    tool_calls_made=tool_calls_made,
                    stop_reason="error",
                    error="consecutive_truncations",
                )
            # Skip executing the truncated tool calls — ask LLM to retry with smaller output
            assistant_retry_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content or "",
            }
            _attach_reasoning_content(assistant_retry_msg, reasoning_content)
            messages.append(assistant_retry_msg)
            messages.append({
                "role": "user",
                "content": runtime_agentic_truncated_tool_call_retry_message(),
            })
            continue
        else:
            consecutive_truncations = 0

        # -- No tool calls -> LLM is done --
        if not tool_calls:
            # Drop the final-response marker before it reaches persisted
            # task/plan output (the model is told to emit it; we own removing it).
            content = _strip_final_response_sentinel(content)
            if (
                stream_handler is not None
                and tool_calls_made
                and not usage.get("_final_response_start_emitted")
            ):
                try:
                    await _emit_stream_event(
                        stream_handler,
                        "final_response_start",
                        {"round": rounds},
                    )
                except Exception:
                    pass
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": content or "",
            }
            _attach_reasoning_content(assistant_msg, reasoning_content)
            messages.append(assistant_msg)
            logger.info(
                "[agentic_loop] Completed in %d round(s), %d tool call(s) total",
                rounds, len(tool_calls_made),
            )
            return AgenticResult(
                content=content or "",
                messages=messages,
                usage=total_usage,
                rounds=rounds,
                tool_calls_made=tool_calls_made,
                stop_reason="completed",
            )

        # -- Has tool calls -> execute and loop --
        # 1. Append assistant message with tool_calls (OpenAI format).
        # OpenAI tolerates content=null when tool_calls is present, but
        # several OpenRouter providers (Novita, some Anthropic routes)
        # return HTTP 400 on null. Use empty string for cross-provider
        # compatibility.
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"],
                    },
                }
                for tc in tool_calls
            ],
        }
        _attach_reasoning_content(assistant_msg, reasoning_content)
        messages.append(assistant_msg)

        try:
            mark_byok_from_metadata(metadata)
            await _preflight_credit_check()
        except CreditExhaustedError as exc:
            logger.warning(
                "Credits exhausted after agentic loop round %d LLM call; skipping tool execution: %s",
                rounds,
                exc,
            )
            return _credit_exhausted_result(
                exc,
                messages=messages,
                usage=total_usage,
                rounds=rounds,
                tool_calls_made=tool_calls_made,
            )

        # Log tool names + abbreviated args so operators can see what's happening
        tool_summaries = []
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
            # Show the most useful arg preview per tool type
            if name == "bash":
                preview = (args.get("command") or "")[:120]
                tool_summaries.append(f"bash({preview!r})")
            elif name in ("write_file", "read_file", "edit_file"):
                preview = (args.get("path") or args.get("file_path") or "")[:80]
                tool_summaries.append(f"{name}({preview!r})")
            elif name == "web_search":
                preview = (args.get("query") or "")[:80]
                tool_summaries.append(f"web_search({preview!r})")
            elif name == "web_fetch":
                preview = (args.get("url") or "")[:80]
                tool_summaries.append(f"web_fetch({preview!r})")
            else:
                tool_summaries.append(name)
        logger.info(
            "[agentic_loop] Round %d: %d tool call(s): [%s]",
            rounds, len(tool_calls), ", ".join(tool_summaries),
        )

        # 2. Execute all tool calls concurrently. We time every tool so
        # the chat log records real wall-clock — without this, bash and
        # invoke_skill all show ``duration_ms=0`` and operators can't tell
        # whether a slow turn is the LLM thinking or the tool itself.
        async def _exec_one(tc_item: dict) -> tuple[dict, str, float]:
            """Execute a single tool call, returning (tc, result_str, duration_ms)."""
            t_name = tc_item["name"]
            t_args = tc_item["arguments"] if isinstance(tc_item["arguments"], dict) else {}

            if on_tool_start:
                try:
                    on_tool_start(t_name, t_args)
                except Exception:
                    pass

            t0 = time.perf_counter()
            try:
                res = await tool_executor(t_name, t_args)
            except Exception as exc:
                res = f"Tool error ({t_name}): {exc}"
                logger.warning("[agentic_loop] Tool %s raised: %s", t_name, exc)
            duration_ms = (time.perf_counter() - t0) * 1000

            if on_tool_end:
                try:
                    # Newer callbacks accept (name, result, duration_ms);
                    # legacy ones only accept (name, result). Try both
                    # so we don't break older wirings.
                    try:
                        on_tool_end(t_name, res, duration_ms, t_args)
                    except TypeError:
                        on_tool_end(t_name, res)
                except Exception:
                    pass

            return tc_item, res, duration_ms

        if any(_requires_serial_tool_execution(tc.get("name", "")) for tc in tool_calls):
            exec_results = []
            for tc in tool_calls:
                exec_results.append(await _exec_one(tc))
        else:
            exec_results = await asyncio.gather(*[_exec_one(tc) for tc in tool_calls])
        # Strip duration for downstream message-append loop — it only
        # needs (tc, result) for the messages array.
        results = [(tc, res) for tc, res, _ in exec_results]
        if exec_results:
            # Log result previews for debugging (especially bash errors)
            for tc, res, dur in exec_results:
                res_str = str(res) if res else ""
                if dur < 5 or '"error"' in res_str[:300]:
                    # Fast return or error — likely blocked/failed, log more detail
                    logger.info(
                        "[agentic_loop] Round %d %s result (%0.fms): %s",
                        rounds, tc["name"], dur, res_str[:300],
                    )
            tool_durations = [(tc["name"], dur) for tc, _, dur in exec_results]
            logger.info(
                "[agentic_loop] Round %d tool durations: %s",
                rounds,
                ", ".join(f"{n}={d:.0f}ms" for n, d in tool_durations),
            )

        # 3. Check for consecutive tool errors — if every tool call in
        #    this round returned an error, increment the counter and
        #    circuit-break after MAX_CONSECUTIVE_TOOL_ERRORS rounds.
        all_errors = all(
            ('"error"' in str(res) or str(res).startswith("Tool error"))
            for _, res in results
        )
        if all_errors and results:
            consecutive_tool_errors += 1
            if consecutive_tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                logger.warning(
                    "[agentic_loop] Circuit breaker: %d consecutive rounds with all tool errors. "
                    "Aborting loop to avoid wasting tokens.",
                    consecutive_tool_errors,
                )
                return AgenticResult(
                    content="I wasn't able to complete this task — my tool calls kept failing. "
                            "This may be a permission or environment issue.",
                    messages=messages,
                    usage=total_usage,
                    rounds=rounds,
                    tool_calls_made=tool_calls_made,
                    stop_reason="error",
                    error="consecutive_tool_errors",
                )
        else:
            consecutive_tool_errors = 0

        # Append tool result messages (OpenAI format) with truncation.
        # Also dynamically inject schemas from search_tools results
        # so the LLM can call discovered tools in subsequent rounds.
        loaded_tool_names = {t.get("function", {}).get("name") for t in tools}
        pending_auto_tool_calls: list[dict[str, Any]] = []
        for tc, full_result in results:
            tool_calls_made.append(tc["name"])
            result_str = str(full_result) if full_result is not None else ""

            # Dynamic tool loading: when search_tools returns schemas,
            # inject them into the tools list for the next LLM round.
            compact_search_result: str | None = None
            if tc["name"] == "search_tools" and result_str.lstrip().startswith("{"):
                try:
                    search_result = json.loads(result_str.lstrip())
                    loaded_from_search: list[str] = []
                    load_names = [
                        str(name)
                        for name in (search_result.get("loaded_tools") or [])
                        if name
                    ]
                    if not load_names:
                        load_names = [
                            str(match.get("name"))
                            for match in search_result.get("matches", [])
                            if match.get("name") and match.get("available") is not False
                        ]
                    for match in search_result.get("matches", []):
                        if match.get("available") is False:
                            continue
                        schema = match.get("schema")
                        schema_name = (
                            schema.get("function", {}).get("name")
                            if isinstance(schema, dict)
                            else None
                        )
                        if schema_name and schema_name not in load_names:
                            load_names.append(schema_name)
                    schema_resolver = tool_schema_resolver
                    if schema_resolver is None:
                        try:
                            from packages.core.ai.runtime.tool_registry import runtime_tool_schema
                            schema_resolver = runtime_tool_schema
                        except Exception:
                            schema_resolver = None
                    for name in load_names:
                        schema = schema_resolver(name) if schema_resolver else None
                        if schema is None:
                            for match in search_result.get("matches", []):
                                if match.get("name") == name:
                                    schema = match.get("schema")
                                    break
                        loaded_name = (
                            schema.get("function", {}).get("name")
                            if isinstance(schema, dict)
                            else None
                        )
                        if loaded_name and loaded_name not in loaded_tool_names:
                            tools.append(schema)
                            loaded_tool_names.add(loaded_name)
                            loaded_from_search.append(loaded_name)
                    compact_search_result = _compact_search_tools_result_for_context(
                        search_result,
                        loaded_from_search,
                    )
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            if compact_search_result is not None:
                result_str = compact_search_result
            elif result_str.lstrip().startswith("{"):
                try:
                    tool_result = json.loads(result_str.lstrip())
                    if isinstance(tool_result, dict):
                        next_calls = tool_result.get("recommended_next_calls") or []
                        load_names: list[str] = []
                        seen_next_tool_names: set[str] = set()
                        for call in next_calls:
                            if not isinstance(call, dict):
                                continue
                            next_name = str(call.get("name") or call.get("tool_name") or "")
                            if not next_name.startswith("mcp__") or next_name in seen_next_tool_names:
                                continue
                            load_names.append(next_name)
                            seen_next_tool_names.add(next_name)
                        if load_names:
                            schema_resolver = tool_schema_resolver
                            if schema_resolver is None:
                                try:
                                    from packages.core.ai.runtime.tool_registry import runtime_tool_schema
                                    schema_resolver = runtime_tool_schema
                                except Exception:
                                    schema_resolver = None
                            for name in load_names:
                                schema = schema_resolver(name) if schema_resolver else None
                                loaded_name = (
                                    schema.get("function", {}).get("name")
                                    if isinstance(schema, dict)
                                    else None
                                )
                                if loaded_name and loaded_name not in loaded_tool_names:
                                    tools.append(schema)
                                    loaded_tool_names.add(loaded_name)
                            pending_auto_tool_calls.extend(_auto_tool_calls_from_result(tool_result, loaded_tool_names))
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

            dedupe_key = _tool_call_dedupe_key(tc)
            result_digest = _tool_result_digest(result_str)
            if seen_tool_result_digests.get(dedupe_key) == result_digest:
                result_str = _duplicate_tool_result_notice(
                    tc["name"],
                    result_str,
                    result_digest,
                )
            else:
                seen_tool_result_digests[dedupe_key] = result_digest

            result_str = _compact_tool_result_for_context(
                tc["name"],
                result_str,
                TOOL_RESULT_MAX_CHARS,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result_str,
            })

        terminal_control = (
            _detect_terminal_tool_result(results, terminal_tool_result_policy, user_message)
            or _detect_stop_parent_tool_result(results, user_message)
        )
        if terminal_control:
            final_content = str(terminal_control.get("content") or "")
            messages.append({
                "role": "assistant",
                "content": final_content,
            })
            logger.info(
                "[agentic_loop] Stopping after terminal tool result: %s",
                terminal_control.get("stop_reason"),
            )
            return AgenticResult(
                content=final_content,
                messages=messages,
                usage=total_usage,
                rounds=rounds,
                tool_calls_made=tool_calls_made,
                stop_reason=str(terminal_control.get("stop_reason") or SKILL_TERMINAL_STOP_REASON),
                control=terminal_control,
            )

        if pending_auto_tool_calls:
            auto_calls = _dedupe_auto_tool_calls(pending_auto_tool_calls, seen=seen_auto_tool_calls)
            if auto_calls:
                messages.append({
                    "role": "user",
                    "content": runtime_agentic_auto_next_calls_message(),
                })
                forced_tool_calls = auto_calls
                continue

        # Context compaction check
        messages = await _compact_messages(messages, model, temperature)

    # Exhausted max_rounds -- return whatever we have
    logger.warning(
        "[agentic_loop] Hit max_rounds=%d with %d tool calls. Forcing final response.",
        max_rounds, len(tool_calls_made),
    )

    # One last LLM call without tools to get a final summary
    messages.append({
        "role": "user",
        "content": runtime_agentic_max_rounds_final_prompt(),
    })
    messages = await _compact_messages(messages, model, temperature)
    _llm_start = time.time()
    final_content, final_usage = await runtime_execute_agentic_final_completion(
        messages,
        temperature=temperature,
        model=model,
        metadata=metadata,
    )
    _llm_duration_ms = (time.time() - _llm_start) * 1000
    final_usage = dict(final_usage or EMPTY_USAGE)
    final_usage["context_attribution"] = _estimate_context_attribution(messages, [])
    final_reasoning_content = _pop_reasoning_content(final_usage)
    _add_usage(total_usage, final_usage)
    final_assistant_msg: Dict[str, Any] = {
        "role": "assistant",
        "content": final_content or "",
    }
    _attach_reasoning_content(final_assistant_msg, final_reasoning_content)
    messages.append(final_assistant_msg)

    if on_llm_call:
        try:
            on_llm_call(rounds + 1, _llm_duration_ms, final_usage, [], "max_rounds_summary")
        except Exception:
            pass

    return AgenticResult(
        content=final_content or "",
        messages=messages,
        usage=total_usage,
        rounds=rounds + 1,
        tool_calls_made=tool_calls_made,
        stop_reason="max_rounds",
    )
