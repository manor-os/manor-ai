from __future__ import annotations

import json
import re
from typing import Any

from packages.core.ai.runtime.skill_forcing import runtime_message_text_for_intent
from packages.core.services.sensitive_data import (
    redact_sensitive_text,
    sanitize_approval_credentials,
    sanitize_sensitive_payload,
)

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
_INTERNAL_ERROR_DETAIL_RE = re.compile(
    r"(?:sqlalchemy|asyncpg|psycopg|InFailedSQLTransactionError|"
    r"current\s+transaction\s+is\s+aborted|Traceback\s+\(most\s+recent\s+call\s+last\)|"
    r"\[SQL:|\[parameters?:)",
    re.IGNORECASE,
)
_INTERNAL_ERROR_FAILURE_SIGNAL_RE = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)|"
    r"error|failed|failure|exception|aborted)\b|"
    r"Traceback\s+\(most\s+recent\s+call\s+last\)|\[SQL:|\[parameters?:",
    re.IGNORECASE,
)
_INTERNAL_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_INTERNAL_TOOL_IDENTIFIER_RE = re.compile(
    r"\bmcp__[a-z0-9_.-]+(?:__[a-z0-9_.-]+)+\b",
    re.IGNORECASE,
)
_INTERNAL_TOOL_CONTRACT_ERROR_RE = re.compile(
    r"(?:"
    r"\b(?:unknown|unrecognized|unsupported|unregistered)\s+"
    r"(?:runtime\s+)?(?:tool|function)(?:\s+(?:name|key))?\b|"
    r"\b(?:tool|function)(?:\s+(?:name|key|id))?\b.{0,100}?"
    r"(?:not\s+(?:found|registered)|unregistered|mismatch|misalign(?:ed|ment)?|"
    r"does\s+not\s+match|missing\s+(?:handler|executor))\b|"
    r"\b(?:no|missing)\s+(?:registered\s+)?(?:handler|executor)\s+for\s+"
    r"(?:tool|function)\b|"
    r"\b(?:no|missing)\s+(?:(?:matching|registered)\s+)?"
    r"(?:handler|executor)\s+for\b|"
    r"\bmcp__[a-z0-9_.-]+(?:__[a-z0-9_.-]+)+\b.{0,100}?"
    r"(?:failed?|failure|unavailable|error)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_ERROR_PREFIX_RE = re.compile(
    r"\btool\s+error\s*\([^\)\r\n]{1,160}\)\s*:\s*",
    re.IGNORECASE,
)
_PUBLIC_ACTIONABLE_EXECUTOR_ERROR_RE = re.compile(
    r"^(?:"
    r"(?:permission denied|authentication required|authorization required)\.?"
    r"(?:\s+(?:reconnect|connect|sign in to|log in to) your "
    r"[a-z0-9][a-z0-9 ._-]{0,79} account\.?)?|"
    r"(?:rate limit|quota|usage limit) "
    r"(?:exceeded|reached)\.?(?:\s+(?:try again later|upgrade your plan"
    r"(?: or try again later)?)\.?)?|"
    r"credits? exhausted\.?(?:\s+(?:add credits|upgrade your plan)"
    r"(?: or try again later)?\.?)?"
    r")$",
    re.IGNORECASE,
)

PUBLIC_TOOL_FAILURE_MESSAGE = (
    "This operation is temporarily unavailable. Please try again."
)
PUBLIC_REQUEST_FAILURE_MESSAGE = "Sorry, the request failed. Please try again."
PUBLIC_TOOL_NAME = "operation"
_PUBLIC_RAW_RESULT_TOOL_NAMES = frozenset({
    "continue_workspace_draft",
    "manor",
    "start_workspace_draft",
})
_PUBLIC_TOOL_RESULT_KEYS = frozenset({
    "error",
    "error_detail",
    "error_message",
    "detail",
    "reason",
    "result",
    "result_preview",
    "workflow_error",
})
_TOOL_CALL_STRUCTURAL_KEYS = frozenset({
    "args",
    "arguments",
    "calls",
    "function",
    "name",
    "result",
    "status",
})
_WORKSPACE_DRAFT_PUBLIC_KEYS = (
    "artifact_kind",
    "draft_id",
    "status",
    "ready",
    "missing",
    "fields",
    "assistant_reply",
    "title",
    "next_step",
)
_PRIVATE_FAILURE_FIELD_RE = re.compile(
    r"^(?:"
    r"raw_?result|"
    r"(?:registered_?|requested_?|expected_?)?(?:tool|function)_?(?:key|name|id)|"
    r"(?:registered_?)?handler(?:_?(?:key|name|id))?|"
    r"executor(?:_?(?:key|name|id))?|"
    r"stack_?trace|traceback|sql|query|parameters?"
    r")$",
    re.IGNORECASE,
)


