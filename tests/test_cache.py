"""Unit tests for packages.core.cache — mocked Redis, no live connection needed."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

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
    cache_module._redis_by_loop = {}
    yield
    cache_module._redis = None
    cache_module._redis_loop = None
    cache_module._redis_by_loop = {}


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
async def test_cache_touch_extends_prefixed_key_ttl():
    mock_redis = _make_mock_redis()
    mock_redis.expire = AsyncMock(return_value=True)
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()

    assert await Cache().touch("knowledge:key", 900) is True
    mock_redis.expire.assert_awaited_once_with("manor:knowledge:key", 900)


@pytest.mark.asyncio
async def test_cache_extend_lease_only_refreshes_the_current_owner():
    mock_redis = _make_mock_redis()
    mock_redis.eval = AsyncMock(side_effect=[1, 0])
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()

    cache = Cache()
    assert await cache.extend_lease("availability", "owner-token", ttl=60) is True
    assert await cache.extend_lease("availability", "stale-token", ttl=60) is False
    assert mock_redis.eval.await_args_list[0].args[2:] == (
        "manor:availability",
        "owner-token",
        60,
    )
    assert mock_redis.eval.await_args_list[1].args[2:] == (
        "manor:availability",
        "stale-token",
        60,
    )


@pytest.mark.asyncio
async def test_cache_get_many_and_set_many_use_single_batch_round_trip():
    mock_redis = _make_mock_redis()
    mock_redis.mget = AsyncMock(return_value=['{"value": 1}', None, "invalid-json"])

    class FakePipeline:
        def __init__(self):
            self.set_calls = []
            self.execute = AsyncMock(return_value=[])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def set(self, *args, **kwargs):
            self.set_calls.append((args, kwargs))
            return self

    pipeline = FakePipeline()
    mock_redis.pipeline = MagicMock(return_value=pipeline)
    cache_module._redis = mock_redis
    cache_module._redis_loop = asyncio.get_running_loop()
    c = Cache()

    assert await c.get_many(["one", "two", "three"]) == [{"value": 1}, None, None]
    mock_redis.mget.assert_awaited_once_with(
        ["manor:one", "manor:two", "manor:three"]
    )

    assert await c.set_many({"one": [1.0], "two": {"value": 2}}, ttl=60) is True
    assert pipeline.set_calls == [
        (("manor:one", "[1.0]"), {"ex": 60}),
        (("manor:two", '{"value": 2}'), {"ex": 60}),
    ]
    pipeline.execute.assert_awaited_once()


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
        assert await c.get_many(["one", "two"]) == [None, None]
        assert await c.set_many({"one": 1}) is False
        assert await c.touch("any", 60) is False
        assert await c.extend_lease("any", "token", 60) is None
        assert await c.delete("any") is False
        assert await c.delete_pattern("any:*") == 0


def test_get_redis_does_not_close_client_owned_by_another_active_loop(monkeypatch):
    """A concurrent loop must not close Redis while its owner is using it."""

    class FakeRedis:
        def __init__(self):
            self.closed = False
            self.close_calls = 0

        async def ping(self):
            if self.closed:
                raise RuntimeError("Redis client was closed")

        async def aclose(self):
            self.close_calls += 1
            self.closed = True

    clients: list[FakeRedis] = []

    def fake_from_url(*_args, **_kwargs):
        client = FakeRedis()
        clients.append(client)
        return client

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", fake_from_url)
    first_ready = threading.Event()
    second_finished = threading.Event()
    failures: list[BaseException] = []

    def run_first_loop():
        async def use_client():
            client = await cache_module._get_redis()
            first_ready.set()
            assert second_finished.wait(timeout=2)
            await client.ping()

        try:
            asyncio.run(use_client())
        except BaseException as exc:  # Thread failures are asserted below.
            failures.append(exc)

    def run_second_loop():
        async def use_client():
            assert first_ready.wait(timeout=2)
            await cache_module._get_redis()
            second_finished.set()

        try:
            asyncio.run(use_client())
        except BaseException as exc:  # Thread failures are asserted below.
            failures.append(exc)
            second_finished.set()

    first = threading.Thread(target=run_first_loop)
    second = threading.Thread(target=run_second_loop)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not failures
    assert len(clients) == 2
    assert clients[0].close_calls == 0
