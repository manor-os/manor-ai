"""Sandbox SDK exceptions."""
from __future__ import annotations

from typing import Any


class SandboxError(Exception):
    """Base exception for all sandbox SDK errors.

    ``code`` is the runner's machine-readable error code (for example
    ``sandbox_lost``, ``sandbox_busy``, ``file_not_found``) when the runner
    sent one; ``status_code`` is the HTTP status.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: str | None = None,
        body: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.body: dict[str, Any] = dict(body or {})


class SandboxNotFoundError(SandboxError):
    """Sandbox or resource not found (HTTP 404)."""


class SandboxSecurityError(SandboxError):
    """Security check failed (HTTP 403)."""


class SandboxCapacityError(SandboxError):
    """Sandbox capacity exhausted (HTTP 429)."""


class SandboxRequestError(SandboxError):
    """The runner rejected the request itself (HTTP 400/413/422): fix it, do not retry."""


class SandboxRuntimeError(SandboxError):
    """Server-side runtime error (HTTP 5xx)."""


class SandboxTimeoutError(SandboxRuntimeError):
    """The command or Docker operation timed out on the runner (HTTP 504).

    ``stdout``/``stderr`` hold whatever output the command produced before
    it was stopped.
    """

    @property
    def stdout(self) -> str:
        return str(self.body.get("stdout") or "")

    @property
    def stderr(self) -> str:
        return str(self.body.get("stderr") or "")

    @property
    def execution_id(self) -> str | None:
        value = self.body.get("execution_id")
        return str(value) if value else None


class SandboxConnectionError(SandboxError):
    """Cannot connect to the Sandbox Service."""


class SandboxRunnerOutdatedError(SandboxError):
    """The runner lacks a capability this request needs; nothing was sent or run.

    ``missing`` names the capabilities (``sandbox_sdk.capabilities``). Not
    retryable until the runner is upgraded -- unless ``reported`` is False:
    the runner's /health could not be read at all (it is restarting or
    unreachable), so nothing is known to be missing yet.
    """

    def __init__(
        self,
        message: str,
        *,
        missing: list[str] | tuple[str, ...] = (),
        build_version: str = "",
        reported: bool = True,
    ):
        super().__init__(message, code="runner_outdated")
        self.missing = list(missing)
        self.build_version = build_version
        self.reported = reported