def _runtime_executor_failure_is_public_actionable(detail: str) -> bool:
    """Return whether an executor failure is safe and useful to show."""

    return bool(_PUBLIC_ACTIONABLE_EXECUTOR_ERROR_RE.fullmatch(detail.strip()))


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

    return "已完成。"


def runtime_sanitize_assistant_content_after_loop(
    content: str,
    *,
    internal_failure_seen: bool = False,
) -> str:
    """Clean internal runtime annotations without guessing user intent from text."""

    cleaned = runtime_strip_leaked_tool_activity(content or "")
    has_explicit_private_failure = bool(
        _TOOL_ERROR_PREFIX_RE.search(cleaned)
        or (
            _INTERNAL_ERROR_DETAIL_RE.search(cleaned)
            and _INTERNAL_ERROR_FAILURE_SIGNAL_RE.search(cleaned)
        )
        or (
            _INTERNAL_TOOL_IDENTIFIER_RE.search(cleaned)
            and (
                _INTERNAL_TOOL_CONTRACT_ERROR_RE.search(cleaned)
                or _INTERNAL_ERROR_FAILURE_SIGNAL_RE.search(cleaned)
            )
        )
    )
    if not internal_failure_seen and not has_explicit_private_failure:
        return cleaned
    public_content = runtime_public_tool_result(cleaned)
    if public_content == PUBLIC_TOOL_FAILURE_MESSAGE:
        return PUBLIC_REQUEST_FAILURE_MESSAGE
    return str(public_content or "")


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


def runtime_is_internal_tool_contract_failure(value: Any) -> bool:
    """Return whether a failure exposes runtime tool routing internals."""

    if value is None:
        return False
    text = str(value)
    had_executor_prefix = bool(_TOOL_ERROR_PREFIX_RE.search(text))
    detail = _TOOL_ERROR_PREFIX_RE.sub("", text).strip()
    if had_executor_prefix:
        return not _runtime_executor_failure_is_public_actionable(detail)
    return bool(
        (
            _INTERNAL_ERROR_DETAIL_RE.search(detail)
            and _INTERNAL_ERROR_FAILURE_SIGNAL_RE.search(detail)
        )
        or (
            _INTERNAL_TOOL_IDENTIFIER_RE.search(detail)
            and _INTERNAL_ERROR_FAILURE_SIGNAL_RE.search(detail)
        )
        or _INTERNAL_TOOL_CONTRACT_ERROR_RE.search(detail)
    )


def runtime_is_internal_failure_detail(value: Any) -> bool:
    """Return whether a user-facing failure contains private implementation detail."""

    if value is None:
        return False
    text = str(value)
    return runtime_is_internal_tool_contract_failure(text)


def runtime_public_tool_result(value: Any) -> Any:
    """Project a tool result for users while retaining actionable failures."""

    if value is None:
        return value
    value = sanitize_approval_credentials(value)
    text = value if isinstance(value, str) else str(value)
    had_executor_prefix = bool(_TOOL_ERROR_PREFIX_RE.search(text))
    public_text = _TOOL_ERROR_PREFIX_RE.sub("", text).strip()
    if had_executor_prefix:
        if _runtime_executor_failure_is_public_actionable(public_text):
            return public_text
        return PUBLIC_TOOL_FAILURE_MESSAGE
    if runtime_is_internal_failure_detail(text):
        return PUBLIC_TOOL_FAILURE_MESSAGE
    return value


