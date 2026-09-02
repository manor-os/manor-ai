"""Small Redis/local concurrency gates for sandbox resources."""
from __future__ import annotations

import asyncio
import inspect
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


async def _wait_for_task_terminal(task: asyncio.Task[object]) -> None:
    """Wait through caller cancellation; callers must pass tasks with bounded I/O."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except BaseException:
            pass


@dataclass(frozen=True)
class SandboxGateConfig:
    redis_url: str = ""
    global_max_active: int = 0
    global_max_executing: int = 0
    instance_max_executing: int = 1
    ttl_seconds: int = 900
    wait_timeout_seconds: float = 0.0
    operation_timeout_seconds: float = 5.0
    retry_interval_seconds: float = 0.05
    active_key: str = "manor:sandbox:active"
    executing_key: str = "manor:sandbox:executing"


class SandboxConcurrencyExceeded(RuntimeError):
    pass


class LocalActiveCapacity:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self._reserved = 0
        self._lock = asyncio.Lock()

    @property
    def reserved(self) -> int:
        return self._reserved

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.limit <= 0 or self._reserved >= self.limit:
                return False
            self._reserved += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._reserved = max(0, self._reserved - 1)


class SandboxConcurrencyLease:
    def __init__(
        self,
        gate: "SandboxConcurrencyGate",
        *,
        local_execute: bool = False,
        redis_key: str | None = None,
        redis_token: str | None = None,
    ) -> None:
        self._gate = gate
        self._local_execute = local_execute
        self._redis_key = redis_key
        self._redis_token = redis_token
        self._released = False
        self._release_lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            release_task = asyncio.create_task(
                self._gate.release(
                    local_execute=self._local_execute,
                    redis_key=self._redis_key,
                    redis_token=self._redis_token,
                )
            )
            try:
                await asyncio.shield(release_task)
            except BaseException:
                await _wait_for_task_terminal(release_task)
                self._local_execute = False
                try:
                    release_task.result()
                except BaseException:
                    pass
                else:
                    self._released = True
                raise
            self._local_execute = False
            self._released = True

    async def renew(self) -> bool:
        if self._released:
            return False
        if not self._redis_key and not self._redis_token:
            return True
        if not self._redis_key or not self._redis_token:
            return False
        return await self._gate.renew(self._redis_key, self._redis_token)

    async def __aenter__(self) -> "SandboxConcurrencyLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


class SandboxConcurrencyGate:
    _ACQUIRE_LUA = """
local key = KEYS[1]
local token = ARGV[1]
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, 0, now - ttl)
local count = redis.call('ZCARD', key)
if count >= limit then
  redis.call('EXPIRE', key, ttl)
  return 0
end
redis.call('ZADD', key, now, token)
redis.call('EXPIRE', key, ttl)
return 1
"""
    _RENEW_LUA = """
local key = KEYS[1]
local token = ARGV[1]
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if redis.call('ZSCORE', key, token) == false then
  return 0
