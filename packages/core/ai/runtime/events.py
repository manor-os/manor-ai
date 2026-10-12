from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


RuntimeEventType = Literal[
    "runtime_start",
    "runtime_end",
    "tool_start",
    "tool_end",
    "tool_denied",
    "approval_required",
    "skill_start",
    "skill_end",
    "subagent_start",
    "subagent_end",
    "subagent_denied",
    "error",
]


class RuntimeToolResultStatus(str, Enum):
    """Closed completion vocabulary for persisted Runtime tool evidence."""

    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def parse(cls, value: Any) -> "RuntimeToolResultStatus | None":
        normalized = str(value or "").strip().lower()
        # ``sandbox_ready`` is the skill handoff: the call completed by
        # binding a sandbox for the follow-up tool calls. Persisting it as a
        # failure hid a working path behind a red execution-log row.
        if normalized in {"completed", "complete", "succeeded", "success", "ok", "sandbox_ready"}:
            return cls.COMPLETED
        if normalized in {"failed", "failure", "error", "cancelled", "canceled"}:
            return cls.FAILED
        return None

    @property
    def is_successful_completion(self) -> bool:
        return self is RuntimeToolResultStatus.COMPLETED


@dataclass(frozen=True)
class RuntimeEvent:
    type: RuntimeEventType
    data: dict[str, Any] = field(default_factory=dict)
