"""Cache races and transaction ownership. Optional live Redis uses unique keys."""
from __future__ import annotations

import asyncio
import json
import os
import time
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, literal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core import read_cache


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    async def get(self, key):
        if self.expiry.get(key, float("inf")) <= time.monotonic():
            await self.delete(key)
        return self.values.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.expiry.pop(key, None)

    async def set(self, key, value, *, ex):
        self.values[key] = value
        self.expiry[key] = time.monotonic() + ex

    async def pttl(self, key):
        if await self.get(key) is None:
            return -2
        return int((self.expiry[key] - time.monotonic()) * 1000)

    async def eval(self, script, numkeys, *args):
        keys, argv = args[:numkeys], args[numkeys:]
        if script == read_cache._READ:
            value = await self.get(keys[0])
            if value is not None:
                return [1, value]
            if await self.get(keys[1]) is not None:
                return [0]
            self.values[keys[1]] = argv[0]
            self.expiry[keys[1]] = time.monotonic() + argv[1]
            return [2]
        if script == read_cache._STORE:
            if await self.get(keys[1]) != argv[0]:
                return 0
            ttl, maximum = argv[2:4]
            if maximum > ttl:
                try:
                    previous = json.loads(await self.get(keys[2]) or "{}")
                except ValueError:
                    previous = {}
                if isinstance(previous, dict) and previous.get("digest") == argv[4]:
                    ttl = min(maximum, max(ttl, previous["ttl"] * 2))
                await self.set(keys[2], json.dumps({"digest": argv[4], "ttl": ttl}), ex=maximum + argv[5])
            else:
                await self.delete(keys[2])
            await self.set(keys[0], argv[1], ex=ttl)
            await self.delete(keys[1])
            return 1
        if script == read_cache._RELEASE:
            if await self.get(keys[0]) == argv[0]:
                await self.delete(keys[0])
                return 1
            return 0
        raise AssertionError("Unexpected Lua script")


@pytest_asyncio.fixture
async def cached_redis(monkeypatch):
    url = os.getenv("TEST_READ_CACHE_REDIS_URL")
    prefix = f"test:read-cache:{uuid4().hex}:"
    original_keys = read_cache._keys
    touched = set()

    def keys(key):
        result = tuple(prefix + part for part in original_keys(key))
        touched.update(result)
        return result

    monkeypatch.setattr(read_cache, "_keys", keys)
    if url:
        from redis.asyncio import Redis

        backend = Redis.from_url(url, decode_responses=True)
        await backend.ping()
    else:
        backend = MemoryRedis()
    monkeypatch.setattr(read_cache, "_get_redis", AsyncMock(return_value=backend))
    try:
        yield backend
    finally:
        if touched:
            await backend.delete(*touched)
        if url:
            await backend.aclose()


async def test_old_fill_cannot_repopulate_after_invalidation(cached_redis):
    old = await read_cache.lookup("entity:alice")
    await read_cache.invalidate("entity:alice")
    new = await read_cache.lookup("entity:alice")
    assert old.token != new.token
    assert await read_cache.store(new, {"locale": "fr"}, ttl=300)
    assert not await read_cache.store(old, {"locale": "zh"}, ttl=300)
    await read_cache.abandon(old)
    assert (await read_cache.lookup("entity:alice")).value == {"locale": "fr"}


async def test_missing_token_never_allows_unconditional_fill(cached_redis):
    read = await read_cache.lookup("expired")
    await cached_redis.delete(read_cache._keys("expired")[1])
    assert not await read_cache.store(read, "stale", ttl=300)
    assert not (await read_cache.lookup("expired")).hit


async def test_cross_pod_reader_waits_for_fill(cached_redis):
    owner = await read_cache.lookup("shared")
    waiter = asyncio.create_task(read_cache.lookup("shared"))
    await asyncio.sleep(0.025)
    await read_cache.store(owner, [1], ttl=60)
    assert (await waiter).value == [1]


@pytest.mark.parametrize("available", [True, False])
async def test_concurrent_misses_issue_one_load_and_do_not_share_mutable_values(cached_redis, monkeypatch, available):
    if not available:
        monkeypatch.setattr(read_cache, "_get_redis", AsyncMock(return_value=None))
    async def load():
        await asyncio.sleep(0.025)
        return {"items": []}
    loader = AsyncMock(side_effect=load)
    results = await asyncio.gather(*(read_cache.get_or_load("same", loader, ttl=60) for _ in range(50)))
    loader.assert_awaited_once()
    results[0]["items"].append("private mutation")
    assert results[1] == {"items": []}


async def test_empty_results_cache_but_exceptions_do_not(cached_redis):
    empty = AsyncMock(return_value=[])
    for _ in range(2):
        assert await read_cache.get_or_load("empty", empty, ttl=900) == []
    empty.assert_awaited_once()
    failure = AsyncMock(side_effect=RuntimeError("database down"))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await read_cache.get_or_load("error", failure, ttl=900)
    assert failure.await_count == 2


