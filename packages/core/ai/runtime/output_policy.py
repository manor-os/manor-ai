from __future__ import annotations

import json
import re
from typing import Any

from packages.core.services.sensitive_data import (
    redact_sensitive_text,
    sanitize_approval_credentials,
    sanitize_sensitive_payload,
)

PREVIOUS_TOOL_ACTIVITY_MARKER = "[Previous tool activity]"
PROVIDER_REASONING_META_KEY = "provider_reasoning_content"

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
PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE = "The target website denied access (HTTP 403)."
PUBLIC_FILE_EDIT_DENIED_MESSAGE = (
    "You don't have permission to edit this file. Ask the file owner for edit access."
)
PUBLIC_FILE_ACCESS_DENIED_MESSAGE = (
    "You don't have permission to access this file. Ask the file owner for access."
)
PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE = (
    "The selected attachment is missing or no longer accessible."
)
PUBLIC_APPROVAL_ATTACHMENT_UNAVAILABLE_MESSAGE = (
    "The selected attachment is no longer accessible for approval."
)
# Fixed domain messages only: extended diagnostics must still use the private
# failure path. Missing, inaccessible and out-of-scope files share one cause.
_PUBLIC_ATTEMPT_CAUSE_MESSAGES = {
    "Knowledge document unavailable": PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE,
    "Knowledge document is outside the current Workspace": PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE,
    "Knowledge Workspace unavailable": PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE,
    "Knowledge file unavailable": PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE,
    "Knowledge file exceeds the transfer size limit": "The selected attachment exceeds the transfer size limit.",
    "Knowledge file changed; request a new transfer": "The selected attachment has changed. Select it again.",
    "Knowledge file unavailable or changed": "The selected attachment is unavailable or has changed.",
    "Knowledge file is busy; retry shortly": "The selected attachment is currently in use. Try again shortly.",
    "Knowledge document download is not allowed": "Downloading the selected attachment is not allowed.",
    "Chrome connection unavailable": "The Chrome connection is unavailable.",
    "Chrome connection credential changed": "The Chrome connection has changed. Start a new transfer.",
}
_PUBLIC_FILE_PERMISSION_MESSAGES = frozenset({
    PUBLIC_FILE_EDIT_DENIED_MESSAGE, PUBLIC_FILE_ACCESS_DENIED_MESSAGE,
})
PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE = (
    "This operation was blocked by the current authorization configuration."
)
PUBLIC_PERMISSION_DENIED_MESSAGE = (
    "You don't have permission to perform this operation."
)
PUBLIC_TOOL_OUTCOME_UNCONFIRMED_MESSAGE = (
    "The previous operation could not be confirmed. Read the current page and try again."
)
PUBLIC_APPROVAL_ORIGIN_NOT_EXECUTABLE_MESSAGE = (
    "This approved action is no longer executable."
)
PUBLIC_WORKFLOW_ORIGIN_NOT_EXECUTABLE_MESSAGE = (
    "This workflow is missing, paused, stopped, or outside the current actor's scope."
)
PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE = (
    "The Chrome browser task already finished. Start a new Chrome task before doing more browser work."
)
PUBLIC_TOOL_BLOCKED_MESSAGE = "This operation was blocked."
_PUBLIC_TYPED_ERROR_MESSAGES = {
    "website_access_denied": PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
    "blocked_by_authorization_configuration": PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE,
    "blocked_by_permission": PUBLIC_PERMISSION_DENIED_MESSAGE,
    "tool_outcome_unconfirmed": PUBLIC_TOOL_OUTCOME_UNCONFIRMED_MESSAGE,
    "approval_origin_not_executable": PUBLIC_APPROVAL_ORIGIN_NOT_EXECUTABLE_MESSAGE,
    "workflow_origin_not_executable": PUBLIC_WORKFLOW_ORIGIN_NOT_EXECUTABLE_MESSAGE,
    "chrome_tabs_already_finalized": PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE,
}
_PUBLIC_TYPED_ERROR_SENTENCES = frozenset(_PUBLIC_TYPED_ERROR_MESSAGES.values())
MODEL_OUTPUT_LIMIT_ERROR_CODE = "model_output_limit"
PUBLIC_MODEL_OUTPUT_LIMIT_MESSAGE = (
    "The model reached its output limit before finishing. Check the activity "
    "log for any completed actions before retrying."
)
PUBLIC_LLM_CONFIGURATION_MESSAGE = (
    "AI is not configured for this chat. Ask your administrator to configure "
    "an AI provider, then try again."
)
#: A failure on the user's own (BYOK) model route. Manor cannot fix it, the
#: owner can, so the owner is told where to look. Mirrors the
#: ``llm_configuration_missing`` contract: a typed code plus a fixed sentence
#: every projection layer recognises and passes through.
BYOK_PROVIDER_ERROR_CODE = "byok_provider_error"
PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE = (
    "Your BYOK model provider returned an error. Check the model, API key, "
    "and base URL in your AI settings and the provider's status, then try again."
)
_PUBLIC_ERROR_DETAIL_PREFIX = "\n\nError detail: "
#: The only detail shape allowed after the BYOK sentence: the HTTP status (or
#: "no response"), the provider host the owner configured, and the model id.
#: Anything else — provider bodies, request diagnostics, secrets — is dropped
#: by every layer, whichever one sees the text first.
_PUBLIC_BYOK_DETAIL_RE = re.compile(
    r"^(?:HTTP [1-5]\d{2}|no response) from [A-Za-z0-9.-]{1,253}(?::\d{1,5})? "
    r"\(model [A-Za-z0-9._:/-]{1,120}\)$"
)
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
_TOOL_RESULT_FAILURE_STATUSES = frozenset({
    "error",
    "failed",
    "failure",
    "timeout",
})
_SIGNED_URL_QUERY_RE = re.compile(
    r"[?&](?:x-amz-(?:credential|security-token|signature)|signature|sig)=",
    re.IGNORECASE,
)