def runtime_public_failure_payload(value: Any) -> Any:
    """Project a known failure envelope while preserving its public shape."""

    value = sanitize_sensitive_payload(value)
    if isinstance(value, list):
        return [runtime_public_failure_payload(item) for item in value]
    if isinstance(value, dict):
        projected = {
            key: runtime_public_failure_payload(item)
            for key, item in value.items()
            if not _PRIVATE_FAILURE_FIELD_RE.fullmatch(str(key))
            and not _INTERNAL_TOOL_IDENTIFIER_RE.fullmatch(str(key))
        }
        if not projected and value:
            return {"message": PUBLIC_TOOL_FAILURE_MESSAGE}
        return projected
    public_value = runtime_public_tool_result(value)
    if isinstance(public_value, str):
        return _INTERNAL_TOOL_IDENTIFIER_RE.sub(
            "this operation",
            public_value,
        )
    return public_value


def _runtime_public_workspace_draft_result(value: Any) -> str | None:
    """Return a validated, allowlisted Workspace Draft artifact envelope."""

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("artifact_kind") != "workspace_draft":
        return None
    draft_id = parsed.get("draft_id")
    if not isinstance(draft_id, str) or not draft_id.strip():
        return None

    projected = {
        key: parsed[key]
        for key in _WORKSPACE_DRAFT_PUBLIC_KEYS
        if key in parsed
    }
    try:
        return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _runtime_strip_private_failure_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_runtime_strip_private_failure_fields(item) for item in value]
    if isinstance(value, str):
        return _INTERNAL_TOOL_IDENTIFIER_RE.sub("this operation", value)
    if not isinstance(value, dict):
        return value
    return {
        key: _runtime_strip_private_failure_fields(item)
        for key, item in value.items()
        if not _PRIVATE_FAILURE_FIELD_RE.fullmatch(str(key))
        and not _INTERNAL_TOOL_IDENTIFIER_RE.fullmatch(str(key))
    }


def runtime_public_tool_payload(value: Any) -> Any:
    """Copy a persisted tool/UI payload without private execution details.

    ``raw_result`` is an audit/replay field and is removed by the generic
    projector. Only result-bearing fields are checked for tool-contract
    failures so ordinary arguments and successful operation labels keep their
    existing UI behavior. ``runtime_public_tool_calls`` owns the one narrow
    successful Workspace Draft compatibility exception.
    """

    if isinstance(value, list):
        return [runtime_public_tool_payload(item) for item in value]
    if not isinstance(value, dict):
        return value

    projected: dict[Any, Any] = {}
    for key, item in value.items():
        if key in {"raw_result", "rawResult"}:
            continue
        if key == "sub_agent_events":
            projected[key] = [
                _runtime_public_sub_agent_event(event)
                for event in item if isinstance(event, dict)
            ] if isinstance(item, list) else []
            continue
        if key in _PUBLIC_TOOL_RESULT_KEYS:
            item = runtime_public_tool_result(item)
        elif key in {"args", "arguments"}:
            item = sanitize_approval_credentials(item)
        projected[key] = runtime_public_tool_payload(item)
    if (
        value.get("kind") == "tool"
        and _runtime_tool_call_contains_private_failure(value, projected)
    ):
        projected = _runtime_strip_private_failure_fields(projected)
        _runtime_redact_private_tool_name(projected)
        if "display_name" in projected:
            projected["display_name"] = "Operation"
    return projected


def _runtime_value_contains_private_failure(value: Any) -> bool:
    if isinstance(value, list):
        return any(_runtime_value_contains_private_failure(item) for item in value)
    if isinstance(value, dict):
        return any(_runtime_value_contains_private_failure(item) for item in value.values())
    return runtime_is_internal_failure_detail(value)


def _runtime_value_contains_explicit_private_failure(value: Any) -> bool:
    if isinstance(value, list):
        return any(
            _runtime_value_contains_explicit_private_failure(item)
            for item in value
        )
    if isinstance(value, dict):
        return any(
            _runtime_value_contains_explicit_private_failure(item)
            for item in value.values()
        )
    if value is None:
        return False
    text = str(value)
    return runtime_is_internal_failure_detail(text) and bool(
        _TOOL_ERROR_PREFIX_RE.search(text)
        or _INTERNAL_TOOL_IDENTIFIER_RE.search(text)
        or (
            _INTERNAL_ERROR_DETAIL_RE.search(text)
            and _INTERNAL_ERROR_FAILURE_SIGNAL_RE.search(text)
        )
    )


def _runtime_value_contains_public_tool_failure(value: Any) -> bool:
    if isinstance(value, list):
        return any(_runtime_value_contains_public_tool_failure(item) for item in value)
    if isinstance(value, dict):
        return any(
            _runtime_value_contains_public_tool_failure(item)
            for item in value.values()
        )
    return value == PUBLIC_TOOL_FAILURE_MESSAGE


