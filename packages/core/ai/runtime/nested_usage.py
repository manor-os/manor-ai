from __future__ import annotations

import contextvars
from collections.abc import Iterable
from typing import Any


_nested_usage_collector: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("runtime_nested_usage_collector", default=None)
)


def runtime_begin_nested_usage_collection() -> contextvars.Token:
    """Start a child-usage scope for one agentic loop invocation."""

    return _nested_usage_collector.set([])


def runtime_record_nested_usage(usage: dict[str, Any] | None) -> None:
    """Attach completed child-loop usage to the current parent loop."""

    collector = _nested_usage_collector.get()
    if collector is not None and isinstance(usage, dict) and usage:
        collector.append(dict(usage))


def runtime_finish_nested_usage_collection(
    token: contextvars.Token,
) -> tuple[dict[str, Any], ...]:
    """Return usage collected in this scope and restore the parent scope."""

    collected: Iterable[dict[str, Any]] = _nested_usage_collector.get() or ()
    result = tuple(dict(usage) for usage in collected)
    _nested_usage_collector.reset(token)
    return result
