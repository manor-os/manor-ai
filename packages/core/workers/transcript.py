"""Worker transcript checkpoints — resume a failed subagent attempt.

A subagent attempt that fails after the loop returned (contract violation,
submitted failure, empty output, an approval pause) used to lose its whole
transcript: the next attempt re-ran the prompt from scratch and repeated every
tool call that had already succeeded. The checkpoint built here is parked on
``execution_steps.runtime_checkpoint`` by the dispatcher (``fail_lease`` /
``complete_lease`` / ``lease_needs_human``) and replayed by the next attempt
as ``initial_messages`` with the failure feedback appended as a new user turn.

This is deliberately NOT a ``RuntimeAgentCheckpoint``: that one resumes at a
pending tool call (``request_input``) and is owned by the agentic loop. A
transcript checkpoint has no pending call — it is a conversation to continue.
The two are told apart by ``schema_version``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from packages.core.constants.execution import (
    WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES,
    WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA,
)

#: Per-message content cap before the whole-checkpoint budget applies.
MESSAGE_CONTENT_MAX_CHARS = 8 * 1024

_DROPPED_TOOL_RESULT_MARKER = "[tool result dropped to fit the retry checkpoint budget]"


def _message_text(content: Any) -> str:
    """Flatten any message content to text (multimodal parts lose their bytes)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
                    continue
                image_url = item.get("image_url")
                if isinstance(image_url, dict) and image_url.get("url"):
                    parts.append("[image]")
                    continue
            elif item is not None:
                parts.append(str(item))
        return " ".join(part for part in parts if part).strip()
    return str(content)


def _truncate(text: str, limit: int = MESSAGE_CONTENT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n[... truncated {dropped} chars ...]"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    compact = dict(message)
    compact["content"] = _truncate(_message_text(message.get("content")))
    return compact


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("id"):
            ids.append(str(call["id"]))
    return ids


def _without_dangling_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cut the transcript before the first assistant tool call that never got
    its tool result — replaying that to a provider is a request error."""
    answered: set[str] = {
        str(message.get("tool_call_id"))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        if any(call_id not in answered for call_id in _tool_call_ids(message)):
            return messages[:index]
    return messages


def _serialized_size(checkpoint: dict[str, Any]) -> int:
    return len(json.dumps(checkpoint, ensure_ascii=False).encode("utf-8"))


def build_worker_transcript_checkpoint(
    messages: list[dict[str, Any]] | None,
    *,
    rounds: int | None,
    usage: dict[str, Any] | None,
    tool_calls_made: list[str] | None,
    terminal_failure: str | None = None,
) -> dict[str, Any] | None:
    """Compact ``messages`` into a JSONB-safe checkpoint, or ``None``.

    * a leading ``system`` message is dropped (the loop prepends its own);
    * every message's content is capped at ``MESSAGE_CONTENT_MAX_CHARS``;
    * if the whole thing still exceeds ``WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES``
      the OLDEST tool results are replaced by a marker until it fits;
    * ``None`` when nothing usable remains (no messages, or it cannot fit).
    """
    compact = [
        _compact_message(message)
        for message in (messages or [])
        if isinstance(message, dict) and message.get("role")
    ]
    while compact and compact[0].get("role") == "system":
        compact.pop(0)
    compact = _without_dangling_tool_calls(compact)
    if not compact:
        return None

    checkpoint: dict[str, Any] = {
        "schema_version": WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA,
        "messages": _json_safe(compact),
        "rounds": max(0, int(rounds or 0)),
        "usage": _json_safe(dict(usage or {})),
        "tool_calls_made": [str(item) for item in (tool_calls_made or [])],
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    if terminal_failure == "tool_outcome_unconfirmed":
        # Resume must not turn a terminal, possibly effectful error into a
        # fresh writable model round or a successful max_rounds result.
        checkpoint["terminal_failure"] = terminal_failure
    budget = WORKER_TRANSCRIPT_CHECKPOINT_MAX_BYTES
    if _serialized_size(checkpoint) <= budget:
        return checkpoint
    for message in checkpoint["messages"]:
        if message.get("role") != "tool" or message.get("content") == _DROPPED_TOOL_RESULT_MARKER:
            continue
        message["content"] = _DROPPED_TOOL_RESULT_MARKER
        if _serialized_size(checkpoint) <= budget:
            return checkpoint
    return None


def is_worker_transcript_checkpoint(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
        and isinstance(value.get("messages"), list)
        and bool(value["messages"])
    )


def worker_transcript_resume_messages(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages to replay as ``initial_messages`` — never a system one."""
    return [
        dict(message)
        for message in (checkpoint.get("messages") or [])
        if isinstance(message, dict) and message.get("role") != "system"
    ]


def worker_transcript_dropped_tool_result_ids(
    checkpoint: dict[str, Any] | None,
) -> set[str]:
    """Return tool-call ids whose result body was discarded for the budget.

    The marker keeps a provider-valid assistant/tool message pair, but it is
    not enough replay authority for a mutating call: the next model turn no
    longer sees the provider's terminal postcondition and may repeat it.
    """

    if not is_worker_transcript_checkpoint(checkpoint):
        return set()
    return {
        str(message.get("tool_call_id"))
        for message in checkpoint.get("messages") or []
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and message.get("content") == _DROPPED_TOOL_RESULT_MARKER
        and message.get("tool_call_id")
    }


__all__ = [
    "MESSAGE_CONTENT_MAX_CHARS",
    "build_worker_transcript_checkpoint",
    "is_worker_transcript_checkpoint",
    "worker_transcript_dropped_tool_result_ids",
    "worker_transcript_resume_messages",
]
