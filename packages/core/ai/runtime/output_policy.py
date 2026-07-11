from __future__ import annotations

import re

from packages.core.ai.runtime.skill_forcing import runtime_message_text_for_intent

PREVIOUS_TOOL_ACTIVITY_MARKER = "[Previous tool activity]"
PROVIDER_REASONING_META_KEY = "provider_reasoning_content"

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ENGLISH_OPERATIONAL_NARRATION_RE = re.compile(
    r"\b("
    r"now\s+(i|let)|let\s+me|i\s+need|i\s+have|i\s+see|i\s+understand|"
    r"the\s+issue|the\s+ppt|good\s+progress|found\s+it|wait|actually|"
    r"json\s+is\s+valid|rebuild|quality\s+gate"
    r")\b",
    re.IGNORECASE,
)
def runtime_strip_leaked_tool_activity(content: str) -> str:
    """Remove internal persisted-tool summaries if a model echoed them."""

    if not content or PREVIOUS_TOOL_ACTIVITY_MARKER not in content:
        return content or ""
    return content.split(PREVIOUS_TOOL_ACTIVITY_MARKER, 1)[0].rstrip()


def runtime_prefers_chinese(message: str | list[dict]) -> bool:
    return bool(_CJK_RE.search(runtime_message_text_for_intent(message)))


def runtime_coerce_visible_text_language(
    text: str,
    *,
    prefers_chinese: bool,
) -> str:
    """Hide routine English progress narration in Chinese conversations."""

    if not prefers_chinese:
        return text
    stripped = (text or "").strip()
    if not stripped or _CJK_RE.search(stripped):
        return text
    alpha_chars = len(re.findall(r"[A-Za-z]", stripped))
    if (
        alpha_chars >= max(8, len(stripped) * 0.45)
        and _ENGLISH_OPERATIONAL_NARRATION_RE.search(stripped)
    ):
        return ""
    return text


def runtime_fallback_stream_final_summary(
    tool_calls_made: list[str] | None,
    tool_results: list[dict] | None,
) -> str:
    calls = tool_calls_made or []
    if not calls:
        return ""

    joined_names = " ".join(calls)
    joined_results = " ".join(
        str(item.get("result") or "")
        for item in (tool_results or [])
        if isinstance(item, dict)
    )
    combined = f"{joined_names} {joined_results}"

    return "已完成。"


def runtime_sanitize_assistant_content_after_loop(
    content: str,
    tool_calls_made: list[str] | None,
) -> str:
    """Clean internal runtime annotations without guessing user intent from text."""

    cleaned = runtime_strip_leaked_tool_activity(content or "")
    return cleaned


def runtime_assistant_reasoning_meta(messages: list[dict] | None) -> dict | None:
    """Extract provider replay-only reasoning from the final assistant turn."""

    if not messages:
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            return {PROVIDER_REASONING_META_KEY: reasoning_content}
    return None


def runtime_assistant_result_meta(result) -> dict | None:
    meta = runtime_assistant_reasoning_meta(
        getattr(result, "messages", None)
    ) or {}
    stop_reason = getattr(result, "stop_reason", None)
    if stop_reason and stop_reason != "completed":
        meta["stop_reason"] = stop_reason
    error = getattr(result, "error", None)
    if error:
        meta["error"] = error
    error_detail = getattr(result, "error_detail", None)
    if error_detail:
        meta["limit_detail"] = error_detail
    return meta or None


def runtime_assistant_stream_error_content(error_message: str) -> str:
    message = (error_message or "Unknown error").strip()
    return (
        "Sorry, the request failed. Please try again.\n\n"
        "Error detail: "
        f"{message}"
    )


def runtime_assistant_stream_interrupted_content(
    partial_content: str | None = None,
) -> str:
    note = (
        "The response was interrupted before the assistant could finish. "
        "Some tool-created files or media jobs may already exist, but this "
        "run did not complete its final reply."
    )
    partial = (partial_content or "").strip()
    if partial:
        return f"{partial}\n\n[{note}]"
    return note