# Tool-call delimiters that never occur in ordinary prose. A model that writes
# them as text is describing (usually inventing) a tool call that never ran.
_PSEUDO_TOOL_FENCED_CODE_RE = re.compile(
    r"^[ \t]*(```|~~~)[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
_PSEUDO_TOOL_INLINE_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)
_PSEUDO_TOOL_NS = r"(?:[A-Za-z_][\w.-]*:)?"
# Delimiters reserved for tool calling. Case-sensitive: these are emitted
# verbatim by tool-calling formats, never as ordinary prose or HTML.
_PSEUDO_TOOL_STRONG_TAG_RE = re.compile(
    r"</?" + _PSEUDO_TOOL_NS
    + r"(tool_call|tool_response|tool_result|tool_use|function_calls|function_results)"
    r"(?=[\s/>])[^<>]*>"
)
_PSEUDO_TOOL_NAMESPACED_INVOKE_RE = re.compile(r"</?[A-Za-z_][\w.-]*:invoke(?=[\s/>])[^<>]*>")
# invoke/parameter are ordinary words in BPEL, Tomcat, Axis2 or DocBook XML.
# They count only beside a strong delimiter or in the call structure itself.
_PSEUDO_TOOL_WEAK_TAG_RE = re.compile(
    r"</?" + _PSEUDO_TOOL_NS + r"(invoke|parameter)(?=[\s/>])[^<>]*>"
)
_PSEUDO_TOOL_INVOKE_STRUCTURE_RE = re.compile(
    r"<" + _PSEUDO_TOOL_NS + r"""invoke\s+name=["'][^"'<>]+["']\s*>"""
    r"(?:(?!</" + _PSEUDO_TOOL_NS + r"invoke>).)*?"
    r"<" + _PSEUDO_TOOL_NS + r"""parameter\s+name=["'][^"'<>]+["']\s*>""",
    re.DOTALL,
)
_PSEUDO_TOOL_BRACKET_RE = re.compile(r"\[TOOL_CALLS\]")
_PSEUDO_TOOL_SPECIAL_TOKEN_RE = re.compile(
    r"<[|｜][^<>|｜\n]{0,40}tool[^<>|｜\n]{0,40}[|｜]>"
)
_PSEUDO_TOOL_FUNCTION_TAG_RE = re.compile(r"<function=[\w.-]+>")


