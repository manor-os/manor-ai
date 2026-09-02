"""Typed internal control results for durable Runtime execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class RuntimeTurnAborted(ValueError):
    """Stop a stale/invalid turn unless a different tool path remains safe.

    ``allow_alternate_path`` is deliberately opt-in.  Runtime callers may set
    it only when they know the failure happened before external I/O, so the
    agent loop can keep working without repeating the failed approved action.
    """

    def __init__(
        self,
        message: str,
        *,
        allow_alternate_path: bool = False,
    ) -> None:
        super().__init__(message)
        self.allow_alternate_path = allow_alternate_path


@dataclass(frozen=True)
class RuntimeToolSuspension:
    """A tool-boundary wait that must never be shown to the model as an error."""

    kind: str
    reservation_id: str
    poll_after_seconds: int
    deadline_at: datetime | str
    reason: str = "sandbox_capacity"

    def to_dict(self) -> dict[str, Any]:
        deadline = (
            self.deadline_at.isoformat()
            if isinstance(self.deadline_at, datetime)
            else str(self.deadline_at)
        )
        return {
            "kind": self.kind,
            "reservation_id": self.reservation_id,
            "poll_after_seconds": self.poll_after_seconds,
            "deadline_at": deadline,
            "reason": self.reason,
        }


@dataclass
class RuntimeAgentCheckpoint:
    """Serializable state needed to resume at one pending tool call."""

    messages: list[dict[str, Any]]
    usage: dict[str, Any]
    rounds: int
    tool_calls_made: list[str]
    pending_tool_call: dict[str, Any]
    remaining_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_attempt: int = 1
    seen_tool_result_digests: dict[str, str] = field(default_factory=dict)
    disable_followup_tools: bool = False
    tool_continuations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "messages": [dict(message) for message in self.messages],
            "usage": dict(self.usage),
            "rounds": self.rounds,
            "tool_calls_made": list(self.tool_calls_made),
            "pending_tool_call": dict(self.pending_tool_call),
            "remaining_tool_calls": [dict(item) for item in self.remaining_tool_calls],
            "pending_attempt": self.pending_attempt,
            "seen_tool_result_digests": dict(self.seen_tool_result_digests),
            "disable_followup_tools": self.disable_followup_tools,
            "tool_continuations": {
                str(key): dict(item)
                for key, item in self.tool_continuations.items()
                if isinstance(item, dict)
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeAgentCheckpoint":
        schema_version = int(value.get("schema_version") or 0)
        if schema_version not in {1, 2}:
            raise ValueError("unsupported Runtime checkpoint schema")
        pending = value.get("pending_tool_call")
        if not isinstance(pending, dict) or not pending.get("id") or not pending.get("name"):
            raise ValueError("Runtime checkpoint is missing pending tool identity")
        return cls(
            messages=[dict(item) for item in value.get("messages") or []],
            usage=dict(value.get("usage") or {}),
            rounds=max(0, int(value.get("rounds") or 0)),
            tool_calls_made=[str(item) for item in value.get("tool_calls_made") or []],
            pending_tool_call=dict(pending),
            remaining_tool_calls=[
                dict(item) for item in value.get("remaining_tool_calls") or []
                if isinstance(item, dict)
            ],
            pending_attempt=max(1, int(value.get("pending_attempt") or 1)),
            seen_tool_result_digests={
                str(key): str(item)
                for key, item in dict(value.get("seen_tool_result_digests") or {}).items()
            },
            disable_followup_tools=(
                bool(value.get("disable_followup_tools"))
                if schema_version >= 2
                else False
            ),
            tool_continuations=(
                {
                    str(key): dict(item)
                    for key, item in dict(value.get("tool_continuations") or {}).items()
                    if isinstance(item, dict)
                }
                if schema_version >= 2
                else {}
            ),
        )


def is_runtime_tool_suspension(value: Any) -> bool:
    return isinstance(value, RuntimeToolSuspension)


__all__ = [
    "RuntimeAgentCheckpoint",
    "RuntimeToolSuspension",
    "RuntimeTurnAborted",
    "is_runtime_tool_suspension",
]
