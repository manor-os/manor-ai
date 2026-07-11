"""Unit tests for packages.core.cache — mocked Redis, no live connection needed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import packages.core.cache as cache_module
from packages.core.cache import Cache


# ── Helpers ──


def _make_mock_redis() -> AsyncMock:
    """Return an AsyncMock that behaves like a redis.asyncio client."""
    r = AsyncMock()
    r.ping = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    r.delete = AsyncMock(return_value=1)
    r.incrby = AsyncMock(return_value=1)
    r.expire = AsyncMock()
    return r


@pytest.fixture(autouse=True)
def _reset_global_redis():
    """Reset the module-level _redis singleton between tests."""
    cache_module._redis = None
    cache_module._redis_loop = None
    yield
    cache_module._redis = None
    cache_module._redis_loop = None


# ── Tests ──


@pytest.mark.asyncio
async def test_cache_set_get():
    """set() stores JSON, get() deserializes it back."""
    mock_redis = _make_mock_redis()
    stored = {}

    async def fake_set(key, value, ex=None):
        stored[key] = value

    async def fake_get(key):
        return stored.get(key)

    mock_redis.set = AsyncMock(side_effect=fake_set)
    mock_redis.get = AsyncMock(side_effect=fake_get)

    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()
    c = Cache()

    await c.set("foo", {"bar": 42}, ttl=60)
    result = await c.get("foo")
    assert result == {"bar": 42}


@pytest.mark.asyncio
async def test_cache_miss():
    """get() returns None for a key that was never set."""
    mock_redis = _make_mock_redis()
    mock_redis.get = AsyncMock(return_value=None)
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()

    c = Cache()
    assert await c.get("nonexistent") is None


@pytest.mark.asyncio
async def test_cache_delete():
    """delete() removes the key from Redis."""
    mock_redis = _make_mock_redis()
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()

    c = Cache()
    result = await c.delete("mykey")
    assert result is True
    mock_redis.delete.assert_awaited_once_with("manor:mykey")


@pytest.mark.asyncio
async def test_cache_incr_sets_prefixed_key_and_ttl():
    mock_redis = _make_mock_redis()
    mock_redis.incrby = AsyncMock(return_value=3)
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()

    c = Cache()
    value = await c.incr("version:key", amount=2, ttl=60)

    assert value == 3
    mock_redis.incrby.assert_awaited_once_with("manor:version:key", 2)
    mock_redis.expire.assert_awaited_once_with("manor:version:key", 60)


@pytest.mark.asyncio
async def test_cache_decorator():
    """@cached decorator caches function return and serves from cache on repeat."""
    mock_redis = _make_mock_redis()
    stored = {}

    async def fake_set(key, value, ex=None):
        stored[key] = value

    async def fake_get(key):
        return stored.get(key)

    mock_redis.set = AsyncMock(side_effect=fake_set)
    mock_redis.get = AsyncMock(side_effect=fake_get)
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()

    c = Cache()
    call_count = 0

    @c.cached(prefix="thing", ttl=60)
    async def get_thing(db, thing_id: str):
        nonlocal call_count
        call_count += 1
        return {"id": thing_id, "value": "hello"}

    # First call — executes the function
    result = await get_thing("fake_db", "abc")
    assert result == {"id": "abc", "value": "hello"}
    assert call_count == 1

    # Second call — served from cache, function not called again
    result = await get_thing("fake_db", "abc")
    assert result == {"id": "abc", "value": "hello"}
    assert call_count == 1

    # Invalidation
    await get_thing.invalidate("abc")
    mock_redis.delete.assert_awaited_with("manor:thing:abc")


@pytest.mark.asyncio
async def test_cache_graceful_when_redis_unavailable():
    """All operations return safe defaults when Redis is None (no exceptions)."""
    # _redis stays None (autouse fixture reset it)
    # Patch _get_redis to always return None (simulating connection failure)
    with patch.object(cache_module, "_get_redis", new=AsyncMock(return_value=None)):
        c = Cache()
        assert await c.get("any") is None
        assert await c.set("any", "val") is False
        assert await c.delete("any") is False
        assert await c.delete_pattern("any:*") == 0
