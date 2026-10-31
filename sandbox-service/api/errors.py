"""
Central error mapping for the Sandbox Service API.

Routes never translate exceptions themselves: they call the runner and let
anything it raises reach these handlers. One table decides every status code,
and every error body carries a machine-readable ``code`` beside ``detail``:

    {"detail": "<human message>", "code": "<stable code>", ...extra}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sandbox.concurrency import SandboxConcurrencyExceeded
from sandbox.errors import RunnerError
from sandbox.security import SecurityError

logger = logging.getLogger("sandbox-service.errors")

# Codes for errors that are not RunnerError subclasses.
_HTTP_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_request",
    429: "capacity_exceeded",
    500: "internal_error",
    503: "service_unavailable",
    504: "timeout",
}


def error_body(detail: Any, code: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = dict(extra or {})
    body["detail"] = detail
    body["code"] = code
    return body


def classify_exception(exc: BaseException) -> tuple[int, str, str, dict[str, Any]]:
    """Return ``(status, code, detail, extra)`` for any exception a route lets escape."""

    if isinstance(exc, RunnerError):
        return exc.status_code, exc.code, exc.message, dict(exc.extra)
    if isinstance(exc, SandboxConcurrencyExceeded):
        return 429, "capacity_exceeded", str(exc), {}
    if isinstance(exc, SecurityError):
        return 403, "security_denied", str(exc), {}
    if isinstance(exc, FileNotFoundError):
        # Host-side lookups (a skill directory that does not exist).
        return 404, "not_found", str(exc), {}
    if isinstance(exc, ValueError):
        return 400, "invalid_request", str(exc), {}
    return 500, "internal_error", str(exc) or type(exc).__name__, {}


async def _runner_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    status, code, detail, extra = classify_exception(exc)
    log = logger.error if status >= 500 else logger.warning
    log(
        "%s %s -> %d %s: %s",
        request.method,
        request.url.path,
        status,
        code,
        detail,
        exc_info=exc if status >= 500 and code == "internal_error" else None,
    )
    return JSONResponse(error_body(detail, code, extra), status_code=status)


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = getattr(exc, "code", None) or _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    return JSONResponse(
        error_body(exc.detail, code),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    from fastapi.encoders import jsonable_encoder

    # Never echo what was sent: ``input`` (and ``ctx``) can hold a whole
    # request object, credentials included, and the control plane logs this
    # body. Where and why is enough.
    errors = [
        {key: value for key, value in error.items() if key not in {"input", "ctx"}}
        for error in exc.errors()
    ]
    return JSONResponse(
        error_body(jsonable_encoder(errors), "invalid_request"),
        status_code=422,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    # Handlers are looked up along the exception's MRO, so RunnerError wins
    # for typed errors that also derive from a builtin. ``Exception`` is the
    # last resort (Starlette serves it from ServerErrorMiddleware as a 500).
    for exc_class in (
        RunnerError,
        SandboxConcurrencyExceeded,
        SecurityError,
        FileNotFoundError,
        ValueError,
        Exception,
    ):
        app.add_exception_handler(exc_class, _runner_exception_handler)
