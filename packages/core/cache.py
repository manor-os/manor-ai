"""Redis cache — centralized caching with TTL.

Usage:
    from packages.core.cache import cache

    # Simple get/set
    await cache.set("key", {"data": "value"}, ttl=300)
    data = await cache.get("key")  # returns dict or None

    # Decorator for caching function results
    @cache.cached(prefix="entity", ttl=300)
    async def get_entity_settings(entity_id: str) -> dict:
        ...
"""
from __future__ import annotations

import asyncio
import json
import logging
from functools import wraps
from typing import Any, Optional

from packages.core.config import get_settings

logger = logging.getLogger(__name__)

_redis = None
_redis_loop = None
_redis_by_loop = {}

# Cache is optional; a black-holed Redis must not pin an API transaction.
# Bound optional cache operations, not lease acquisition/heartbeats: an
# uncertain lock write must not become permission to proceed unlocked.
# Also preserve the shared client's legitimate BLPOP/XREAD and Pub/Sub waits.
_CACHE_IO_TIMEOUT_SECONDS = 1.0


def _bounded_cache_io(fallback=None, *, fallback_factory=None):
    def decorate(method):
        @wraps(method)
        async def bounded(*args, **kwargs):
            try:
                async with asyncio.timeout(_CACHE_IO_TIMEOUT_SECONDS):
                    return await method(*args, **kwargs)
            except TimeoutError:
                logger.debug("Cache %s exceeded its I/O deadline", method.__name__)
                if fallback_factory is not None:
                    return fallback_factory(*args, **kwargs)
                return fallback

        return bounded

    return decorate

_INCREMENT_WITH_TTL_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""

_COUNTER_VALUE_WITH_TTL_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if not value then
    return {0, -2}
end
local count = tonumber(value)
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


async def redis_increment_with_ttl(
    redis_client: Any,
    key: str,
    ttl_seconds: int,
) -> tuple[int, int]:
    """Atomically increment a Redis counter and ensure that it expires."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    result = await redis_client.eval(
        _INCREMENT_WITH_TTL_SCRIPT,
        1,
        key,
        ttl_seconds,
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("Unexpected Redis counter result")
    return int(result[0]), int(result[1])


async def redis_counter_value_with_ttl(
    redis_client: Any,
    key: str,
    ttl_seconds: int,
) -> tuple[int, int]:
    """Atomically read a Redis counter and repair a missing expiry."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    result = await redis_client.eval(
        _COUNTER_VALUE_WITH_TTL_SCRIPT,
        1,
        key,
        ttl_seconds,
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("Unexpected Redis counter result")
    return int(result[0]), int(result[1])


async def _get_redis():
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    redis = _redis_by_loop.get(loop)
    if redis is not None:
        _redis = redis
        _redis_loop = loop
        return redis
    if _redis is not None and _redis_loop is loop:
        _redis_by_loop[loop] = _redis
        return _redis
    if _redis is not None and _redis_loop is not None:
        _redis_by_loop.setdefault(_redis_loop, _redis)

    # Celery workers create and close event loops frequently. Retire clients
    # only after their owning loops close: another active loop may still be
    # using its client while this one connects.
    for owner_loop, client in list(_redis_by_loop.items()):
        if owner_loop is loop or not owner_loop.is_closed():
            continue
        _redis_by_loop.pop(owner_loop, None)
        try:
            await client.aclose()
        except Exception:
            pass
    redis = None
    try:
        import redis.asyncio as aioredis

        url = get_settings().REDIS_URL
        redis = aioredis.from_url(url, decode_responses=True)
        await redis.ping()
        _redis_by_loop[loop] = redis
        _redis = redis
        _redis_loop = loop
        logger.info(
            "Redis cache connected: %s",
            url.split("@")[-1] if "@" in url else url,
        )
    except BaseException as e:
        # Failed/cancelled initial pings never enter the per-loop registry.
        # Close that otherwise untracked pool before returning or propagating.
        if redis is not None:
            try:
                async with asyncio.timeout(
                    0.05 if isinstance(e, asyncio.CancelledError) else _CACHE_IO_TIMEOUT_SECONDS
                ):
                    await redis.aclose()
            except Exception:
                pass
        if not isinstance(e, Exception):
            raise
        logger.warning("Redis not available for caching: %s", e)
        redis = None
    return redis