def runtime_pseudo_tool_markup(text: Any) -> list[str]:
    """Return the tool-call markup kinds written as plain text, or ``[]``.

    Fenced and inline code are ignored, so explaining the syntax is fine.
    Only delimiters reserved for tool calling count; ordinary HTML and XML
    vocabularies that reuse ``invoke``/``parameter`` do not.
    """

    if not isinstance(text, str) or not text:
        return []
    visible = _PSEUDO_TOOL_FENCED_CODE_RE.sub(" ", text)
    visible = _PSEUDO_TOOL_INLINE_CODE_RE.sub(" ", visible)
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    for match in _PSEUDO_TOOL_STRONG_TAG_RE.finditer(visible):
        add(match.group(1))
    if _PSEUDO_TOOL_NAMESPACED_INVOKE_RE.search(visible):
        add("invoke")
    if kinds or _PSEUDO_TOOL_INVOKE_STRUCTURE_RE.search(visible):
        for match in _PSEUDO_TOOL_WEAK_TAG_RE.finditer(visible):
            add(match.group(1))
    for pattern, kind in (
        (_PSEUDO_TOOL_BRACKET_RE, "[TOOL_CALLS]"),
        (_PSEUDO_TOOL_SPECIAL_TOKEN_RE, "<|tool_call|>"),
        (_PSEUDO_TOOL_FUNCTION_TAG_RE, "<function=name>"),
    ):
        if pattern.search(visible):
            add(kind)
    return kinds


def _runtime_executor_failure_is_public_actionable(detail: str) -> bool:
    """Return whether an executor failure is safe and useful to show."""

    return bool(_PUBLIC_ACTIONABLE_EXECUTOR_ERROR_RE.fullmatch(detail.strip()))


def runtime_public_byok_failure_message(detail: str | None = None) -> str:
    """Owner-facing BYOK failure: the fixed sentence plus only a structured detail."""

    candidate = str(detail or "").strip()
    if candidate and _PUBLIC_BYOK_DETAIL_RE.fullmatch(candidate):
        return f"{PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE}{_PUBLIC_ERROR_DETAIL_PREFIX}{candidate}"
    return PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE


def _runtime_byok_failure_projection(message: str) -> str | None:
    """Recognise the BYOK contract in any error text; ``None`` when absent."""

    if message == BYOK_PROVIDER_ERROR_CODE or message == PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE:
        return PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE
    prefix = f"{PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE}{_PUBLIC_ERROR_DETAIL_PREFIX}"
    if message.startswith(prefix):
        return runtime_public_byok_failure_message(message[len(prefix):])
    return None


def _runtime_typed_public_error_message(value: Any) -> str | None:
    """Map a known runtime block code or its public sentence; never leak internals."""

    if value is None:
        return None
    if isinstance(value, dict):
        for code in (value.get("error"), value.get("reason")):
            if isinstance(code, str):
                if code.strip() == "website_access_denied" and value.get("status_code") != 403:
                    continue
                mapped = _PUBLIC_TYPED_ERROR_MESSAGES.get(code.strip())
                if mapped is not None:
                    return mapped
        message = value.get("message")
        if isinstance(message, str):
            return _runtime_typed_public_error_message(message)
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in _PUBLIC_TYPED_ERROR_SENTENCES:
        return text
    mapped = _PUBLIC_TYPED_ERROR_MESSAGES.get(text)
    if mapped is not None:
        return mapped
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return _runtime_typed_public_error_message(parsed)
    return None


def _runtime_is_file_read_content_payload(value: Any) -> bool:
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return False
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and all(
            isinstance(payload.get(key), str)
            for key in ("path", "source_sha256", "content")
        )
        and (
            "resolved_path" not in payload
            or isinstance(payload["resolved_path"], str)
        )
        and not {"error", "error_detail", "error_message", "status"}.intersection(payload)
    )


def _runtime_is_successful_file_read_tool_call(value: dict[Any, Any]) -> bool:
    return (
        value.get("name") == "read_file"
        and value.get("status") == "success"
        and not {"error", "error_detail", "error_message", "workflow_error"}.intersection(value)
        and _runtime_is_file_read_content_payload(
            value.get("raw_result", value.get("result"))
        )
    )


def runtime_strip_leaked_tool_activity(content: str) -> str:
    """Remove internal persisted-tool summaries if a model echoed them."""

    if not content or PREVIOUS_TOOL_ACTIVITY_MARKER not in content:
        return content or ""
    return content.split(PREVIOUS_TOOL_ACTIVITY_MARKER, 1)[0].rstrip()


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
    if error or stop_reason == "error":
        meta["stream_status"] = "error"
    error_detail = getattr(result, "error_detail", None)
    if error_detail:
        meta["limit_detail"] = error_detail
    outcome = getattr(result, "outcome", None)
    if isinstance(outcome, dict):
        meta["outcome"] = outcome
    hints = (getattr(result, "tool_evidence", None) or {}).get("capability_hints")
    if isinstance(hints, dict) and hints.get("hints"):
        meta["capability_hints"] = hints
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


