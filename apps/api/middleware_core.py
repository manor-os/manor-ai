"""
Production middleware for Manor AI API.

Provides: request ID tracing, structured request logging,
error handling, and in-memory rate limiting.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from packages.core.i18n import set_locale, SUPPORTED_LOCALES

logger = logging.getLogger(__name__)

_SECRET_RE = re.compile(
    r"(?i)\b("
    r"authorization|bearer|token|access_token|refresh_token|api[_-]?key|"
    r"password|secret|client_secret"
    r")\b\s*[:=]\s*([^\s&\"']+)"
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)

# ── Context var for request ID (accessible from services/loggers) ──
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)

# ── Configuration ──
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "100"))
_RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "").lower() in ("1", "true", "yes")
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "oss")
_HEALTH_PATHS = {"/health", "/health/"}

# Streaming endpoints — BaseHTTPMiddleware buffers StreamingResponse bodies,
# so we must skip call_next wrapping for these paths.
_STREAMING_PATHS = {"/api/v1/chat/stream"}


def _is_streaming(request: Request) -> bool:
    return request.url.path in _STREAMING_PATHS

# ---------------------------------------------------------------------------
# Request ID Middleware
# ---------------------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request/response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(rid)
        request.state.request_id = rid

        if _is_streaming(request):
            return await call_next(request)

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-API-Version"] = "0.1.0"
        return response


# ---------------------------------------------------------------------------
# Request Logging Middleware
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status, and duration for every request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _HEALTH_PATHS or _is_streaming(request):
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        rid = getattr(request.state, "request_id", "")
        logger.info(
            "%s %s -> %d (%.0fms) [%s]",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            rid,
        )
        return response


# ---------------------------------------------------------------------------
# Rate Limiting (sliding window, in-memory)
# ---------------------------------------------------------------------------

_buckets: dict[str, list[float]] = defaultdict(list)
_last_cleanup: float = 0.0
_CLEANUP_INTERVAL = 60.0  # seconds between full sweeps


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _cleanup_buckets(now: float, window: float = 60.0) -> None:
    """Remove expired entries across all IPs."""
    expired_ips = []
    for ip, timestamps in _buckets.items():
        _buckets[ip] = [t for t in timestamps if now - t < window]
        if not _buckets[ip]:
            expired_ips.append(ip)
    for ip in expired_ips:
        del _buckets[ip]


# ---------------------------------------------------------------------------
# Locale Detection Middleware
# ---------------------------------------------------------------------------

def _parse_accept_language(header: str) -> str:
    """Extract the best matching locale from an Accept-Language header value."""
    if not header:
        return "en"
    # Parse tags like "zh-CN,zh;q=0.9,en;q=0.8" — pick highest-q supported locale
    best_locale = "en"
    best_q = 0.0
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        if ";q=" in part:
            tag, q_str = part.split(";q=", 1)
            try:
                q = float(q_str.strip())
            except ValueError:
                q = 0.0
        else:
            tag = part
            q = 1.0
        # Normalise: "zh-CN" -> "zh", "en-US" -> "en"
        lang = tag.strip().split("-")[0].lower()
        if lang in SUPPORTED_LOCALES and q > best_q:
            best_q = q
            best_locale = lang
    return best_locale


class LocaleMiddleware(BaseHTTPMiddleware):
    """Detect request locale from query param, header, or Accept-Language.

    Priority: ?lang= > X-Language header > Accept-Language header > "en"
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_streaming(request):
            return await call_next(request)

        # 1. Query param
        lang = request.query_params.get("lang", "").strip().lower()
        if lang and lang in SUPPORTED_LOCALES:
            set_locale(lang)
        else:
            # 2. Custom header
            x_lang = request.headers.get("x-language", "").strip().lower()
            if x_lang and x_lang in SUPPORTED_LOCALES:
                set_locale(x_lang)
            else:
                # 3. Accept-Language
                accept = request.headers.get("accept-language", "")
                set_locale(_parse_accept_language(accept))

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-IP sliding-window rate limiter.

    Disabled by default. Enable with RATE_LIMIT_ENABLED=true in environment.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not _RATE_LIMIT_ENABLED or request.url.path in _HEALTH_PATHS or _is_streaming(request):
            return await call_next(request)

        global _last_cleanup
        now = time.time()
        window = 60.0

        # Periodic cleanup
        if now - _last_cleanup > _CLEANUP_INTERVAL:
            _cleanup_buckets(now, window)
            _last_cleanup = now

        ip = _client_ip(request)
        timestamps = _buckets[ip]
        # Trim this IP's old entries
        _buckets[ip] = [t for t in timestamps if now - t < window]

        if len(_buckets[ip]) >= RATE_LIMIT_PER_MINUTE:
            oldest = min(_buckets[ip])
            retry_after = int(window - (now - oldest)) + 1
            rid = getattr(request.state, "request_id", "")
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "detail": f"Rate limit of {RATE_LIMIT_PER_MINUTE} requests per minute exceeded.",
                    "request_id": rid,
                },
                headers={"Retry-After": str(retry_after), "X-Request-ID": rid},
            )

        _buckets[ip].append(now)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Error Handlers (registered as exception handlers, not middleware)