async def test_unchanged_loads_renew_ttl_up_to_cap_with_canonical_json(cached_redis):
    key, _, history = read_cache._keys("stable")
    loader = AsyncMock()
    for index, expected in enumerate([60, 120, 240, 300, 300]):
        # JSON object ordering is not a content change, including nested maps.
        loader.return_value = {"a": 1, "b": {"c": 2, "d": 3}} if index % 2 else {"b": {"d": 3, "c": 2}, "a": 1}
        await read_cache.get_or_load("stable", loader, ttl=60, max_ttl=300)
        assert expected * 1000 - 1000 <= await cached_redis.pttl(key) <= expected * 1000
        assert json.loads(await cached_redis.get(key)) == loader.return_value
        metadata = json.loads(await cached_redis.get(history))
        assert metadata["ttl"] == expected
        assert len(metadata["digest"]) == 64
        assert 300_000 <= await cached_redis.pttl(history) <= 310_000
        # Model expiry of the value while its bounded comparison history lives.
        await cached_redis.delete(key)
    assert loader.await_count == 5


async def test_hits_do_not_renew_or_revalidate(cached_redis):
    loader = AsyncMock(return_value="stable")
    await read_cache.get_or_load("hits", loader, ttl=60, max_ttl=300)
    key, _, history = read_cache._keys("hits")
    await asyncio.sleep(0.025)
    value_ttl, history_ttl = await cached_redis.pttl(key), await cached_redis.pttl(history)
    for _ in range(10):
        assert await read_cache.get_or_load("hits", loader, ttl=60, max_ttl=300) == "stable"
    loader.assert_awaited_once()
    assert await cached_redis.pttl(key) <= value_ttl
    assert await cached_redis.pttl(history) <= history_ttl


async def test_expiry_revalidates_and_detects_a_missed_invalidation(cached_redis):
    loader = AsyncMock(return_value="original")
    assert await read_cache.get_or_load("expiry", loader, ttl=1, max_ttl=2) == "original"
    await asyncio.sleep(1.05)
    assert await read_cache.get_or_load("expiry", loader, ttl=1, max_ttl=2) == "original"
    key = read_cache._keys("expiry")[0]
    assert 1000 <= await cached_redis.pttl(key) <= 2000
    # A raw/bulk write that skipped the normal invalidation still becomes visible.
    loader.return_value = "changed"
    assert await read_cache.get_or_load("expiry", loader, ttl=1, max_ttl=2) == "original"
    await asyncio.sleep(2.05)
    assert await read_cache.get_or_load("expiry", loader, ttl=1, max_ttl=2) == "changed"
    assert 0 < await cached_redis.pttl(key) <= 1000
    assert loader.await_count == 3


async def test_change_and_write_invalidation_reset_renewal(cached_redis):
    loader = AsyncMock(return_value="original")
    key, _, history = read_cache._keys("change")
    for _ in range(2):
        await read_cache.get_or_load("change", loader, ttl=60, max_ttl=300)
        await cached_redis.delete(key)
    assert json.loads(await cached_redis.get(history))["ttl"] == 120
    loader.return_value = "changed"
    assert await read_cache.get_or_load("change", loader, ttl=60, max_ttl=300) == "changed"
    assert json.loads(await cached_redis.get(history))["ttl"] == 60
    await cached_redis.delete(key)
    await read_cache.get_or_load("change", loader, ttl=60, max_ttl=300)
    assert json.loads(await cached_redis.get(history))["ttl"] == 120
    await read_cache.invalidate("change")
    assert await cached_redis.get(history) is None
    await read_cache.get_or_load("change", loader, ttl=60, max_ttl=300)
    assert json.loads(await cached_redis.get(history))["ttl"] == 60


@pytest.mark.parametrize("history_value", [None, "not-json", "[]"])
async def test_missing_or_invalid_history_starts_at_base_ttl(cached_redis, history_value):
    key, _, history = read_cache._keys("no-history")
    if history_value is not None:
        await cached_redis.set(history, history_value, ex=300)
    await read_cache.get_or_load("no-history", AsyncMock(return_value="ready"), ttl=60, max_ttl=300)
    assert 59_000 <= await cached_redis.pttl(key) <= 60_000
    assert json.loads(await cached_redis.get(history))["ttl"] == 60


async def test_failed_revalidation_does_not_renew_history(cached_redis):
    await read_cache.get_or_load("failure", AsyncMock(return_value="ready"), ttl=60, max_ttl=300)
    key, token, history = read_cache._keys("failure")
    await cached_redis.delete(key)
    previous, remaining = await cached_redis.get(history), await cached_redis.pttl(history)
    with pytest.raises(RuntimeError, match="database down"):
        await read_cache.get_or_load("failure", AsyncMock(side_effect=RuntimeError("database down")), ttl=60, max_ttl=300)
    assert await cached_redis.get(key) is None
    assert await cached_redis.get(token) is None
    assert await cached_redis.get(history) == previous
    assert await cached_redis.pttl(history) <= remaining