end
redis.call('ZADD', key, 'XX', now, token)
redis.call('EXPIRE', key, ttl)
return 1
"""

    def __init__(
        self,
        config: SandboxGateConfig,
        *,
        redis_getter: Callable[[], object | Awaitable[object]] | None = None,
        instance_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._redis_getter = redis_getter or self._default_redis_getter
        self._instance_id = instance_id or socket.gethostname() or uuid.uuid4().hex
        self._clock = clock
        self._execute_semaphore = (
            asyncio.Semaphore(config.instance_max_executing)
            if config.instance_max_executing > 0
            else None
        )
        self._executing = 0
        self._redis_client: object | None = None

    @property
    def executing(self) -> int:
        return self._executing

    async def acquire_active(self) -> SandboxConcurrencyLease:
        if self.config.global_max_active <= 0:
            return SandboxConcurrencyLease(self)
        token = await self._acquire_redis_slot_cancellation_safe(
            key=self.config.active_key,
            limit=self.config.global_max_active,
            label="active sandbox",
        )
        return SandboxConcurrencyLease(self, redis_key=self.config.active_key, redis_token=token)

    async def acquire_execute(self) -> SandboxConcurrencyLease:
        try:
            if self._execute_semaphore is not None:
                if self.config.wait_timeout_seconds <= 0:
                    if self._execute_semaphore.locked():
                        raise TimeoutError
                    await self._execute_semaphore.acquire()
                else:
                    await asyncio.wait_for(
                        self._execute_semaphore.acquire(),
                        timeout=self.config.wait_timeout_seconds,
                    )
            self._executing += 1
        except TimeoutError as exc:
            raise SandboxConcurrencyExceeded(
                f"Max executing sandbox instance limit reached ({self.config.instance_max_executing})"
            ) from exc
        redis_key: str | None = None
        token: str | None = None
        try:
            if self.config.global_max_executing > 0:
                redis_key = self.config.executing_key
                token = await self._acquire_redis_slot_cancellation_safe(
                    key=redis_key,
                    limit=self.config.global_max_executing,
                    label="executing sandbox",
                )
            return SandboxConcurrencyLease(
                self,
                local_execute=True,
                redis_key=redis_key,
                redis_token=token,
            )
        except BaseException:
            self._release_local_execute()
            raise

    async def release(
        self,
        *,
        local_execute: bool = False,
        redis_key: str | None = None,
        redis_token: str | None = None,
    ) -> None:
        try:
            if redis_key and redis_token:
                try:
                    await self._remove_redis_slot(redis_key, redis_token)
                except Exception:
                    logger.debug("Sandbox Redis slot release failed", exc_info=True)
        finally:
            if local_execute:
                self._release_local_execute()

    def _release_local_execute(self) -> None:
        self._executing = max(0, self._executing - 1)
        if self._execute_semaphore is not None:
            self._execute_semaphore.release()

    async def renew(self, redis_key: str, redis_token: str) -> bool:
        async def _renew(redis: object) -> bool:
            now = self._clock()
            try:
                renewed = await redis.eval(
                    self._RENEW_LUA,
                    1,
                    redis_key,
                    redis_token,
                    now,
                    self.config.ttl_seconds,
                )
                return int(renewed or 0) == 1
            except AttributeError:
                zsets = getattr(redis, "zsets", None)
                if isinstance(zsets, dict):
                    members = zsets.setdefault(redis_key, {})
                    if redis_token not in members:
                        return False
                else:
                    zscore = getattr(redis, "zscore", None)
                    if zscore is None or await zscore(redis_key, redis_token) is None:
                        return False
                await redis.zadd(redis_key, {redis_token: now})
                await redis.expire(redis_key, self.config.ttl_seconds)
                return True

        try:
            return bool(await self._run_redis_operation(_renew))
        except SandboxConcurrencyExceeded:
            raise
        except Exception as exc:
            logger.warning("Sandbox Redis slot renew failed", exc_info=True)
            raise SandboxConcurrencyExceeded("Sandbox concurrency gate unavailable") from exc

    async def _default_redis_getter(self) -> object:
        if not self.config.redis_url:
            raise SandboxConcurrencyExceeded("Redis URL is required for sandbox global concurrency gate")
        if self._redis_client is None:
            import redis.asyncio as aioredis

            self._redis_client = aioredis.from_url(self.config.redis_url, decode_responses=True)
        return self._redis_client

    async def _get_redis(self) -> object:
        redis = self._redis_getter()
        if inspect.isawaitable(redis):
            redis = await redis
        if redis is None:
            raise SandboxConcurrencyExceeded("Redis is required for sandbox global concurrency gate")
        return redis

    async def _acquire_redis_slot_cancellation_safe(
        self,
        *,
        key: str,
        limit: int,
        label: str,
    ) -> str:
        token = f"{self._instance_id}:{uuid.uuid4().hex}"
        admission_task = asyncio.create_task(
            self._acquire_redis_slot(key=key, limit=limit, label=label, token=token)
        )
        try:
            return await asyncio.shield(admission_task)
        except BaseException:
            await _wait_for_task_terminal(admission_task)
            cleanup_task = asyncio.create_task(self._remove_redis_slot(key, token))
            await _wait_for_task_terminal(cleanup_task)
            try:
                cleanup_task.result()
            except BaseException:
                logger.warning(
                    "Sandbox Redis admission compensation failed for %s",
                    key,
                    exc_info=True,
                )
            raise

    async def _remove_redis_slot(self, key: str, token: str) -> None:
        async def _remove(redis: object) -> None:
            await redis.zrem(key, token)

        await self._run_redis_operation(_remove)

    async def _acquire_redis_slot(
        self,
        *,
        key: str,
        limit: int,
        label: str,
        token: str,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + max(0.0, self.config.wait_timeout_seconds)
        while True:
            now = self._clock()

            async def _try_acquire(redis: object) -> bool:
                try:
                    acquired = await redis.eval(
                        self._ACQUIRE_LUA,
                        1,
                        key,
                        token,
                        now,
                        self.config.ttl_seconds,
                        limit,
                    )
                    return int(acquired or 0) == 1
                except AttributeError:
                    await redis.zremrangebyscore(key, 0, now - self.config.ttl_seconds)
                    if int(await redis.zcard(key)) < limit:
                        await redis.zadd(key, {token: now})
                        await redis.expire(key, self.config.ttl_seconds)
                        return True
                    await redis.expire(key, self.config.ttl_seconds)
                    return False

            try:
                if await self._run_redis_operation(_try_acquire):
                    return token
            except SandboxConcurrencyExceeded:
                raise
            except Exception as exc:
                logger.warning("Sandbox Redis concurrency gate failed closed: %s", exc)
                raise SandboxConcurrencyExceeded("Sandbox concurrency gate unavailable") from exc

            if asyncio.get_running_loop().time() >= deadline:
                raise SandboxConcurrencyExceeded(f"Max {label} limit reached ({limit})")
            await asyncio.sleep(min(self.config.retry_interval_seconds, max(0.0, deadline - asyncio.get_running_loop().time())))

    async def _run_redis_operation(
        self,
        operation: Callable[[object], Awaitable[object]],
    ) -> object:
        async def _run() -> object:
            redis = await self._get_redis()
            return await operation(redis)

        try:
            return await asyncio.wait_for(
                _run(),
                timeout=max(0.001, self.config.operation_timeout_seconds),
            )
        except TimeoutError as exc:
            raise SandboxConcurrencyExceeded("Sandbox concurrency gate unavailable") from exc
