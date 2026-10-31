"""Tell long-running Celery work that its worker is shutting down.

A warm shutdown (SIGTERM during a rollout or scale-down) stops the worker from
taking new messages but lets the task it is running continue until the pod's
termination grace runs out. A task that drives many units of work in one
delivery, such as a Workflow run executing node after node, should stop at
its next safe boundary and hand the rest to another worker instead of starting
a unit the shutdown can kill halfway.

The flag lives in anonymous shared memory created in the worker's main
process before the pool forks, so pool children inherit it and see the main
process set it from its signal handler. Outside a Celery worker (API, tests,
scripts) nothing is registered and no drain is ever reported.
"""
from __future__ import annotations

import multiprocessing
from typing import Any

_drain_flag: Any = None


def register_worker_main_process() -> None:
    """Create the shared flag in the worker main process, before the pool forks."""

    global _drain_flag
    _drain_flag = multiprocessing.RawValue("B", 0)


def mark_worker_draining() -> None:
    """Record that this worker is shutting down (safe inside a signal handler)."""

    if _drain_flag is not None:
        _drain_flag.value = 1


def worker_drain_requested() -> bool:
    """Whether the worker running this code has begun shutting down."""

    return _drain_flag is not None and bool(_drain_flag.value)


__all__ = [
    "mark_worker_draining",
    "register_worker_main_process",
    "worker_drain_requested",
]