def _runtime_known_tool_result_envelope(value: Any) -> tuple[str | None, Any]:
    """Return the declared outer execution state for a JSON tool envelope."""

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None, value
    if not isinstance(parsed, dict):
        return None, value

    status = str(parsed.get("status") or "").strip().lower()
    is_approval_control = (
        status == "approval_required"
        and parsed.get("approval_required") is True
    )
    if (
        parsed.get("isError") is True
        or bool(parsed.get("error"))
    ):
        return "failure", parsed
    if is_approval_control:
        return "success", parsed
    if parsed.get("ok") is False or status in _TOOL_RESULT_FAILURE_STATUSES:
        return "failure", parsed
    if status == "blocked":
        return "blocked", parsed
    if parsed.get("isError") is False or parsed.get("ok") is True:
        return "success", parsed
    if status == "completed":
        return "success", parsed
    return None, parsed


def _runtime_redact_signed_urls(value: Any) -> Any:
    if isinstance(value, str):
        return "<redacted>" if _SIGNED_URL_QUERY_RE.search(value) else value
    if isinstance(value, list):
        return [_runtime_redact_signed_urls(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _runtime_redact_signed_urls(item)
            for key, item in value.items()
        }
    return value


def _runtime_file_permission_failure_message(value: Any) -> str | None:
    """Expose an ACL denial without copying paths or private diagnostics."""
    if isinstance(value, str) and value in _PUBLIC_FILE_PERMISSION_MESSAGES:
        return value
    state, parsed = _runtime_known_tool_result_envelope(value)
    if (
        state != "failure"
        or parsed.get("error") != "file_permission_denied"
        or parsed.get("mode") != "entity_file_acl_denied"
    ):
        return None
    operation = parsed.get("operation")
    if isinstance(operation, dict) and operation.get("action") == "edit":
        return PUBLIC_FILE_EDIT_DENIED_MESSAGE
    return PUBLIC_FILE_ACCESS_DENIED_MESSAGE


def _runtime_terminal_file_permission_failure(meta: dict[str, Any], tool_calls: Any) -> str | None:
    if meta.get("error") not in {"tool_error", "stateful_tool_call_failed"}:
        return None
    if not isinstance(tool_calls, list):
        return None
    detail = meta.get("limit_detail")
    source_tool = detail.get("source_tool") if isinstance(detail, dict) else None
    # Use the latest result for the failing tool. A recovered denial must not
    # replace the cause of a later failure, nor override a successful turn.
    for call in reversed(tool_calls):
        if not isinstance(call, dict) or (source_tool and call.get("name") != source_tool):
            continue
        return _runtime_file_permission_failure_message(call.get("raw_result", call.get("result")))
    return None


def _runtime_public_blocked_tool_result(parsed: dict[str, Any]) -> str:
    """Project a blocked envelope without leaking internal tool identifiers."""

    typed = _runtime_typed_public_error_message(parsed)
    if typed is not None:
        return typed
    message = parsed.get("message")
    if isinstance(message, str):
        text = message.strip()
        if (
            text
            and text not in {
                PUBLIC_TOOL_FAILURE_MESSAGE,
                PUBLIC_REQUEST_FAILURE_MESSAGE,
            }
            and not _INTERNAL_ERROR_CODE_RE.fullmatch(text)
            and not _INTERNAL_TOOL_IDENTIFIER_RE.search(text)
            and not runtime_is_internal_failure_detail(text)
        ):
            return text
    return PUBLIC_TOOL_BLOCKED_MESSAGE


def _runtime_public_attempt_cause(value: Any, *, depth: int = 0) -> str | None:
    """Recognize bounded domain causes without publishing diagnostic values."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        for key in ("message", "error"):
            cause = _runtime_public_attempt_cause(value.get(key), depth=depth + 1)
            if cause:
                if (
                    value.get("error") == "approval_continuation_unavailable"
                    and cause == PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE
                ):
                    return PUBLIC_APPROVAL_ATTACHMENT_UNAVAILABLE_MESSAGE
                return cause
        if value.get("error") == "tool_error":
            return _runtime_public_attempt_cause(value.get("detail"), depth=depth + 1)
        return None
    if not isinstance(value, str):
        return None
    text = _TOOL_ERROR_PREFIX_RE.sub("", value).strip()
    if text.startswith("{"):
        state, parsed = _runtime_known_tool_result_envelope(text)
        return _runtime_public_attempt_cause(parsed, depth=depth + 1) if state == "failure" else None
    if text.startswith(
        "Knowledge document unavailable Save large content as a Workspace document"
    ):
        return PUBLIC_ATTACHMENT_UNAVAILABLE_MESSAGE
    if text.startswith("File not found: "):
        return "The requested file is missing or no longer accessible."
    return _PUBLIC_ATTEMPT_CAUSE_MESSAGES.get(text)


def _runtime_public_known_tool_result_envelope(value: Any) -> tuple[bool, Any]:
    """Project a success/failure result envelope before scanning its content."""

    state, parsed = _runtime_known_tool_result_envelope(value)
    if state is None:
        return False, value
    if state == "blocked":
        return True, _runtime_public_blocked_tool_result(parsed)
    if state == "failure":
        if parsed.get("kind") == "media_jobs" and isinstance(parsed.get("jobs"), list):
            # A failed media job is an observation the agent must act on. Keep
            # its identifiers and states while withholding provider diagnostics.
            public_value = {
                "kind": "media_jobs",
                "status": "failed",
                "jobs": [
                    {
                        key: job[key]
                        for key in ("job_id", "kind", "status", "document_id")
                        if key in job
                    }
                    for job in parsed["jobs"]
                    if isinstance(job, dict)
                ],
                "failed_job_ids": parsed.get("failed_job_ids", []),
                "missing_job_ids": parsed.get("missing_job_ids", []),
                "pending_job_ids": parsed.get("pending_job_ids", []),
                "retry_guidance": "Retry failed clips with another generation method or use an existing still image.",
            }
            if isinstance(value, str):
                return True, json.dumps(public_value, ensure_ascii=False)
            return True, public_value
        return True, (
            _runtime_file_permission_failure_message(parsed)
            or _runtime_typed_public_error_message(parsed)
            or _runtime_public_attempt_cause(parsed)
            or PUBLIC_TOOL_FAILURE_MESSAGE
        )

    public_value = _runtime_redact_signed_urls(
        sanitize_sensitive_payload(sanitize_approval_credentials(parsed))
    )
    if isinstance(value, str):
        if public_value == parsed:
            return True, value
        return True, json.dumps(public_value, ensure_ascii=False)
    return True, public_value


def runtime_public_tool_result(value: Any) -> Any:
    """Project a tool result for users while retaining actionable failures."""

    if value is None:
        return value
    if value == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE:
        return value
    is_known_envelope, public_envelope = _runtime_public_known_tool_result_envelope(value)
    if is_known_envelope:
        return public_envelope
    public_cause = _runtime_public_attempt_cause(value)
    if public_cause:
        return public_cause
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


def _runtime_is_website_access_denied_tool(value: dict[Any, Any]) -> bool:
    """Recognise only failed web fetches, never successful page content."""
    if value.get("name") != "web_fetch" or value.get("status") not in {"error", "failed"}:
        return False
    if value.get("reason") == "website_access_denied" and value.get("status_code") == 403:
        return True
    for key in ("result", "result_preview", "raw_result", "rawResult"):
        result = value.get(key)
        if result == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE:
            return True
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                continue
        if (
            isinstance(result, dict)
            and result.get("reason") == "website_access_denied"
            and result.get("status_code") == 403
            and bool(result.get("error"))
        ):
            return True
    return False


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

    file_read_content = _runtime_is_successful_file_read_tool_call(value)
    website_denied = _runtime_is_website_access_denied_tool(value)
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
            item = (
                sanitize_approval_credentials(item)
                if file_read_content and key == "result"
                else runtime_public_tool_result(item)
            )
        elif key in {"args", "arguments"}:
            item = sanitize_approval_credentials(item)
        projected[key] = runtime_public_tool_payload(item)
    if website_denied:
        projected = _runtime_strip_private_failure_fields(projected)
        projected["reason"] = "website_access_denied"
        projected["status_code"] = 403
    if (
        value.get("kind") == "tool"
        and not website_denied
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
    if _runtime_is_successful_file_read_tool_call(value):
        return False
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
        _runtime_known_tool_result_envelope(value[key])[0] == "failure"
        for key in result_keys
        if key in value
    ):
        return True
    if any(
        key in value
        and _runtime_known_tool_result_envelope(value[key])[0] != "success"
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
        if (
            not _runtime_is_website_access_denied_tool(value)
            and _runtime_tool_call_contains_private_failure(value, projected)
        ):
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


def runtime_response_validation_failure_content(meta: dict[str, Any] | None) -> str | None:
    """Keep validation diagnostics internal, including when reading old messages.

    Neither a rejected answer nor successful tool calls prove the user's goal was
    met. Without a validated summary, acknowledge uncertainty without inventing a
    task-specific result, user requirement, or automatic retry. Detailed evidence
    stays in the original metadata for diagnosis and in-loop correction.
    """
    if not isinstance(meta, dict) or meta.get("error") != "final_response_validation_failed":
        return None
    detail = meta.get("limit_detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    zh = str(detail.get("response_locale") or "").startswith("zh")
    return (
        "抱歉，这次还没能确认你的请求是否完成。你可以让我继续跟进。" if zh else
        "Sorry, I couldn't confirm whether your request was completed. You can ask me to follow up."
    )


def runtime_public_assistant_message_content(
    content: str | None,
    meta: dict[str, Any] | None,
    tool_calls: Any = None,
) -> str | None:
    """Return the user-facing body for a persisted assistant message."""

    message_meta = meta if isinstance(meta, dict) else {}
    validation_failure = runtime_response_validation_failure_content(message_meta)
    if validation_failure is not None:
        return validation_failure
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
        permission_message = _runtime_terminal_file_permission_failure(message_meta, tool_calls)
        if permission_message is not None:
            return permission_message
        if str(content or "").strip() in _PUBLIC_FILE_PERMISSION_MESSAGES:
            return str(content).strip()
        # The BYOK sentence is the curated public body; keep its structured
        # detail instead of collapsing the row to its bare error code.
        byok_message = _runtime_byok_failure_projection(str(content or "").strip())
        if byok_message is not None:
            return byok_message
        limit_detail = message_meta.get("limit_detail") or {}
        if not isinstance(limit_detail, dict):
            limit_detail = {}
        public_body = str(public_content or "").strip()
        typed_message = (
            _runtime_typed_public_error_message(limit_detail.get("reason"))
            or _runtime_typed_public_error_message(limit_detail)
            or _runtime_typed_public_error_message(message_meta.get("error"))
            or _runtime_typed_public_error_message(str(content or "").strip())
            or _runtime_typed_public_error_message(public_body)
        )
        keepable_summary = (
            public_body
            and public_body not in {
                PUBLIC_TOOL_FAILURE_MESSAGE,
                PUBLIC_REQUEST_FAILURE_MESSAGE,
                "tool_error",
            }
            and not _INTERNAL_ERROR_CODE_RE.fullmatch(public_body)
            and not runtime_is_internal_failure_detail(public_body)
            and _runtime_typed_public_error_message(public_body) is None
        )
        if (
            (
                message_meta.get("error") == "tool_error"
                or limit_detail.get("reason") == "tool_outcome_unconfirmed"
            )
            and keepable_summary
        ):
            return public_body
        if typed_message is not None:
            return typed_message
        error_detail = (
            message_meta.get("error_message")
            or message_meta.get("error")
            or public_content
        )
        return runtime_assistant_stream_error_content(str(error_detail))
    return public_content if content is not None else None


def runtime_visitor_assistant_message_content(
    content: str | None,
    meta: dict[str, Any] | None,
    tool_calls: Any = None,
) -> str | None:
    """Body for anonymous visitors: an owner's provider failure is not theirs to fix."""

    public_content = runtime_public_assistant_message_content(content, meta, tool_calls)
    if public_content is not None and _runtime_byok_failure_projection(public_content) is not None:
        return PUBLIC_REQUEST_FAILURE_MESSAGE
    return public_content


def runtime_assistant_stream_error_content(error_message: str) -> str:
    message = str(
        redact_sensitive_text(error_message or "Unknown error") or "Unknown error"
    ).strip()
    if message in _PUBLIC_FILE_PERMISSION_MESSAGES:
        return message
    typed_message = _runtime_typed_public_error_message(message)
    if typed_message is not None:
        return typed_message
    if message in {"llm_configuration_missing", PUBLIC_LLM_CONFIGURATION_MESSAGE}:
        return PUBLIC_LLM_CONFIGURATION_MESSAGE
    if message in {MODEL_OUTPUT_LIMIT_ERROR_CODE, PUBLIC_MODEL_OUTPUT_LIMIT_MESSAGE}:
        return PUBLIC_MODEL_OUTPUT_LIMIT_MESSAGE
    byok_message = _runtime_byok_failure_projection(message)
    if byok_message is not None:
        return byok_message
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