async def test_invalidated_fill_cannot_extend_new_renewal_history(cached_redis):
    old = await read_cache.lookup("race")
    await read_cache.invalidate("race")
    new = await read_cache.lookup("race")
    assert await read_cache.store(new, "same", ttl=60, max_ttl=300)
    key, _, history = read_cache._keys("race")
    previous, remaining = await cached_redis.get(history), await cached_redis.pttl(history)
    # Even an unchanged stale fill must not increment or refresh the new history.
    assert not await read_cache.store(old, "same", ttl=60, max_ttl=300)
    assert await cached_redis.get(history) == previous
    assert await cached_redis.pttl(history) <= remaining
    assert 59_000 <= await cached_redis.pttl(key) <= 60_000


@pytest.mark.parametrize("value", [None, [], {}])
async def test_empty_results_never_get_longer_renewals(cached_redis, value):
    key, _, history = read_cache._keys("empty-renewal")
    # A previously populated key must also forget its long TTL when it empties.
    await read_cache.get_or_load("empty-renewal", AsyncMock(return_value="previous"), ttl=60, max_ttl=300)
    loader = AsyncMock(return_value=value)
    for _ in range(2):
        await cached_redis.delete(key)
        assert await read_cache.get_or_load("empty-renewal", loader, ttl=60, max_ttl=300) == value
        assert 4000 <= await cached_redis.pttl(key) <= 5000
        assert await cached_redis.get(history) is None


async def test_concurrent_revalidation_only_advances_ttl_once(cached_redis):
    key, _, history = read_cache._keys("concurrent-renewal")
    await read_cache.get_or_load("concurrent-renewal", AsyncMock(return_value="ready"), ttl=60, max_ttl=300)
    await cached_redis.delete(key)
    async def load():
        await asyncio.sleep(0.025)
        return "ready"
    loader = AsyncMock(side_effect=load)
    assert await asyncio.gather(*(
        read_cache.get_or_load("concurrent-renewal", loader, ttl=60, max_ttl=300) for _ in range(50)
    )) == ["ready"] * 50
    loader.assert_awaited_once()
    assert json.loads(await cached_redis.get(history))["ttl"] == 120


async def test_cancelled_waiter_does_not_cancel_other_waiter(cached_redis):
    started, finish = asyncio.Event(), asyncio.Event()
    async def load():
        started.set()
        await finish.wait()
        return "ready"
    first = asyncio.create_task(read_cache.get_or_load("cancel", load, ttl=60))
    await started.wait()
    second = asyncio.create_task(read_cache.get_or_load("cancel", load, ttl=60))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert await second == "ready"


async def test_last_cancelled_waiter_closes_loader_before_return(cached_redis):
    started, closed = asyncio.Event(), asyncio.Event()
    async def load():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
    task = asyncio.create_task(read_cache.get_or_load("cancel-all", load, ttl=60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert (await read_cache.lookup("cancel-all")).token is not None


async def test_hung_redis_has_bounded_fallback(monkeypatch):
    async def hang():
        await asyncio.Event().wait()
    monkeypatch.setattr(read_cache, "_get_redis", hang)
    async with asyncio.timeout(0.5):
        assert await read_cache.get_or_load("down", AsyncMock(return_value="db"), ttl=60) == "db"


async def test_actor_and_entity_keys_are_isolated(cached_redis):
    for key in ["entity1:alice", "entity2:alice", "entity1:bob"]:
        assert await read_cache.get_or_load(key, AsyncMock(return_value=key), ttl=60) == key
    for key in ["entity1:alice", "entity2:alice", "entity1:bob"]:
        assert (await read_cache.lookup(key)).value == key


@pytest.mark.parametrize("outer_commit", [True, False])
@pytest.mark.parametrize("nested_commit", [True, False])
async def test_invalidation_waits_for_outer_commit_and_discards_savepoint_rollback(monkeypatch, outer_commit, nested_commit):
    invalidate = AsyncMock()
    monkeypatch.setattr(read_cache, "invalidate", invalidate)
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with async_sessionmaker(engine)() as db:
            await db.scalar(select(literal(1)))
            read_cache.invalidate_after_commit(db, "outer")
            nested = await db.begin_nested()
            read_cache.invalidate_after_commit(db, "nested")
            await (nested.commit() if nested_commit else nested.rollback())
            invalidate.assert_not_awaited()
            await (db.commit() if outer_commit else db.rollback())
            if outer_commit:
                assert set(invalidate.await_args.args) == ({"outer", "nested"} if nested_commit else {"outer"})
            else:
                invalidate.assert_not_awaited()
            # Reusing the same Session must never resurrect rolled-back keys.
            await db.scalar(select(literal(1)))
            await db.commit()
            assert invalidate.await_count == int(outer_commit)
    finally:
        await engine.dispose()
