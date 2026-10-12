"""How deeply the current call is nested in agent loops and workflow runs.

Every agent loop (``runtime_execute_agentic_loop``) and every inline
workflow run is one level. A host can scope a behavior to the calls its own
loop makes directly, never to a loop or workflow nested inside it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_DEPTH: ContextVar[int] = ContextVar("runtime_execution_nesting_depth", default=0)


def runtime_execution_nesting_depth() -> int:
    return _DEPTH.get()


@contextmanager
def runtime_nested_execution_scope() -> Iterator[None]:
    """One more nesting level, restored on exit (also when the body raises)."""

    token = _DEPTH.set(_DEPTH.get() + 1)
    try:
        yield
    finally:
        _DEPTH.reset(token)
