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
@pytest.mark.parametrize("close_blocks", [False, True])
async def test_cancelled_initial_ping_closes_unregistered_pool(monkeypatch, close_blocks):
    started = asyncio.Event()
    async def ping():
        started.set()
        await asyncio.Event().wait()
    redis = _make_mock_redis()
    redis.ping.side_effect = ping
    if close_blocks:
        async def close():
            await asyncio.Event().wait()
        redis.aclose.side_effect = close
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: redis)
    task = asyncio.create_task(cache_module._get_redis())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    redis.aclose.assert_awaited_once()
    assert asyncio.get_running_loop() not in cache_module._redis_by_loop


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
@pytest.mark.oss_regression
async def test_governance_cache_keys_follow_the_live_cache_namespace(monkeypatch):
    """A cache instance created before a test namespace change must not leak keys."""
    from packages.core import cache as cache_module
    from packages.core.governance import policy_cache

    redis = _make_mock_redis()
    redis.eval = AsyncMock(return_value=1)
    monkeypatch.setattr(policy_cache, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(cache_module.cache, "PREFIX", "manor:")
    governance_cache = policy_cache.RedisGovernancePolicyCache()
    default_fence = await governance_cache.begin_mutation("workspace")

    monkeypatch.setattr(cache_module.cache, "PREFIX", "manor:test:isolated:")
    isolated_fence = await governance_cache.begin_mutation("workspace")
    await governance_cache.store_if_current(
        policy_cache.GovernancePolicySnapshot("workspace", 3, {"mode": "automatic"})
    )

    default_keys = redis.eval.await_args_list[0].args[2:4]
    fence_keys = redis.eval.await_args_list[1].args[2:4]
    store_keys = redis.eval.await_args_list[2].args[2:4]
    body_prefix = redis.eval.await_args_list[2].args[5]
    assert default_keys == (
        "manor:governance:policy:v2:workspace:mutating",
        "manor:governance:policy:v2:workspace:head",
    )
    assert fence_keys == (
        "manor:test:isolated:governance:policy:v2:workspace:mutating",
        "manor:test:isolated:governance:policy:v2:workspace:head",
    )
    assert store_keys == fence_keys
    assert body_prefix == "manor:test:isolated:governance:policy:v2:workspace:revision:"
    assert default_fence.active is True
    assert isolated_fence.active is True


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "expected"), [
    (lambda c: c.get("test"), None),
    (lambda c: c.set("test", 1), False),
    (lambda c: c.get_many(keys=["one", "two"]), [None, None]),
    (lambda c: c.set_many({"test": 1}), False),
    (lambda c: c.delete("test"), False),
    (lambda c: c.touch("test", 60), False),
    (lambda c: c.incr("test"), None),
    (lambda c: c.delete_pattern("test:*"), 0),
])
async def test_optional_cache_operations_bound_client_acquisition(monkeypatch, operation, expected):
    async def black_hole():
        await asyncio.Event().wait()

    monkeypatch.setattr(cache_module, "_CACHE_IO_TIMEOUT_SECONDS", 0.02)
    getter = AsyncMock(side_effect=black_hole)
    monkeypatch.setattr(cache_module, "_get_redis", getter)
    assert await asyncio.wait_for(operation(Cache()), 0.5) == expected
    getter.assert_awaited_once()  # Timeout must not retry an uncertain write.


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["acquire_lease", "release_lease", "extend_lease"])
async def test_lease_operations_do_not_inherit_optional_cache_deadline(monkeypatch, method):
    redis = _make_mock_redis()

    async def delayed_result(*_args, **_kwargs):
        await asyncio.sleep(0.04)
        return True

    redis.set = AsyncMock(side_effect=delayed_result)
    redis.eval = AsyncMock(side_effect=delayed_result)
    monkeypatch.setattr(cache_module, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(cache_module, "_CACHE_IO_TIMEOUT_SECONDS", 0.01)
    assert await asyncio.wait_for(getattr(Cache(), method)("test", "owner"), 0.5) is True


@pytest.mark.asyncio
async def test_cache_deadline_includes_command_and_preserves_task_cancellation(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def black_hole(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    redis = _make_mock_redis()
    redis.get = AsyncMock(side_effect=black_hole)
    monkeypatch.setattr(cache_module, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(cache_module, "_CACHE_IO_TIMEOUT_SECONDS", 0.02)
    assert await asyncio.wait_for(Cache().get("test"), 0.5) is None
    assert cancelled.is_set()
    redis.get.assert_awaited_once()

    monkeypatch.setattr(cache_module, "_CACHE_IO_TIMEOUT_SECONDS", 10)
    started.clear()
    cancelled.clear()
    task = asyncio.create_task(Cache().get("test"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_failed_initial_ping_closes_unregistered_client(monkeypatch):
    import redis.asyncio as aioredis

    redis = _make_mock_redis()

    async def black_hole():
        await asyncio.Event().wait()

    redis.ping = AsyncMock(side_effect=black_hole)
    monkeypatch.setattr(aioredis, "from_url", MagicMock(return_value=redis))
    monkeypatch.setattr(cache_module, "_CACHE_IO_TIMEOUT_SECONDS", 0.02)
    assert await asyncio.wait_for(Cache().get("test"), 0.5) is None
    redis.aclose.assert_awaited_once()
    assert not cache_module._redis_by_loop


@pytest.mark.asyncio
async def test_shared_runtime_client_has_no_cache_deadline(monkeypatch):
    # Runtime transports intentionally perform long XREAD/BLPOP/PubSub waits.
    redis = _make_mock_redis()
    cache_module._redis = redis
    cache_module._redis_loop = asyncio.get_running_loop()
    monkeypatch.setattr(cache_module, "_CACHE_IO_TIMEOUT_SECONDS", 0.01)

    async def blocking_read():
        await asyncio.sleep(0.04)
        return "runtime-event"

    redis.xread = AsyncMock(side_effect=blocking_read)
    assert await (await cache_module._get_redis()).xread() == "runtime-event"