def _runtime_tool_call_contains_private_failure(
    value: dict[Any, Any],
    projected: dict[Any, Any] | None = None,
) -> bool:
    error_keys = {"error", "error_detail", "error_message", "workflow_error"}
    if any(
        key in value and _runtime_value_contains_private_failure(value[key])
        for key in error_keys
    ):
        return True
    result_keys = _PUBLIC_TOOL_RESULT_KEYS | {
        "content",
        "message",
        "output",
        "raw_result",
        "rawResult",
    }
    if any(
        key in value
        and _runtime_value_contains_explicit_private_failure(value[key])
        for key in result_keys
    ):
        return True
    status = str(value.get("status") or "").strip().lower()
    if status not in {"blocked", "error", "failed", "timeout"}:
        return False
    return any(
        key in value and _runtime_value_contains_private_failure(value[key])
        for key in result_keys
    ) or _runtime_value_contains_public_tool_failure(value) or bool(
        projected and _runtime_value_contains_public_tool_failure(projected)
    )


def _runtime_tool_calls_contain_private_failure(value: Any) -> bool:
    if isinstance(value, list):
        return any(_runtime_tool_calls_contain_private_failure(item) for item in value)
    if not isinstance(value, dict):
        return False
    if _TOOL_CALL_STRUCTURAL_KEYS.intersection(value):
        return _runtime_tool_call_contains_private_failure(value)
    return any(
        _runtime_value_contains_explicit_private_failure(item)
        or _runtime_value_contains_public_tool_failure(item)
        for item in value.values()
    )


def _runtime_redact_private_tool_name(projected: dict[Any, Any]) -> None:
    if "name" in projected:
        projected["name"] = PUBLIC_TOOL_NAME
    function = projected.get("function")
    if isinstance(function, dict) and "name" in function:
        projected["function"] = {**function, "name": PUBLIC_TOOL_NAME}


