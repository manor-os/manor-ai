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

import json
import logging
import asyncio
from functools import wraps
from typing import Any, Optional

from packages.core.config import get_settings

logger = logging.getLogger(__name__)

_redis = None
_redis_loop = None


async def _get_redis():
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is not None and _redis_loop is not None and _redis_loop is not loop:
        # Celery workers create and close event loops frequently. Reusing an
        # asyncio Redis client from a previous loop causes noisy cross-loop
        # failures during best-effort pub/sub and cache writes.
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
        _redis_loop = None
    if _redis is None:
        try:
            import redis.asyncio as aioredis

            url = get_settings().REDIS_URL
            _redis = aioredis.from_url(url, decode_responses=True)
            await _redis.ping()
            _redis_loop = loop
            logger.info(
                "Redis cache connected: %s",
                url.split("@")[-1] if "@" in url else url,
            )
        except Exception as e:
            logger.warning("Redis not available for caching: %s", e)
            _redis = None
            _redis_loop = None
    return _redis


class Cache:
    """Redis-backed cache with JSON serialization."""

    PREFIX = "manor:"

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
        """Close the Redis connection."""
        global _redis, _redis_loop
        if _redis:
            await _redis.aclose()
            _redis = None
            _redis_loop = None


# Global singleton
cache = Cache()
