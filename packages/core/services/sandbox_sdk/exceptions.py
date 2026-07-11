"""Sandbox SDK exceptions."""
from __future__ import annotations


class SandboxError(Exception):
    """Base exception for all sandbox SDK errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SandboxNotFoundError(SandboxError):
    """Sandbox or resource not found (HTTP 404)."""


class SandboxSecurityError(SandboxError):
    """Security check failed (HTTP 403)."""


class SandboxRuntimeError(SandboxError):
    """Server-side runtime error (HTTP 500)."""


class SandboxConnectionError(SandboxError):
    """Cannot connect to the Sandbox Service."""
