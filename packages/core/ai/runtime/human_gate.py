"""Shared blocking-human-gate semantics for every Runtime agent loop.

Approval-shaped tool results are control flow, not model observations.  This
module is deliberately independent of either loop implementation so Chat,
Skills, scheduled Tasks, and future Runtime drivers cannot drift on what must
stop execution.
"""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.control import runtime_sequence_child_owner
from packages.core.ai.runtime.provider_approvals import normalize_provider_approval
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.constants.hitl_envelope import parse_hitl_envelope


_SERIAL_BROWSER_TOOL_PREFIXES = ("mcp__chrome__",)


def _json_object(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    text = str(result or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_safe(result: Any) -> Any:
    """Preserve a blocking result's JSON type, falling back to text."""

    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)
    return result


def runtime_detect_blocking_human_gate(
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: Any,
) -> dict[str, Any] | None:
    """Normalize every trusted approval representation into one stop control.

    The leaf ``blocking_result`` is preserved because the owning surface needs
    the original Manor HITL envelope or provider continuation.  A nested Prompt
    Skill crosses the tool boundary through a host-authored wrapper; it must not
    mint or masquerade as a second approval request.
    """

    normalized_name = str(tool_name or "").strip()
    normalized_arguments = arguments if isinstance(arguments, dict) else {}

    sequence_owner = runtime_sequence_child_owner(normalized_name, result)
    if sequence_owner is not None:
        child_control = runtime_detect_blocking_human_gate(
            sequence_owner["tool_name"],
            sequence_owner["arguments"],
            sequence_owner["raw_result"],
        )
        if child_control is None:
            return None
        return {
            **child_control,
            "tool_name": sequence_owner["tool_name"],
            "blocking_result": _json_safe(result),
        }

    if normalized_name == "invoke_skill":
        payload = _json_object(result)
        nested_control = (
            payload.get("control")
            if isinstance(payload, dict) and isinstance(payload.get("control"), dict)
            else None
        )
        if (
            isinstance(payload, dict)
            and str(payload.get("stop_reason") or payload.get("status") or "").strip()
            == "hitl_required"
            and isinstance(nested_control, dict)
            and nested_control.get("kind") == "approval_required"
            and str(nested_control.get("tool_name") or "").strip()
            and "blocking_result" in nested_control
        ):
            return {
                "kind": "approval_required",
                "tool_name": str(nested_control["tool_name"]),
                "blocking_result": _json_safe(nested_control.get("blocking_result")),
            }

    if parse_hitl_envelope(result) is None:
        try:
            provider_approval = normalize_provider_approval(
                normalized_name,
                normalized_arguments,
                result,
            )
        except Exception:
            provider_approval = None
        if provider_approval is None:
            return None

    return {
        "kind": "approval_required",
        "tool_name": normalized_name,
        "blocking_result": _json_safe(result),
    }


def runtime_tool_requires_serial_execution(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    """Serialize every call not proven read-only.

    A gate is only discovered at the execution boundary.  Parallelizing a
    mutation-shaped or unknown sibling could therefore let it overtake the
    first approval request.  Classification failures intentionally fail closed.
    """

    name = str(tool_name or "")
    if (
        name == "invoke_skill"
        # request_input parks the whole turn on a human answer; tool calls
        # issued alongside it must not fire side effects past the wait.
        or name == "request_input"
        or name.startswith("ws_")
        or name.startswith("sandbox_")
        or name.startswith(_SERIAL_BROWSER_TOOL_PREFIXES)
        or name.endswith("__browser_action")
    ):
        return True
    try:
        classification = classify_runtime_tool(
            name,
            arguments if isinstance(arguments, dict) else {},
        )
    except Exception:
        return True
    return classification.effect is not RuntimeToolEffect.READ_ONLY


__all__ = [
    "runtime_detect_blocking_human_gate",
    "runtime_tool_requires_serial_execution",
]
