"""Eligibility and caller-input sanitization for trusted Chrome fast mode."""
from __future__ import annotations

from typing import Any


RUNTIME_CHROME_INTERACTION_MODE_ARGUMENT = (
    "_runtime_chrome_interaction_mode_from_context"
)

_CHROME_FAST_MODE_TOOLS = frozenset(
    {
        "mcp__chrome__click_element",
        "mcp__chrome__fill_or_select",
    }
)
_MODEL_INTERACTION_MODE_ARGUMENTS = frozenset(
    {
        "interaction_mode",
        "interactionMode",
        "__manorRuntimeInteractionMode",
        RUNTIME_CHROME_INTERACTION_MODE_ARGUMENT,
        "__manorRuntimeSequenceChild",
        "_runtime_chrome_sequence_child_from_context",
    }
)
_SEMANTIC_IDENTITY_KEYS = ("name", "text", "testId", "test_id", "href")
_DISALLOWED_TARGET_KEYS = frozenset(
    {
        "selector",
        "ref",
        "node_id",
        "nodeId",
        "coordinates",
        "x",
        "y",
        "scope",
        "scopeRef",
        "scope_ref",
        "frame",
        "frameId",
        "frame_id",
        "frameRef",
        "frame_ref",
        "frameRefs",
        "fallback",
        "fallbackReason",
        "fallback_reason",
        "visual",
        "visualFallback",
        "visual_fallback",
        "visual_fallback_recommended",
    }
)


def runtime_strip_model_chrome_interaction_mode(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Remove all caller-controlled Chrome interaction mode fields."""

    if not str(tool_name or "").startswith("mcp__chrome__"):
        return dict(arguments)
    return {
        key: value
        for key, value in arguments.items()
        if key not in _MODEL_INTERACTION_MODE_ARGUMENTS
    }


def runtime_chrome_fast_interaction_mode(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    workspace_id: str | None,
) -> str | None:
    """Return ``fast`` only for trusted exact semantic actions."""

    if tool_name not in _CHROME_FAST_MODE_TOOLS:
        return None

    if any(key in arguments for key in _DISALLOWED_TARGET_KEYS):
        return None
    target = arguments.get("target")
    if not isinstance(target, dict) or target.get("exact") is not True:
        return None
    if any(key in target for key in _DISALLOWED_TARGET_KEYS):
        return None
    if not _nonempty_string(target.get("role")):
        return None
    if not any(_nonempty_string(target.get(key)) for key in _SEMANTIC_IDENTITY_KEYS):
        return None
    return "fast"


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
