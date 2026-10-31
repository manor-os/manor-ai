"""
Typed runner errors.

Every failure the runner can report to a caller is one of these classes. Each
class carries its HTTP status and a machine-readable ``code``; the API layer
maps them in one place (``api/errors.py``) instead of per-route try/except, so
a route can never turn a "sandbox is gone" or "sandbox is busy" into a bare
500 again.

Several classes also derive from the builtin they replace (``KeyError`` for
not-found, ``RuntimeError`` for state conflicts, ``ValueError`` for invalid
input, ``OSError`` for file problems) so in-process callers that predate the
typed errors keep working.
"""

from __future__ import annotations

from typing import Any

from .concurrency import SandboxConcurrencyExceeded


class RunnerError(Exception):
    """Base class: an error with an HTTP status and a stable ``code``."""

    status_code: int = 500
    code: str = "runner_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.extra: dict[str, Any] = dict(extra or {})

    def __str__(self) -> str:  # KeyError would otherwise quote the message
        return self.message


# ── 404: the thing addressed does not exist (any more) ──


class SandboxGone(RunnerError, KeyError):
    """The sandbox id is not (or no longer) served by this runner."""

    status_code = 404
    code = "sandbox_not_found"


class SandboxLost(SandboxGone):
    """Docker reports the sandbox's container is gone or not running.

    Raised by the Docker backend after the daemon error was confirmed with an
    inspect. The runner reacts by reclaiming the sandbox (registry, capacity,
    in-flight executions), so every later request for the id answers 404.
    """

    code = "sandbox_lost"


class ExecutionNotFound(RunnerError, KeyError):
    """No execution with this id is running or recorded for the sandbox."""

    status_code = 404
    code = "execution_not_found"


class ExecutionEventNotFound(ExecutionNotFound):
    code = "execution_event_not_found"


class SandboxFileNotFound(RunnerError, FileNotFoundError):
    status_code = 404
    code = "file_not_found"


# ── 409: the request conflicts with the sandbox's current state ──


class SandboxBusy(RunnerError, RuntimeError):
    """Another command or a skill load is running; retry later."""

    status_code = 409
    code = "sandbox_busy"


class ExecQueueTimeout(SandboxBusy):
    """The command waited its whole ``queue_timeout`` for a free slot; it never started."""

    code = "sandbox_queue_timeout"


class SandboxDiskQuotaExceeded(RunnerError, RuntimeError):
    """The command's work area is over its share of a shared disk; it never started.

    Raised for a new command of a work area the disk quota refuses
    (``sandbox.disk_quota``), and for one that was waiting for a slot when
    the refusal began. ``extra`` carries ``started: false``, the area and the
    disk. Cleanup commands are never refused.
    """

    status_code = 409
    code = "sandbox_disk_quota"


class ExecutionCancelled(RunnerError, RuntimeError):
    """The command was cancelled while it waited for a slot; it never started."""

    status_code = 409
    code = "execution_cancelled"


class SandboxUnavailable(RunnerError, RuntimeError):
    """The sandbox is quarantined or being destroyed; it will not recover."""

    status_code = 409
    code = "sandbox_unavailable"


class BakedSkillUnavailable(RunnerError, RuntimeError):
    """The sandbox image does not hold this built-in Skill at the requested digest.

    Raised before the container is touched; the caller sends the files instead.
    """

    status_code = 409
    code = "baked_skill_unavailable"


class ExecutionConflict(RunnerError, ValueError):
    """The execution id or event already exists with a different request."""

    status_code = 409
    code = "execution_conflict"


class ExecutionStateConflict(RunnerError, RuntimeError):
    """The execution is in a state that does not accept the request."""

    status_code = 409
    code = "execution_state_conflict"


# ── 400 / 413 / 422: the caller sent something the runner cannot use ──


class InvalidRequest(RunnerError, ValueError):
    status_code = 400
    code = "invalid_request"


class InvalidPath(RunnerError, OSError):
    """The path cannot be read or written (read-only, not a directory, ...)."""

    status_code = 400
    code = "invalid_path"


class InvalidFileContent(RunnerError, OSError):
    """The content to write is unusable (bad base64, empty binary)."""

    status_code = 400
    code = "invalid_content"


class FileTooLarge(RunnerError, OSError):
    status_code = 413
    code = "file_too_large"


class ConfigOverrideRejected(InvalidRequest):
    """A caller asked for container config it may not set, or above a bound.

    ``extra`` names the ``field``, the ``reason`` (``not_overridable``,
    ``above_maximum``, ``invalid_value``) and, for a bound, the ``maximum``.
    """

    code = "config_override_rejected"


class DependencyInstallFailed(RunnerError, RuntimeError):
    """The skill's dependency install exited non-zero."""

    status_code = 422
    code = "dependency_install_failed"


# ── 429: the runner has no room right now ──


class RunnerFull(RunnerError, SandboxConcurrencyExceeded):
    """No capacity for this container now; it fits once others are released.

    Raised before anything is created, so a caller may simply retry later.
    ``extra`` carries ``reason`` (``memory`` or ``max_sandboxes``) and the
    memory numbers in MiB: ``required_memory_mb``, ``available_memory_mb``,
    ``largest_allocatable_mb``, ``memory_budget_mb``, ``max_container_memory_mb``,
    plus ``active_sandboxes`` / ``max_active``. Also a
    ``SandboxConcurrencyExceeded`` for in-process callers that predate it.
    """

    status_code = 429
    code = "runner_full"


# ── 5xx: the runner or Docker failed ──


class FileOperationFailed(RunnerError, OSError):
    status_code = 500
    code = "file_operation_failed"


class LoadSkillFailed(RunnerError, RuntimeError):
    status_code = 500
    code = "load_skill_failed"


class DestroyFailed(RunnerError, RuntimeError):
    status_code = 500
    code = "destroy_failed"


class DockerUnavailable(RunnerError, RuntimeError):
    """The docker CLI or daemon cannot be reached."""

    status_code = 503
    code = "docker_unavailable"




class CancelFailed(RunnerError, RuntimeError):
    status_code = 503
    code = "cancel_failed"


class RunnerShuttingDown(RunnerError, RuntimeError):
    status_code = 503
    code = "shutting_down"


class CommandTimeout(RunnerError, RuntimeError):
    """A command exceeded its timeout; ``extra`` carries the partial output."""

    status_code = 504
    code = "command_timeout"


# Partial output carried by a timeout error is bounded so an error body can
# never be larger than a normal exec response would have been.
PARTIAL_OUTPUT_LIMIT = 64 * 1024


def bounded_text(data: bytes | str | None, limit: int = PARTIAL_OUTPUT_LIMIT) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    if len(data) > limit:
        return data[-limit:]
    return data
