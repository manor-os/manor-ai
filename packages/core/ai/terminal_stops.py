"""Stop reasons that mean "a terminal tool deliberately ended the loop".

The agentic loop can be armed with a ``terminal_tool_result_policy``: when a
matching tool result appears, the loop stops on purpose and reports the
policy's own ``stop_reason`` (``skill_terminal``, ``local_coding_dispatched``,
…). These are SUCCESS terminators — the mechanism working as designed — so
callers that gate on ``stop_reason == "completed"`` must not read them as
failures. This module is the single place those terminators are declared.
"""
from __future__ import annotations

from typing import Any

# Built-in terminal-tool stop reasons (declared here, used by their owners).
SKILL_TERMINAL_STOP_REASON = "skill_terminal"
MEDIA_GENERATION_TERMINAL_STOP_REASON = "media_generation_tool_result"
LOCAL_CODING_DISPATCHED_STOP_REASON = "local_coding_dispatched"

_TERMINAL_SUCCESS_STOP_REASONS: set[str] = {
    SKILL_TERMINAL_STOP_REASON,
    MEDIA_GENERATION_TERMINAL_STOP_REASON,
    LOCAL_CODING_DISPATCHED_STOP_REASON,
}


def is_terminal_success_stop_reason(stop_reason: Any) -> bool:
    return str(stop_reason or "").strip() in _TERMINAL_SUCCESS_STOP_REASONS

