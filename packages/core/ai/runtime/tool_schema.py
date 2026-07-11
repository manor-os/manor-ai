from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def _normalize_allowed_tool_names(
    allowed_tool_names: Iterable[str] | None,
) -> frozenset[str] | None:
    if allowed_tool_names is None:
        return None
    return frozenset(
        str(tool_name)
        for tool_name in allowed_tool_names
        if str(tool_name or "").strip()
    )


def runtime_tool_schema_resolver(
    *,
    get_schema: Callable[[str], dict[str, Any] | None],
    allowed_tool_names: Iterable[str] | None = None,
) -> Callable[[str], dict[str, Any] | None]:
    """Build the deferred tool-schema resolver for one runtime turn.

    This is the schema-side counterpart to RuntimeEnvelope.allowed_tool_names:
    a dynamically discovered tool can only become visible if it is still in the
    resolved runtime surface for the current turn.
    """

    allowed = _normalize_allowed_tool_names(allowed_tool_names)

    def _resolver(name: str) -> dict[str, Any] | None:
        tool_name = str(name or "").strip()
        if not tool_name:
            return None
        if allowed is not None and tool_name not in allowed:
            return None
        return get_schema(tool_name)

    return _resolver