# ---------------------------------------------------------------------------

async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Preserve HTTPException status/detail in structured JSON."""
    rid = getattr(request.state, "request_id", request_id_var.get(""))
    if exc.status_code >= 500:
        await _record_server_error(request, exc, rid, status_code=exc.status_code, handled=True)
    detail = exc.detail
    error_msg = detail if isinstance(detail, str) else (detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail))
    headers = dict(exc.headers or {})
    headers["X-Request-ID"] = rid
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": error_msg,
            "detail": detail,
            "request_id": rid,
        },
        headers=headers,
    )


async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    """Return 422 with field-level Pydantic errors."""
    rid = getattr(request.state, "request_id", request_id_var.get(""))
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation Error",
            "detail": exc.errors(),
            "request_id": rid,
        },
        headers={"X-Request-ID": rid},
    )


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions — no stack traces in production."""
    rid = getattr(request.state, "request_id", request_id_var.get(""))
    stack = traceback.format_exc()
    logger.error(
        "Unhandled exception [%s]: %s\n%s",
        rid,
        exc,
        stack,
    )
    await _record_server_error(request, exc, rid, status_code=500, handled=False, stack=stack)
    detail: str | None = str(exc)

    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": detail or "An unexpected error occurred.",
            "request_id": rid,
        },
        headers={"X-Request-ID": rid},
    )


async def _record_server_error(
    request: Request,
    exc: Exception,
    request_id: str,
    *,
    status_code: int,
    handled: bool,
    stack: str | None = None,
) -> None:
    """Persist FastAPI 5xx exceptions into the admin error center.

    Best-effort by design: error capture must never make the original
    exception path worse. It uses a fresh DB session because the route's
    session may already be rolling back.
    """
    try:
        from packages.core.database import async_session
        from packages.core.models.client_error import ClientErrorEvent

        claims = getattr(request.state, "auth_claims", {}) or {}
        message = _clip(_scrub_text(str(exc) or exc.__class__.__name__), 4000) or exc.__class__.__name__
        stack_text = _clip(_scrub_text(stack or "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))), 12000)
        route = _clip(_scrub_text(request.url.path), 500)
        method = request.method.upper()
        fingerprint = _fingerprint_server(method, request.url.path, exc, stack_text)
        async with async_session() as db:
            db.add(ClientErrorEvent(
                entity_id=_clip(_scrub_text(claims.get("entity_id")), 26),
                user_id=_clip(_scrub_text(claims.get("sub")), 26),
                source="api",
                level="error",
                handled=handled,
                name=exc.__class__.__name__,
                message=message,
                stack=stack_text,
                component_stack=None,
                fingerprint=fingerprint,
                route=route,
                url=_clip(_scrub_text(_safe_url(str(request.url))), 2000),
                release=os.getenv("BUILD_VERSION") or os.getenv("APP_VERSION"),
                environment=os.getenv("DEPLOYMENT_MODE", "oss"),
                request_id=_clip(_scrub_text(request_id), 80),
                tags={
                    "method": method,
                    "status": status_code,
                    "mechanism": "fastapi.exception_handler",
                },
                extra={},
                context={},
                ip_address=_clip(_scrub_text(_client_ip(request)), 128),
                user_agent=_clip(_scrub_text(request.headers.get("user-agent")), 1000),
                created_at=datetime.now(timezone.utc),
            ))
            await db.commit()
    except Exception:
        logger.debug("server error capture failed", exc_info=True)


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        query = urlencode([(k, "<redacted>") for k, _v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except Exception:
        return value


def _fingerprint_server(method: str, path: str, exc: Exception, stack: str | None) -> str:
    first_frames = "\n".join((stack or "").splitlines()[:8])
    raw = "|".join([
        "api",
        method,
        path,
        exc.__class__.__name__,
        str(exc)[:500],
        first_frames,
    ])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _scrub_text(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _EMAIL_RE.sub("<email>", text)
    return text.replace("\x00", "")


def _clip(value, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 20] + "...[truncated]"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_middleware(app: FastAPI) -> None:
    """Register all middleware on the app (called from main.py).

    Middleware execution order is bottom-to-top (last added runs first),
    so we add in reverse order of desired execution:
      1. RateLimitMiddleware  (added last  -> runs first)
      2. RequestLoggingMiddleware
      3. RequestIDMiddleware  (added first -> runs last / closest to route)
    """
    # Exception handlers (not middleware)
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_error_handler)  # type: ignore[arg-type]

    # Middleware — added in reverse execution order
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(LocaleMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RateLimitMiddleware)
