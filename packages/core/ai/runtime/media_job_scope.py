"""Media jobs started by a host that parks instead of waiting in process.

A heavy worker normally runs a media job inside its own event loop, so a
Workflow waiting on that job cannot deadlock behind it in the shared queue.
``run_in_worker`` drains those in-loop jobs before the loop closes, so the
worker stays occupied until they finish. A Task agent loop parks instead of
waiting: holding the heavy slot after it parks (and past the Celery time
limit) defeats parking. For the calls that loop makes directly, the media
schedulers therefore dispatch the job through Celery, and the job carries
the Run it belongs to so its completion can wake that Run.

Any agent loop or workflow run nested inside it (a prompt skill, a delegated
agent, a workflow and its agent nodes) waits for its own jobs in process, so
it is never detached: queueing its job on the shared heavy queue could leave
that blocking wait behind its own worker.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

from packages.core.ai.runtime.execution_nesting import runtime_execution_nesting_depth

# (host identity, the nesting depth of the host's own loop)
_DETACHED_MEDIA_JOBS: contextvars.ContextVar[tuple[dict[str, str], int] | None] = contextvars.ContextVar(
    "runtime_media_jobs_detached", default=None,
)


def runtime_media_jobs_detached() -> dict[str, str] | None:
    """The parking host's identity ({"task_id", "run_id"}) for a call its own
    loop makes directly; None anywhere else."""

    scope = _DETACHED_MEDIA_JOBS.get()
    if scope is None or runtime_execution_nesting_depth() != scope[1]:
        return None
    return dict(scope[0])


@contextmanager
def runtime_media_jobs_detached_scope(*, task_id: str, run_id: str) -> Iterator[None]:
    """Detach the media jobs of the agent loop entered directly inside this scope."""

    token = _DETACHED_MEDIA_JOBS.set(
        ({"task_id": str(task_id), "run_id": str(run_id)}, runtime_execution_nesting_depth() + 1),
    )
    try:
        yield
    finally:
        _DETACHED_MEDIA_JOBS.reset(token)


def runtime_media_job_lineage(task_id: str | None, tool_kwargs: dict[str, Any] | None) -> dict[str, str]:
    """Job params that name the parked Run and the tool call that started it.

    Only a job of the scope's own Task is attributed to its Run; outside a
    parking host a job's params are unchanged.
    """

    scope = runtime_media_jobs_detached()
    if scope is None or not task_id or scope.get("task_id") != str(task_id):
        return {}
    lineage = {"task_coordinator_run_id": scope["run_id"]}
    call_id = str((tool_kwargs or {}).get("_runtime_tool_call_id_from_context") or "").strip()
    if call_id:
        lineage["tool_call_id"] = call_id[:200]
    return lineage