#: The shared per-loop client, or None when Redis is unavailable.
get_redis = _get_redis

#: A slice when the client names no read timeout.
_BLOCKING_SLICE_SECONDS = 4.0


def blocking_slice_seconds(redis: Any, remaining: float) -> float:
    """How long one blocking read (BLPOP, XREAD block) on ``redis`` may wait.

    The client gives up reading after its socket timeout (redis-py 5.1+
    defaults to 5 s), so a longer server-side block fails with "Timeout
    reading" instead of waiting. Wait in slices that end before it, looping
    until ``remaining`` runs out.
    """

    kwargs = getattr(getattr(redis, "connection_pool", None), "connection_kwargs", None) or {}
    socket_timeout = kwargs.get("socket_timeout")
    ceiling = _BLOCKING_SLICE_SECONDS
    if isinstance(socket_timeout, (int, float)) and socket_timeout > 0:
        # Redis expires blocking reads on its timer, so their actual duration
        # can exceed the requested block by a tick plus scheduling/transport.
        # Keep the usual one-second margin, or half a short client's budget.
        margin = min(1.0, socket_timeout / 2)
        ceiling = min(ceiling, socket_timeout - margin)
    return max(0.0, min(remaining, ceiling))


class Cache:
    """Redis-backed cache with JSON serialization."""

    PREFIX = "manor:"

    @_bounded_cache_io()
    async def get(self, key: str) -> Optional[Any]:
        """Get a value from cache. Returns None if not found or Redis unavailable."""
        r = await _get_redis()
        if r is None:
            return None
        try:
            data = await r.get(f"{self.PREFIX}{key}")
            return json.loads(data) if data else None
        except Exception as e:
            logger.debug("Cache get error for %s: %s", key, e)
            return None

    @_bounded_cache_io(False)
    async def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        """Set a value in cache with TTL (seconds). Returns True on success."""
        r = await _get_redis()
        if r is None:
            return False
        try:
            await r.set(
                f"{self.PREFIX}{key}", json.dumps(value, default=str), ex=ttl
            )
            return True
        except Exception as e:
            logger.debug("Cache set error for %s: %s", key, e)
            return False

    async def acquire_lease(
        self,
        key: str,
        token: str,
        ttl: int = 60,
    ) -> bool | None:
        """Acquire a short Redis lease.

        Returns True when acquired, False when another owner holds it, and
        None when Redis is unavailable so callers can fall back without
        waiting for a lock that does not exist.
        """
        r = await _get_redis()
        if r is None:
            return None
        try:
            acquired = await r.set(
                f"{self.PREFIX}{key}",
                token,
                ex=max(int(ttl), 1),
                nx=True,
            )
            return bool(acquired)
        except Exception as e:
            logger.debug("Cache lease acquire error for %s: %s", key, e)
            return None

    async def release_lease(self, key: str, token: str) -> bool:
        """Release a Redis lease only when *token* still owns it."""
        r = await _get_redis()
        if r is None:
            return False
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            return bool(await r.eval(script, 1, f"{self.PREFIX}{key}", token))
        except Exception as e:
            logger.debug("Cache lease release error for %s: %s", key, e)
            return False

    async def extend_lease(
        self,
        key: str,
        token: str,
        ttl: int = 60,
    ) -> bool | None:
        """Extend a Redis lease only when *token* still owns it."""
        r = await _get_redis()
        if r is None:
            return None
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        )
        try:
            return bool(await r.eval(
                script,
                1,
                f"{self.PREFIX}{key}",
                token,
                max(int(ttl), 1),
            ))
        except Exception as e:
            logger.debug("Cache lease extend error for %s: %s", key, e)
            return None

    @_bounded_cache_io(fallback_factory=lambda self, keys: [None] * len(keys))
    async def get_many(self, keys: list[str]) -> list[Any | None]:
        """Fetch several JSON values in one Redis round trip."""
        if not keys:
            return []
        r = await _get_redis()
        if r is None:
            return [None] * len(keys)
        try:
            values = await r.mget([f"{self.PREFIX}{key}" for key in keys])
            decoded: list[Any | None] = []
            for value in values:
                if value is None:
                    decoded.append(None)
                    continue
                try:
                    decoded.append(json.loads(value))
                except (TypeError, ValueError):
                    decoded.append(None)
            return decoded
        except Exception as e:
            logger.debug("Cache multi-get error: %s", e)
            return [None] * len(keys)

    @_bounded_cache_io(False)
    async def set_many(self, values: dict[str, Any], ttl: int = 300) -> bool:
        """Store several JSON values with one non-transactional pipeline."""
        if not values:
            return True
        r = await _get_redis()
        if r is None:
            return False
        try:
            async with r.pipeline(transaction=False) as pipeline:
                for key, value in values.items():
                    pipeline.set(
                        f"{self.PREFIX}{key}",
                        json.dumps(value, default=str),
                        ex=ttl,
                    )
                await pipeline.execute()
            return True
        except Exception as e:
            logger.debug("Cache multi-set error: %s", e)
            return False

    @_bounded_cache_io(False)
    async def delete(self, key: str) -> bool:
        """Delete a key from cache."""
        r = await _get_redis()
        if r is None:
            return False
        try:
            await r.delete(f"{self.PREFIX}{key}")
            return True
        except Exception:
            return False

    @_bounded_cache_io(False)
    async def touch(self, key: str, ttl: int) -> bool:
        """Extend a cache entry's TTL without downloading or rewriting it."""
        if ttl <= 0:
            return False
        r = await _get_redis()
        if r is None:
            return False
        try:
            return bool(await r.expire(f"{self.PREFIX}{key}", ttl))
        except Exception as e:
            logger.debug("Cache touch error for %s: %s", key, e)
            return False

    @_bounded_cache_io()
    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int | None:
        """Increment an integer key. Returns the new value, or None when Redis is unavailable."""
        r = await _get_redis()
        if r is None:
            return None
        try:
            full_key = f"{self.PREFIX}{key}"
            value = await r.incrby(full_key, amount)
            if ttl is not None and ttl > 0:
                await r.expire(full_key, ttl)
            return int(value)
        except Exception as e:
            logger.debug("Cache incr error for %s: %s", key, e)
            return None

    @_bounded_cache_io(0)
    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a pattern. Returns count deleted."""
        r = await _get_redis()
        if r is None:
            return 0
        try:
            keys = []
            async for key in r.scan_iter(f"{self.PREFIX}{pattern}"):
                keys.append(key)
            if keys:
                return await r.delete(*keys)
            return 0
        except Exception:
            return 0

    def cached(self, prefix: str, ttl: int = 300, key_arg: str | None = None):
        """Decorator to cache async function results.

        Cache key is built from prefix + first positional arg (or key_arg kwarg).
        """

        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # Build cache key from first arg or specified kwarg
                if key_arg and key_arg in kwargs:
                    cache_key = f"{prefix}:{kwargs[key_arg]}"
                elif args:
                    # Skip 'db' session arg (first arg for service functions)
                    key_val = args[1] if len(args) > 1 else args[0]
                    cache_key = f"{prefix}:{key_val}"
                else:
                    return await func(*args, **kwargs)

                # Try cache first
                cached = await self.get(cache_key)
                if cached is not None:
                    return cached

                # Call function and cache result
                result = await func(*args, **kwargs)
                if result is not None:
                    await self.set(cache_key, result, ttl=ttl)
                return result

            # Expose invalidation helper
            wrapper.invalidate = lambda key_val: self.delete(f"{prefix}:{key_val}")
            return wrapper

        return decorator

    async def close(self):
        """Close the Redis connection owned by the current event loop."""
        global _redis, _redis_loop
        loop = asyncio.get_running_loop()
        redis = _redis_by_loop.pop(loop, None)
        if redis is None and _redis_loop is loop:
            redis = _redis
        if _redis_loop is loop:
            _redis = None
            _redis_loop = None
        if redis:
            await redis.aclose()


# Global singleton
cache = Cache()
