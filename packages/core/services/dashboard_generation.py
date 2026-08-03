"""In-process job registry for asynchronous Dashboard module generation.

A generation turn (agent + validation + submit) routinely outlives a browser
request timeout, so the API starts it as a background asyncio task and the
frontend polls for the result. The API runs as a single uvicorn process in
every deployment, so an in-process registry is sufficient; jobs do not
survive a process restart, which is acceptable for a preview-only flow.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

DASHBOARD_GENERATION_TIMEOUT_SECONDS = 480.0
DASHBOARD_GENERATION_MAX_CONCURRENT = 3
_JOB_RETENTION_SECONDS = 1800.0
_MAX_TRACKED_JOBS = 200

DashboardGenerationJobStatus = Literal[
    "running", "succeeded", "failed", "cancelled"
]


class DashboardGenerationError(Exception):
    """A generation failure with a user-facing message."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


class DashboardGenerationConflict(Exception):
    """A new job cannot start because of concurrency limits."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DashboardGenerationJob:
    id: str
    user_id: str
    target_key: str | None = None
    status: DashboardGenerationJobStatus = "running"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    task: asyncio.Task | None = None


_jobs: dict[str, DashboardGenerationJob] = {}


def _prune_jobs(now: float | None = None) -> None:
    now = time.time() if now is None else now
    stale = [
        job_id
        for job_id, job in _jobs.items()
        if job.finished_at is not None
        and now - job.finished_at > _JOB_RETENTION_SECONDS
    ]
    for job_id in stale:
        del _jobs[job_id]
    if len(_jobs) > _MAX_TRACKED_JOBS:
        terminal = sorted(
            (job for job in _jobs.values() if job.finished_at is not None),
            key=lambda job: job.finished_at or 0.0,
        )
        for job in terminal[: len(_jobs) - _MAX_TRACKED_JOBS]:
            _jobs.pop(job.id, None)


def get_dashboard_generation_job(
    job_id: str, user_id: str
) -> DashboardGenerationJob | None:
    job = _jobs.get(job_id)
    if job is None or job.user_id != user_id:
        return None
    return job


def cancel_dashboard_generation_job(
    job_id: str, user_id: str
) -> DashboardGenerationJob | None:
    job = get_dashboard_generation_job(job_id, user_id)
    if job is None:
        return None
    if job.status == "running" and job.task is not None:
        job.task.cancel()
    return job


def start_dashboard_generation_job(
    user_id: str,
    runner: Callable[[], Awaitable[dict[str, Any]]],
    *,
    target_key: str | None = None,
    timeout_seconds: float = DASHBOARD_GENERATION_TIMEOUT_SECONDS,
    max_concurrent: int = DASHBOARD_GENERATION_MAX_CONCURRENT,
) -> DashboardGenerationJob:
    """Start ``runner`` in the background and track it as a job.

    A user may run several generations concurrently (up to ``max_concurrent``)
    so multiple modules can be built in parallel. Jobs that share a
    ``target_key`` (edits of the same module) are mutually exclusive — there
    is no sane merge for two concurrent edits of one module.
    """
    _prune_jobs()
    running = [
        job
        for job in _jobs.values()
        if job.user_id == user_id and job.status == "running"
    ]
    if target_key is not None and any(
        job.target_key == target_key for job in running
    ):
        raise DashboardGenerationConflict(
            "This dashboard module is already being generated",
            code="target_busy",
        )
    if len(running) >= max_concurrent:
        raise DashboardGenerationConflict(
            f"At most {max_concurrent} dashboard generations can run at once",
            code="concurrency_limit",
        )

    job = DashboardGenerationJob(
        id=f"dashjob_{secrets.token_hex(12)}",
        user_id=user_id,
        target_key=target_key,
    )

    async def _run() -> None:
        try:
            job.result = await asyncio.wait_for(runner(), timeout_seconds)
            job.status = "succeeded"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error_code = "cancelled"
        except TimeoutError:
            job.status = "failed"
            job.error_code = "timeout"
            job.error = "Dashboard generation timed out"
        except DashboardGenerationError as exc:
            job.status = "failed"
            job.error_code = exc.code
            job.error = str(exc)[:500]
        except Exception as exc:  # noqa: BLE001 — job boundary must not leak
            job.status = "failed"
            job.error_code = "error"
            job.error = str(exc)[:500] or exc.__class__.__name__
        finally:
            job.finished_at = time.time()
            job.task = None

    job.task = asyncio.create_task(_run())
    _jobs[job.id] = job
    return job
