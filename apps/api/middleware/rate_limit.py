"""Rate limiting middleware for chat and API endpoints."""
from __future__ import annotations

import os
import time
import logging
import ipaddress
from collections import OrderedDict
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
_DEFAULT_MEMORY_MAX_BUCKETS = 10_000
_DEFAULT_MEMORY_CLEANUP_INTERVAL = 60.0


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0


@dataclass
class _MemoryWindow:
    entries: list[float]
    window_seconds: int


class RateLimiter:
    """Rate limiter with in-memory defaults and optional Redis shared buckets."""

    def __init__(
        self,
        *,
        redis_client=None,
        redis_enabled: bool | None = None,
        max_memory_buckets: int = _DEFAULT_MEMORY_MAX_BUCKETS,
        memory_cleanup_interval: float = _DEFAULT_MEMORY_CLEANUP_INTERVAL,
    ):
        self._windows: OrderedDict[str, _MemoryWindow] = OrderedDict()
        self._redis_client = redis_client
        self._redis_enabled = REDIS_RATE_LIMIT_ENABLED if redis_enabled is None else redis_enabled
        self._max_memory_buckets = max(1, max_memory_buckets)
        self._memory_cleanup_interval = max(0.0, memory_cleanup_interval)
        self._last_memory_cleanup = 0.0

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
        self._cleanup_memory(now)
        cutoff = now - window_seconds
        bucket = self._windows.get(key)
        entries = [t for t in (bucket.entries if bucket else []) if t > cutoff]
        if len(entries) >= max_requests:
            self._store_memory(key, entries, window_seconds)
            retry_after = max(1, int(window_seconds - (now - min(entries))) + 1)
            return RateLimitResult(False, retry_after)
        entries.append(now)
        self._store_memory(key, entries, window_seconds)
        return RateLimitResult(True)

    def _cleanup_memory(self, now: float) -> None:
        if now - self._last_memory_cleanup < self._memory_cleanup_interval:
            return
        for key, bucket in list(self._windows.items()):
            cutoff = now - bucket.window_seconds
            entries = [stamp for stamp in bucket.entries if stamp > cutoff]
            if entries:
                self._windows[key] = _MemoryWindow(entries, bucket.window_seconds)
            else:
                self._windows.pop(key, None)
        self._last_memory_cleanup = now

    def _store_memory(
        self,
        key: str,
        entries: list[float],
        window_seconds: int,
    ) -> None:
        self._windows[key] = _MemoryWindow(entries, window_seconds)
        self._windows.move_to_end(key)
        while len(self._windows) > self._max_memory_buckets:
            self._windows.popitem(last=False)

    async def _check_redis(self, key: str, max_requests: int, window_seconds: int) -> RateLimitResult:
        try:
            from packages.core.cache import redis_increment_with_ttl

            client = self._redis_client or await self._get_redis_client()
            redis_key = f"rate:{key}:{int(time.time() // window_seconds)}"
            count, ttl = await redis_increment_with_ttl(
                client,
                redis_key,
                window_seconds,
            )
            if count <= max_requests:
                return RateLimitResult(True)
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


def _normalized_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """Return the client address already resolved by Uvicorn's proxy allowlist."""
    peer = _normalized_ip(request.client.host if request.client else None)
    if peer:
        return peer
    return request.client.host if request.client and request.client.host else "unknown"


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
        ip = client_ip(request)
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
