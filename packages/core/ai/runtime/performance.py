"""Privacy-safe, additive runtime performance telemetry."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from typing import Any


logger = logging.getLogger(__name__)

PERFORMANCE_PHASES = frozenset({
    "browser_operation",
    "file_transfer",
    "youtube_wait",
    "final_publish",
    "model_thinking",
})
PERFORMANCE_ROUTES = frozenset({
    "authorization_rejection",
    "capability_rejection",
    "live_local",
    "live_redis",
    "redis_pre_dispatch_fallback",
    "worklease",
    "model_provider",
    "unknown",
})
PERFORMANCE_OUTCOMES = frozenset({
    "success",
    "failure",
    "rejected",
    "timeout",
    "uncertain",
    "fallback",
})

_FILE_TRANSFER_TOOLS = frozenset({"upload", "download", "wait_download"})
_FINAL_ACTION_LABELS = frozenset({"save", "publish"})
_FINAL_ACTION_TARGET_KEYS = ("name", "text", "label", "ariaLabel", "aria-label")
_YOUTUBE_WAIT_TEXTS = frozenset({
    "checks complete. no issues found.",
    "select files",
    "upload videos",
})
_YOUTUBE_WAIT_TEXT_KEYS = ("text", "textContains", "text_contains")
_MAX_ID_CHARS = 160


def _bounded_id(value: Any) -> str:
    return str(value or "").strip()[:_MAX_ID_CHARS]


def _request_id_digest(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
    except Exception:
        canonical = str(value)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _non_negative_duration(value: Any) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(resolved) or resolved <= 0:
        return 0.0
    return round(resolved, 3)


def runtime_performance_run_id(
    *,
    performance_run_id: Any = None,
    runtime_run_id: Any = None,
    workflow_run_id: Any = None,
    chat_trace_id: Any = None,
    task_id: Any = None,
) -> str:
    """Resolve the shared performance identity without changing runtime lineage."""

    for value in (
        performance_run_id,
        runtime_run_id,
        workflow_run_id,
        chat_trace_id,
        task_id,
    ):
        resolved = _bounded_id(value)
        if resolved:
            return resolved
    return ""


def _tool_leaf(tool_name: Any) -> str:
    return str(tool_name or "").strip().rsplit("__", 1)[-1].lower()


def _contains_youtube_reference(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.casefold()
        return "youtube.com" in normalized or "youtu.be" in normalized
    if isinstance(value, Mapping):
        return any(_contains_youtube_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_youtube_reference(item) for item in value)
    return False


def _is_exact_normalized(value: Any, expected: frozenset[str]) -> bool:
    return isinstance(value, str) and value.strip().casefold() in expected


def _exact_youtube_wait_signal(arguments: Mapping[str, Any]) -> bool:
    return any(
        _is_exact_normalized(arguments.get(key), _YOUTUBE_WAIT_TEXTS)
        for key in _YOUTUBE_WAIT_TEXT_KEYS
    )


def _exact_final_action_target(arguments: Mapping[str, Any]) -> bool:
    if any(
        _is_exact_normalized(arguments.get(key), _FINAL_ACTION_LABELS)
        for key in ("label", "text")
    ):
        return True
    target = arguments.get("target")
    if isinstance(target, str):
        return _is_exact_normalized(target, _FINAL_ACTION_LABELS)
    if not isinstance(target, Mapping):
        return False
    return any(
        _is_exact_normalized(target.get(key), _FINAL_ACTION_LABELS)
        for key in _FINAL_ACTION_TARGET_KEYS
    )


def runtime_browser_phase(
    tool_name: Any,
    arguments: Mapping[str, Any] | None = None,
) -> str:
    """Classify a Browser call while keeping inspected arguments private."""

    tool = _tool_leaf(tool_name)
    private_arguments = arguments if isinstance(arguments, Mapping) else {}
    if tool in _FILE_TRANSFER_TOOLS:
        return "file_transfer"
    if tool == "wait" and (
        _contains_youtube_reference(private_arguments)
        or _exact_youtube_wait_signal(private_arguments)
    ):
        return "youtube_wait"
    if tool == "click_element" and _exact_final_action_target(private_arguments):
        return "final_publish"
    return "browser_operation"


def runtime_json_bytes(value: Any) -> int:
    """Measure a JSON-compatible value without returning or logging its content."""

    try:
        return len(
            json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except Exception:
        return 0


def log_runtime_performance(
    *,
    request_id: Any,
    run_id: Any,
    tool_call_id: Any,
    phase: str,
    route: str,
    outcome: str,
    duration_ms: Any,
    request_bytes: Any,
    response_bytes: Any,
    runtime_validation_duration_ms: Any = 0,
    api_websocket_duration_ms: Any = 0,
    redis_relay_duration_ms: Any = 0,
    response_return_duration_ms: Any = 0,
    gateway_duration_ms: Any = 0,
    extension_duration_ms: Any = 0,
    llm_round_count: Any = 0,
    chrome_call_count: Any = 0,
    read_page_count: Any = 0,
    worklease_count: Any = 0,
) -> None:
    """Emit only identifiers, fixed enums, counts, bytes, and duration."""

    payload = {
        "event": "runtime_performance",
        "request_id": _request_id_digest(request_id),
        "run_id": _bounded_id(run_id),
        "tool_call_id": _bounded_id(tool_call_id),
        "phase": phase if phase in PERFORMANCE_PHASES else "browser_operation",
        "route": route if route in PERFORMANCE_ROUTES else "unknown",
        "outcome": outcome if outcome in PERFORMANCE_OUTCOMES else "failure",
        "duration_ms": _non_negative_duration(duration_ms),
        "runtime_validation_duration_ms": _non_negative_duration(
            runtime_validation_duration_ms
        ),
        "api_websocket_duration_ms": _non_negative_duration(
            api_websocket_duration_ms
        ),
        "redis_relay_duration_ms": _non_negative_duration(redis_relay_duration_ms),
        "response_return_duration_ms": _non_negative_duration(
            response_return_duration_ms
        ),
        "gateway_duration_ms": _non_negative_duration(gateway_duration_ms),
        "extension_duration_ms": _non_negative_duration(extension_duration_ms),
        "request_bytes": _non_negative_int(request_bytes),
        "response_bytes": _non_negative_int(response_bytes),
        "llm_round_count": _non_negative_int(llm_round_count),
        "chrome_call_count": _non_negative_int(chrome_call_count),
        "read_page_count": _non_negative_int(read_page_count),
        "worklease_count": _non_negative_int(worklease_count),
    }
    logger.info("runtime_performance %s", json.dumps(payload, sort_keys=True))


def log_model_performance(
    *,
    run_id: Any,
    tool_call_id: Any,
    outcome: str,
    duration_ms: Any,
    request_bytes: Any,
    response_bytes: Any,
) -> None:
    """Record one actual model-provider round."""

    log_runtime_performance(
        request_id=tool_call_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        phase="model_thinking",
        route="model_provider",
        outcome=outcome,
        duration_ms=duration_ms,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        llm_round_count=1,
    )
