"""Rate limiting middleware for chat and API endpoints."""
from __future__ import annotations

import os
import time
import logging
from collections import defaultdict
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# ── Configuration (env-driven) ──
CHAT_RATE_LIMIT_ENABLED = os.getenv("CHAT_RATE_LIMIT_ENABLED", "false").lower() == "true"
CHAT_RATE_LIMIT_REQUESTS = int(os.getenv("CHAT_RATE_LIMIT_REQUESTS", "30"))
CHAT_RATE_LIMIT_WINDOW = int(os.getenv("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60"))
API_RATE_LIMIT_REQUESTS = int(os.getenv("API_RATE_LIMIT_REQUESTS", "200"))
API_RATE_LIMIT_WINDOW = int(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60"))
REDIS_RATE_LIMIT_ENABLED = os.getenv("REDIS_RATE_LIMIT_ENABLED", "false").lower() in ("1", "true", "yes", "on")

_HEALTH_PATHS = {"/health", "/health/"}


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0


class RateLimiter:
    """Rate limiter with in-memory defaults and optional Redis shared buckets."""

    def __init__(self, *, redis_client=None, redis_enabled: bool | None = None):
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._redis_client = redis_client
        self._redis_enabled = REDIS_RATE_LIMIT_ENABLED if redis_enabled is None else redis_enabled

    async def check(self, key: str, max_requests: int, window_seconds: int) -> RateLimitResult:
        """Check if request is allowed."""
        if self._redis_enabled:
            return await self._check_redis(key, max_requests, window_seconds)
        return self._check_memory(key, max_requests, window_seconds)

    def check_sync(self, key: str, max_requests: int, window_seconds: int) -> RateLimitResult:
        """Synchronous in-memory check for non-middleware callers."""
        return self._check_memory(key, max_requests, window_seconds)

    def _check_memory(self, key: str, max_requests: int, window_seconds: int) -> RateLimitResult:
        now = time.time()
        cutoff = now - window_seconds
        entries = [t for t in self._windows[key] if t > cutoff]
        if not entries:
            self._windows.pop(key, None)
            self._windows[key] = [now]
            return RateLimitResult(True)
        if len(entries) >= max_requests:
            self._windows[key] = entries
            retry_after = max(1, int(window_seconds - (now - min(entries))) + 1)
            return RateLimitResult(False, retry_after)
        entries.append(now)
        self._windows[key] = entries
        return RateLimitResult(True)

    async def _check_redis(self, key: str, max_requests: int, window_seconds: int) -> RateLimitResult:
        try:
            client = self._redis_client or await self._get_redis_client()
            redis_key = f"rate:{key}:{int(time.time() // window_seconds)}"
            count = int(await client.incr(redis_key))
            if count == 1:
                await client.expire(redis_key, window_seconds)
            if count <= max_requests:
                return RateLimitResult(True)
            ttl = int(await client.ttl(redis_key))
            retry_after = ttl if ttl > 0 else window_seconds
            return RateLimitResult(False, retry_after)
        except Exception as exc:
            logger.warning("Redis rate limiter failed open: %s", exc)
            return RateLimitResult(True)

    async def _get_redis_client(self):
        import redis.asyncio as aioredis

        self._redis_client = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6389/0"))
        return self._redis_client


_limiter = RateLimiter()


def _path_group(path: str) -> str:
    """Group similar paths for rate limiting."""
    if "/chat/" in path:
        return "chat"
    if "/api/v1/" in path:
        return "api"
    return "other"


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class ChatRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-user/IP rate limiter with stricter limits for chat endpoints.

    This supplements the global per-IP rate limiter in middleware.py with
    finer-grained, user-aware limits — especially for chat, which is
    more expensive to serve.
    """

    async def dispatch(self, request: Request, call_next):
        if not CHAT_RATE_LIMIT_ENABLED or request.url.path in _HEALTH_PATHS:
            return await call_next(request)

        # Build key from authenticated user or client IP
        user_id = getattr(request.state, "user_id", None)
        ip = _client_ip(request)
        key = f"user:{user_id}" if user_id else f"ip:{ip}"

        # Chat endpoints get stricter limits
        path = request.url.path
        group = _path_group(path)
        if group == "chat":
            max_req, window = CHAT_RATE_LIMIT_REQUESTS, CHAT_RATE_LIMIT_WINDOW
        else:
            max_req, window = API_RATE_LIMIT_REQUESTS, API_RATE_LIMIT_WINDOW

        result = await _limiter.check(f"{key}:{group}", max_req, window)
        if not result.allowed:
            rid = getattr(request.state, "request_id", "")
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "detail": "Rate limit exceeded. Please try again later.",
                    "request_id": rid,
                },
                headers={"X-Request-ID": rid, "Retry-After": str(result.retry_after or window)},
            )

        return await call_next(request)
