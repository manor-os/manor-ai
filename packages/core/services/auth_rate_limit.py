"""Shared brute-force protection for password authentication.

Cloud deployments enable this unconditionally. Redis provides cross-worker
account and IP buckets; a bounded in-process fallback remains active during a
Redis incident instead of silently disabling protection.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from packages.core.config import get_settings

logger = logging.getLogger(__name__)

_ACCOUNT_LIMIT = int(os.getenv("AUTH_ACCOUNT_FAILURE_LIMIT", "10"))
_ACCOUNT_WINDOW = int(os.getenv("AUTH_ACCOUNT_FAILURE_WINDOW_SECONDS", "900"))
_IP_LIMIT = int(os.getenv("AUTH_IP_FAILURE_LIMIT", "80"))
_IP_WINDOW = int(os.getenv("AUTH_IP_FAILURE_WINDOW_SECONDS", "3600"))
_MEMORY_MAX_BUCKETS = max(100, int(os.getenv("AUTH_FALLBACK_MAX_BUCKETS", "10000")))
_MEMORY_CLEANUP_INTERVAL = max(
    1,
    int(os.getenv("AUTH_FALLBACK_CLEANUP_INTERVAL_SECONDS", "60")),
)
_MEMORY_BUCKETS: OrderedDict[str, list[float]] = OrderedDict()
_MEMORY_LAST_CLEANUP = 0.0


@dataclass(frozen=True)
class AuthRateLimitDecision:
    allowed: bool
    retry_after: int = 0


def auth_rate_limit_enabled() -> bool:
    if get_settings().DEPLOYMENT_MODE.strip().lower() == "cloud":
        return True
    configured = os.getenv("AUTH_RATE_LIMIT_ENABLED")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _memory_cleanup(now: float) -> None:
    global _MEMORY_LAST_CLEANUP
    if now - _MEMORY_LAST_CLEANUP < _MEMORY_CLEANUP_INTERVAL:
        return
    oldest_allowed = now - max(_ACCOUNT_WINDOW, _IP_WINDOW)
    for key in list(_MEMORY_BUCKETS):
        entries = [stamp for stamp in _MEMORY_BUCKETS[key] if stamp > oldest_allowed]
        if entries:
            _MEMORY_BUCKETS[key] = entries
        else:
            _MEMORY_BUCKETS.pop(key, None)
    _MEMORY_LAST_CLEANUP = now


def _memory_store(key: str, entries: list[float]) -> None:
    _MEMORY_BUCKETS[key] = entries
    _MEMORY_BUCKETS.move_to_end(key)
    while len(_MEMORY_BUCKETS) > _MEMORY_MAX_BUCKETS:
        _MEMORY_BUCKETS.popitem(last=False)


def _memory_increment(key: str, *, limit: int, window: int) -> AuthRateLimitDecision:
    now = time.time()
    _memory_cleanup(now)
    entries = [stamp for stamp in _MEMORY_BUCKETS.get(key, []) if stamp > now - window]
    if len(entries) >= limit:
        _memory_store(key, entries)
        return AuthRateLimitDecision(
            False,
            max(1, int(window - (now - min(entries))) + 1),
        )
    entries.append(now)
    _memory_store(key, entries)
    return AuthRateLimitDecision(True)


async def _redis_increment(key: str, *, limit: int, window: int) -> AuthRateLimitDecision | None:
    try:
        from packages.core.cache import _get_redis

        client = await _get_redis()
        if client is None:
            return None
        redis_key = f"auth:fail:{key}"
        count = int(await client.incr(redis_key))
        if count == 1:
            await client.expire(redis_key, window)
        ttl = int(await client.ttl(redis_key))
        if count > limit:
            return AuthRateLimitDecision(False, ttl if ttl > 0 else window)
        return AuthRateLimitDecision(True)
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        logger.warning("Auth rate limiter Redis unavailable; using local fallback: %s", exc)
        return None


async def _redis_check(key: str, *, limit: int, window: int) -> AuthRateLimitDecision | None:
    try:
        from packages.core.cache import _get_redis

        client = await _get_redis()
        if client is None:
            return None
        redis_key = f"auth:fail:{key}"
        count = int(await client.get(redis_key) or 0)
        if count >= limit:
            ttl = int(await client.ttl(redis_key))
            return AuthRateLimitDecision(False, ttl if ttl > 0 else window)
        return AuthRateLimitDecision(True)
    except Exception:
        return None


def _memory_check(key: str, *, limit: int, window: int) -> AuthRateLimitDecision:
    now = time.time()
    _memory_cleanup(now)
    entries = [stamp for stamp in _MEMORY_BUCKETS.get(key, []) if stamp > now - window]
    if entries:
        _memory_store(key, entries)
    else:
        _MEMORY_BUCKETS.pop(key, None)
    if len(entries) >= limit:
        return AuthRateLimitDecision(False, max(1, int(window - (now - min(entries))) + 1))
    return AuthRateLimitDecision(True)


async def check_login_allowed(login_id: str, client_ip: str) -> AuthRateLimitDecision:
    """Check current failure budgets without charging a successful login."""
    if not auth_rate_limit_enabled():
        return AuthRateLimitDecision(True)

    account_key = f"account:{_digest(login_id)}"
    ip_key = f"ip:{_digest(client_ip or 'unknown')}"
    for key, limit, window in (
        (account_key, _ACCOUNT_LIMIT, _ACCOUNT_WINDOW),
        (ip_key, _IP_LIMIT, _IP_WINDOW),
    ):
        decision = await _redis_check(key, limit=limit, window=window)
        if decision is None:
            decision = _memory_check(key, limit=limit, window=window)
        if not decision.allowed:
            return decision
    return AuthRateLimitDecision(True)


async def record_login_failure(login_id: str, client_ip: str) -> AuthRateLimitDecision:
    """Charge both account and IP budgets after failed authentication."""
    if not auth_rate_limit_enabled():
        return AuthRateLimitDecision(True)
    account_key = f"account:{_digest(login_id)}"
    ip_key = f"ip:{_digest(client_ip or 'unknown')}"
    for key, limit, window in (
        (account_key, _ACCOUNT_LIMIT, _ACCOUNT_WINDOW),
        (ip_key, _IP_LIMIT, _IP_WINDOW),
    ):
        decision = await _redis_increment(key, limit=limit, window=window)
        if decision is None:
            decision = _memory_increment(key, limit=limit, window=window)
        if not decision.allowed:
            return decision
    return AuthRateLimitDecision(True)


async def clear_login_failures(login_id: str) -> None:
    """Reset the account bucket after full password + MFA authentication."""
    if not auth_rate_limit_enabled():
        return
    account_key = f"account:{_digest(login_id)}"
    _MEMORY_BUCKETS.pop(account_key, None)
    try:
        from packages.core.cache import _get_redis

        client = await _get_redis()
        if client is not None:
            await client.delete(f"auth:fail:{account_key}")
    except Exception:
        logger.debug("Unable to clear Redis auth failure bucket", exc_info=True)