def runtime_public_tool_calls(
    value: Any,
    *,
    preserve_replay_result: bool = True,
) -> Any:
    """Project both current tool-event lists and legacy name/result maps."""

    if isinstance(value, list):
        return [
            runtime_public_tool_calls(
                item,
                preserve_replay_result=preserve_replay_result,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return runtime_public_tool_payload(value)
    if _TOOL_CALL_STRUCTURAL_KEYS.intersection(value):
        tool_name = str(value.get("name") or "").strip().lower()
        tool_status = str(value.get("status") or "").strip().lower()
        raw_result = value.get("raw_result", value.get("rawResult"))
        projected = runtime_public_tool_payload(value)
        for key in ("content", "message", "output"):
            if key in projected:
                projected[key] = runtime_public_tool_payload(
                    runtime_public_tool_result(projected[key])
                )
        if "calls" in projected:
            projected["calls"] = runtime_public_tool_calls(
                projected["calls"],
                preserve_replay_result=preserve_replay_result,
            )
        if (
            preserve_replay_result
            and tool_name in _PUBLIC_RAW_RESULT_TOOL_NAMES
            and tool_status not in {"blocked", "error", "failed", "timeout"}
        ):
            public_draft_result = _runtime_public_workspace_draft_result(raw_result)
            if public_draft_result is None:
                public_draft_result = _runtime_public_workspace_draft_result(
                    value.get("result")
                )
            if public_draft_result is not None:
                projected["raw_result"] = public_draft_result
        if _runtime_tool_call_contains_private_failure(value, projected):
            projected = _runtime_strip_private_failure_fields(projected)
            _runtime_redact_private_tool_name(projected)
        return projected
    projected: dict[Any, Any] = {}
    private_failure_count = 0
    for key, item in value.items():
        if key in {"raw_result", "rawResult"}:
            continue
        public_item = runtime_public_tool_payload(runtime_public_tool_result(item))
        public_key = key
        if (
            _runtime_value_contains_explicit_private_failure(item)
            or _runtime_value_contains_public_tool_failure(public_item)
        ):
            private_failure_count += 1
            public_key = (
                PUBLIC_TOOL_NAME
                if private_failure_count == 1
                else f"{PUBLIC_TOOL_NAME}_{private_failure_count}"
            )
            while public_key in projected or public_key in value:
                private_failure_count += 1
                public_key = f"{PUBLIC_TOOL_NAME}_{private_failure_count}"
        projected[public_key] = public_item
    return projected


def _runtime_public_tool_name(value: Any) -> Any:
    if not isinstance(value, str):
        return runtime_public_tool_payload(value)
    if _INTERNAL_TOOL_IDENTIFIER_RE.fullmatch(value.strip()):
        return PUBLIC_TOOL_NAME
    return value


def _runtime_public_sub_agent_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = runtime_public_tool_payload(value)
    if not isinstance(projected, dict):
        return {}

    error = value.get("error")
    if error:
        projected["error"] = runtime_assistant_stream_error_content(str(error))

    def public_tool_event(tool: dict[str, Any]) -> dict[str, Any]:
        public_tool = runtime_public_tool_calls(
            tool,
            preserve_replay_result=False,
        )
        if isinstance(public_tool, dict) and "name" in public_tool:
            public_tool["name"] = _runtime_public_tool_name(public_tool["name"])
        if tool.get("error"):
            public_tool["error"] = runtime_assistant_stream_error_content(str(tool["error"]))
        return public_tool

    tool = value.get("tool")
    if isinstance(tool, dict):
        projected["tool"] = public_tool_event(tool)
    if isinstance(value.get("tools"), list):
        projected["tools"] = [
            public_tool_event(tool) for tool in value["tools"] if isinstance(tool, dict)
        ]

    tool_calls_made = value.get("tool_calls_made")
    if isinstance(tool_calls_made, list):
        projected["tool_calls_made"] = [
            _runtime_public_tool_name(tool_name)
            for tool_name in tool_calls_made
        ]
    return projected


def runtime_public_transport_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a live event or snapshot without replay-only tool results."""

    projected = runtime_public_tool_payload(payload)
    if not isinstance(projected, dict):
        return {}
    for key in ("tool_call", "tool_calls"):
        if key in payload:
            projected[key] = runtime_public_tool_calls(
                payload[key],
                preserve_replay_result=False,
            )
    if "sub_agent" in payload:
        projected["sub_agent"] = _runtime_public_sub_agent_event(
            payload["sub_agent"]
        )
    return projected


def runtime_public_assistant_message_content(
    content: str | None,
    meta: dict[str, Any] | None,
    tool_calls: Any = None,
) -> str | None:
    """Return the user-facing body for a persisted assistant message."""

    message_meta = meta if isinstance(meta, dict) else {}
    public_content = runtime_sanitize_assistant_content_after_loop(
        content or "",
        internal_failure_seen=_runtime_tool_calls_contain_private_failure(tool_calls),
    )
    stop_reason = str(message_meta.get("stop_reason") or "").lower()
    if stop_reason == "credit_exhausted":
        return public_content if content is not None else None
    if (
        message_meta.get("stream_error") is True
        or str(message_meta.get("stream_status") or "").lower() == "error"
        or stop_reason == "error"
        or bool(message_meta.get("error"))
    ):
        error_detail = (
            message_meta.get("error_message")
            or message_meta.get("error")
            or public_content
        )
        return runtime_assistant_stream_error_content(str(error_detail))
    return public_content if content is not None else None


def runtime_assistant_stream_error_content(error_message: str) -> str:
    message = str(
        redact_sensitive_text(error_message or "Unknown error") or "Unknown error"
    ).strip()
    if _INTERNAL_ERROR_CODE_RE.fullmatch(message):
        return PUBLIC_REQUEST_FAILURE_MESSAGE
    public_message = runtime_public_tool_result(message)
    if public_message == PUBLIC_TOOL_FAILURE_MESSAGE:
        return PUBLIC_REQUEST_FAILURE_MESSAGE
    message = str(public_message or "Unknown error").strip()
    if message == PUBLIC_REQUEST_FAILURE_MESSAGE:
        return message

    public_prefix = f"{PUBLIC_REQUEST_FAILURE_MESSAGE}\n\nError detail: "
    candidate_detail = (
        message[len(public_prefix) :].strip()
        if message.startswith(public_prefix)
        else message
    )
    if not _runtime_executor_failure_is_public_actionable(candidate_detail):
        return PUBLIC_REQUEST_FAILURE_MESSAGE
    return f"{public_prefix}{candidate_detail}"


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
