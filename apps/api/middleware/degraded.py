"""Emergency load-shedding middleware for single-server deployments."""
from __future__ import annotations

import os
import uuid

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def _is_chat_stream(path: str) -> bool:
    return (
        path == "/api/v1/chat/stream"
        or (path.startswith("/api/v1/public/chat/") and path.endswith("/message/stream"))
        or path.startswith("/api/v1/workspace-drafts/") and path.endswith("/stream")
        or path == "/api/v1/workspace-drafts/stream"
    )


def _is_sandbox_work(path: str) -> bool:
    return (
        path == "/api/v1/workspaces/sandbox"
        or path.startswith("/api/v1/browser")
        or path.startswith("/api/v1/headed-login")
        or path.endswith("/browser-mcp")
    )


def _is_media_generation(path: str, method: str) -> bool:
    if method not in {"POST", "PUT", "PATCH"}:
        return False
    return (
        path.startswith("/api/v1/media")
        or path == "/api/v1/skills/generate"
        or path == "/api/v1/skills/generate-stream"
        or path.endswith("/generate-stream")
    )


def _is_large_upload(path: str, method: str) -> bool:
    if method not in {"POST", "PUT", "PATCH"}:
        return False
    return path in {
        "/api/v1/fs/upload",
        "/api/v1/documents/upload",
        "/api/v1/audio/transcribe",
        "/api/v1/tasks/import",
        "/api/v1/bulk/import",
    }


def degraded_reason(path: str, method: str) -> str | None:
    """Return a reason when degraded mode should shed this request."""
    method = method.upper()
    if _env_bool("DEGRADED_DISABLE_CHAT_STREAM", "true") and _is_chat_stream(path):
        return "chat_stream"
    if _env_bool("DEGRADED_DISABLE_SANDBOX", "true") and _is_sandbox_work(path):
        return "sandbox"
    if _env_bool("DEGRADED_DISABLE_MEDIA_GENERATION", "true") and _is_media_generation(path, method):
        return "media_generation"
    if _env_bool("DEGRADED_DISABLE_LARGE_UPLOADS", "true") and _is_large_upload(path, method):
        return "large_upload"
    return None


def _request_id_from_scope(scope: Scope) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"x-request-id":
            try:
                return value.decode("latin1")
            except UnicodeDecodeError:
                return ""
    return ""


class DegradedModeMiddleware:
    """Return 503 for known high-cost requests when DEGRADED_MODE=true."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not _env_bool("DEGRADED_MODE"):
            await self.app(scope, receive, send)
            return

        reason = degraded_reason(str(scope.get("path") or ""), str(scope.get("method") or "GET"))
        if not reason:
            await self.app(scope, receive, send)
            return

        rid = _request_id_from_scope(scope) or uuid.uuid4().hex
        response = JSONResponse(
            status_code=503,
            content={
                "error": "Service temporarily busy",
                "detail": "This operation is temporarily disabled while the service is in degraded mode.",
                "code": "degraded_mode",
                "reason": reason,
                "request_id": rid,
            },
            headers={"Retry-After": "60", "X-Request-ID": rid},
        )
        await response(scope, receive, send)
