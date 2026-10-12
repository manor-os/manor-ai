"""Bounded, optional caching for derived reads, never authorization decisions.

A fill owns a short token. Invalidation deletes the value, token and TTL history,
so a query that started before a committed write cannot repopulate stale data.
Redis failure still permits a DB read; database admission bounds that fallback.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from uuid import uuid4
from weakref import WeakKeyDictionary

from packages.core.cache import _get_redis

logger = logging.getLogger(__name__)
IO_TIMEOUT = 0.1
FILL_TTL = 10
_inflight = WeakKeyDictionary()

_READ = """
local value = redis.call('GET', KEYS[1])
if value then return {1, value} end
if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2]) then
    return {2}
end
return {0}
"""
_STORE = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
local base, maximum = tonumber(ARGV[3]), tonumber(ARGV[4])
local ttl = base
if maximum > base then
    local history = redis.call('GET', KEYS[3])
    if history then
        local ok, previous = pcall(cjson.decode, history)
        if ok and type(previous) == 'table' and previous.digest == ARGV[5] and tonumber(previous.ttl) then
            ttl = math.min(maximum, math.max(base, tonumber(previous.ttl) * 2))
        end
    end
    redis.call('SET', KEYS[3], cjson.encode({digest=ARGV[5], ttl=ttl}), 'EX', maximum + tonumber(ARGV[6]))
else
    redis.call('DEL', KEYS[3])
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
redis.call('DEL', KEYS[2])
return 1
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


def _keys(key: str) -> tuple[str, str, str]:
    digest = sha256(key.encode()).hexdigest()
    base = f"manor:read:v1:{{{digest}}}"
    return base, f"{base}:fill", f"{base}:renewal"


@dataclass(frozen=True)
class CacheRead:
    key: str
    hit: bool = False
    value: object = None
    token: str | None = None


async def lookup(key: str) -> CacheRead:
    """Wait briefly for another Pod's fill; cache I/O including waits is bounded."""
    token = uuid4().hex
    try:
        async with asyncio.timeout(IO_TIMEOUT):
            redis = await _get_redis()
            if redis is not None:
                while True:
                    result = await redis.eval(_READ, 2, *_keys(key)[:2], token, FILL_TTL)
                    if result[0] == 1:
                        return CacheRead(key, hit=True, value=json.loads(result[1]))
                    if result[0] == 2:
                        return CacheRead(key, token=token)
                    await asyncio.sleep(0.02)
    except Exception:
        logger.debug("Optional read cache unavailable", exc_info=True)
    return CacheRead(key)


async def store(read: CacheRead, value, *, ttl: int, max_ttl: int | None = None) -> bool:
    """After an authoritative load, extend unchanged data up to max_ttl.

    Only the fill owner can update the value or its history. Cache hits never
    extend expiry; missed invalidations are still bounded by max_ttl.
    """
    if read.token is None:
        return False
    try:
        async with asyncio.timeout(IO_TIMEOUT):
            redis = await _get_redis()
            if redis is not None:
                maximum = max(ttl, max_ttl or ttl)
                payload = json.dumps(value, sort_keys=maximum > ttl)
                return bool(await redis.eval(
                    _STORE, 3, *_keys(read.key), read.token, payload, ttl, maximum,
                    sha256(payload.encode()).hexdigest(), FILL_TTL,
                ))
    except Exception:
        logger.debug("Optional read cache fill failed", exc_info=True)
    return False


async def abandon(read: CacheRead) -> None:
    if read.token is None:
        return
    try:
        async with asyncio.timeout(IO_TIMEOUT):
            redis = await _get_redis()
            if redis is not None:
                await redis.eval(_RELEASE, 1, _keys(read.key)[1], read.token)
    except Exception:
        logger.debug("Optional read cache release failed", exc_info=True)


async def invalidate(*keys: str) -> None:
    if not keys:
        return
    try:
        async with asyncio.timeout(IO_TIMEOUT):
            redis = await _get_redis()
            if redis is not None:
                # Each group shares a Redis cluster hash slot. Delete the value,
                # in-flight token and renewal history together.
                await asyncio.gather(*(redis.delete(*_keys(key)) for key in set(keys)))
    except Exception:
        # The DB is already committed. This must never cause a write retry.
        logger.warning("Read cache invalidation unavailable; values expire by TTL")


async def get_or_load(key: str, load, *, ttl: int, empty_ttl: int = 5, max_ttl: int | None = None):
    """Coalesce reads per event loop, including when Redis is unavailable.

    The loader MUST own and close its short DB session. Never capture a
    request's borrowed AsyncSession here: another waiter can outlive it.
    With max_ttl, unchanged successful loads double TTL up to that bound.
    Empty results keep a short fixed TTL so newly created data appears promptly.
    """
    loop = asyncio.get_running_loop()
    pending = _inflight.setdefault(loop, {})
    entry = pending.get(key)
    if entry is None:
        async def fill():
            read = await lookup(key)
            if read.hit:
                return read.value
            try:
                value = await load()
                await store(
                    read, value, ttl=ttl if value else min(ttl, empty_ttl),
                    max_ttl=max_ttl if value else None,
                )
                return value
            finally:
                await abandon(read)

        entry = [asyncio.create_task(fill()), 0]
        pending[key] = entry
    entry[1] += 1
    try:
        return deepcopy(await asyncio.shield(entry[0]))
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            pending.pop(key, None)
            if not entry[0].done():
                entry[0].cancel()
            # Await cancellation so a disposable worker loop cannot close
            # before the loader releases its DB session/engine.
            try:
                await entry[0]
            except BaseException:
                pass


def invalidate_after_commit(db, *keys: str) -> None:
    """Queue keys on an AsyncSession's transaction; rollback discards them.

    Per-session listeners run after DatabaseSession releases admission. The
    greenlet bridge awaits bounded Redis I/O after connections have closed,
    without untracked background tasks or changing the caller's transaction.
    """
    from sqlalchemy import event
    from sqlalchemy.util.concurrency import await_only, in_greenlet

    session = getattr(db, "sync_session", db)
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("Cache invalidation requires an active transaction")
    state = session.info.setdefault("read_cache_invalidations", {})
    state.setdefault(transaction, set()).update(keys)
    if session.info.get("read_cache_listeners"):
        return
    session.info["read_cache_listeners"] = True
    committed = set()

    def after_commit(owner):
        committed.add(owner.get_nested_transaction() or owner.get_transaction())

    def after_end(owner, ended):
        queued = state.pop(ended, set())
        success = ended in committed
        committed.discard(ended)
        if success and ended.parent is not None:
            state.setdefault(ended.parent, set()).update(queued)
        elif success and queued:
            if in_greenlet():
                await_only(invalidate(*queued))
            else:
                logger.warning("Synchronous write skipped read cache invalidation; values expire by TTL")

    event.listen(session, "after_commit", after_commit)
    event.listen(session, "after_transaction_end", after_end)
