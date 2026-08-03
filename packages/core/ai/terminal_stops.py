"""Stop reasons that mean "a terminal tool deliberately ended the loop".

The agentic loop can be armed with a ``terminal_tool_result_policy``: when a
matching tool result appears, the loop stops on purpose and reports the
policy's own ``stop_reason`` (``submit_result``, ``skill_terminal``,
``local_coding_dispatched``, …). These are SUCCESS terminators — the mechanism
working as designed — but any caller that gates on ``stop_reason == "completed"``
reads them as failures.

That is exactly how every compliant subagent step started failing: the step
prompt orders the model to finish by calling ``submit_result``, the loop then
stops with ``stop_reason="submit_result"``, and the worker raised
``RuntimeError("subagent stopped with submit_result: Result submitted.")``.

This module is the single place those terminators are declared, so a new
terminal policy never has to be re-spelled at every stop_reason call site.
It intentionally has no imports beyond typing: both the loop layer and the
worker layer depend on it.
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


def register_terminal_success_stop_reason(stop_reason: str) -> str:
    """Declare a stop reason produced by a terminal-tool policy.

    Modules that own a policy (e.g. ``workers/submit_result.py``) register
    their reason at import time and keep owning the constant.
    """
    reason = str(stop_reason or "").strip()
    if reason:
        _TERMINAL_SUCCESS_STOP_REASONS.add(reason)
    return reason


def terminal_success_stop_reasons() -> frozenset[str]:
    """Snapshot of every registered terminal-tool success stop reason."""
    return frozenset(_TERMINAL_SUCCESS_STOP_REASONS)


def is_terminal_success_stop_reason(stop_reason: Any) -> bool:
    return str(stop_reason or "").strip() in _TERMINAL_SUCCESS_STOP_REASONS


def is_terminal_tool_success(result: Any) -> bool:
    """True when an agentic-loop result stopped on a terminal tool.

    Two signals, most authoritative first:

    1. the result carries the ``control`` block the loop attaches whenever a
       terminal-tool policy fires (``{"terminal": True, ...}``). This covers
       *any* policy, including skill-configured ones whose ``stop_reason`` is
       an arbitrary string nobody could have registered in advance;
    2. the stop reason is a registered terminal success — for results rebuilt
       or serialized without the control block.
    """
    control = getattr(result, "control", None)
    if isinstance(control, dict) and control.get("terminal"):
        return True
    return is_terminal_success_stop_reason(getattr(result, "stop_reason", None))
