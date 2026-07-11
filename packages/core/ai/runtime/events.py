from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class RuntimeEvent:
    type: RuntimeEventType
    data: dict[str, Any] = field(default_factory=dict)
