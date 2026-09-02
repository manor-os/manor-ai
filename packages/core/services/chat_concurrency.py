"""Active concurrency guard for long-lived chat/SSE streams."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from packages.core.cache import _get_redis

_Result = TypeVar("_Result")

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ChatConcurrencyConfig:
    enabled: bool
    global_limit: int
    per_instance_limit: int
    wait_timeout_seconds: float
    slot_ttl_seconds: int
    key: str = "manor:chat:stream:active"
    retry_interval_seconds: float = 0.05
    heartbeat_interval_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "ChatConcurrencyConfig":
        global_limit = int(os.getenv("CHAT_STREAM_MAX_CONCURRENCY_GLOBAL", "0"))
        per_instance_limit = int(os.getenv("CHAT_STREAM_MAX_CONCURRENCY_PER_INSTANCE", "0"))
        enabled = _env_bool(
            "CHAT_STREAM_CONCURRENCY_ENABLED",
            "true" if global_limit > 0 or per_instance_limit > 0 else "false",
        )
        return cls(
            enabled=enabled,
            global_limit=global_limit,
            per_instance_limit=per_instance_limit,
            wait_timeout_seconds=float(os.getenv("CHAT_STREAM_QUEUE_TIMEOUT_SECONDS", "0.5")),
            slot_ttl_seconds=int(os.getenv("CHAT_STREAM_SLOT_TTL_SECONDS", "900")),
            key=os.getenv("CHAT_STREAM_CONCURRENCY_KEY", "manor:chat:stream:active"),
            retry_interval_seconds=float(os.getenv("CHAT_STREAM_RETRY_INTERVAL_SECONDS", "0.05")),
            heartbeat_interval_seconds=float(os.getenv("CHAT_STREAM_HEARTBEAT_INTERVAL_SECONDS", "60")),
        )


class ChatConcurrencyExceeded(Exception):
    """Raised before an SSE response is created when no active slot is available."""

    def __init__(
        self,
        message: str = "Chat stream capacity exceeded",
        *,
        status_code: int = 503,
        retry_after_seconds: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ChatConcurrencyLease:
    def __init__(
        self,
        gate: "ChatConcurrencyGate",
        *,
        local_acquired: bool,
        redis_token: str | None,
    ) -> None:
        self._gate = gate
        self._local_acquired = local_acquired
        self._redis_token = redis_token
        self._released = False
        self._acquired_at = time.monotonic()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._gate._release(self._local_acquired, self._redis_token)

    async def renew(self) -> bool:
        if self._released or not self._redis_token:
            return False
        return await self._gate._renew(self._redis_token)

    async def run(self, operation: Awaitable[_Result]) -> _Result:
        """Keep a non-streaming operation's slot alive, including its cleanup."""
        task = asyncio.ensure_future(operation)
        heartbeat = asyncio.create_task(self._heartbeat_loop()) if self._redis_token else None
        try:
            if heartbeat:
                done, _ = await asyncio.wait((task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    raise ChatConcurrencyExceeded("Chat capacity lease was lost; please reconnect.")
            return await asyncio.shield(task)
        finally:
            task.cancel()
            # A call can drain already accepted provider I/O after hangup.
            # Keep renewing until that bounded settlement has finished.
            try:
                await asyncio.gather(task, return_exceptions=True)
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

    async def wrap(self, source: AsyncIterator[str]) -> AsyncIterator[str]:
        heartbeat_task: asyncio.Task | None = None
        if self._redis_token and self._gate.config.heartbeat_interval_seconds > 0:
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async for chunk in source:
                yield chunk
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            await self.release()

    async def _heartbeat_loop(self) -> None:
        interval = max(0.001, min(
            self._gate.config.heartbeat_interval_seconds or self._gate.config.slot_ttl_seconds / 3,
            max(0.001, self._gate.config.slot_ttl_seconds / 3),
        ))
        deadline = self._acquired_at + self._gate.config.slot_ttl_seconds
        while True:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            started = time.monotonic()
            remaining = deadline - started
            if remaining <= 0:
                return
            try:
                # A stalled Redis read must fail closed before the last
                # confirmed slot can expire and admit a second call.
                async with asyncio.timeout(min(interval, remaining)):
                    if not await self.renew():
                        return
            except TimeoutError:
                return
            # Redis stamps the slot before awaiting the renewal response.
            deadline = started + self._gate.config.slot_ttl_seconds


class ChatConcurrencyGate:
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
        *,
        redis_getter: Callable[[], object | Awaitable[object]] | None = None,
        config: ChatConcurrencyConfig | None = None,
        instance_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or ChatConcurrencyConfig.from_env()
        self._redis_getter = redis_getter or _get_redis
        self._instance_id = instance_id or os.getenv("MANOR_INSTANCE_ID") or socket.gethostname() or uuid.uuid4().hex
        self._clock = clock
        self._local_active = 0
        self._local_lock = threading.Lock()

    @property
    def local_active(self) -> int:
        with self._local_lock:
            return self._local_active

    async def acquire(self, *, scope: str = "chat") -> ChatConcurrencyLease:
        cfg = self.config
        if not cfg.enabled or (cfg.global_limit <= 0 and cfg.per_instance_limit <= 0):
            return ChatConcurrencyLease(self, local_acquired=False, redis_token=None)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, cfg.wait_timeout_seconds)
        retry_after = max(1, int(round(cfg.wait_timeout_seconds or cfg.retry_interval_seconds or 1)))

        while True:
            local_acquired = await self._try_acquire_local()
            redis_token: str | None = None
            if local_acquired:
                try:
                    redis_token = await self._try_acquire_global(scope=scope)
                    if cfg.global_limit <= 0 or redis_token:
                        return ChatConcurrencyLease(
                            self,
                            local_acquired=True,
                            redis_token=redis_token,
                        )
                finally:
                    if not redis_token and cfg.global_limit > 0:
                        await self._release_local()

            if loop.time() >= deadline:
                raise ChatConcurrencyExceeded(retry_after_seconds=retry_after)

            await asyncio.sleep(min(cfg.retry_interval_seconds, max(0.0, deadline - loop.time())))

    async def _try_acquire_local(self) -> bool:
        limit = self.config.per_instance_limit
        if limit <= 0:
            return True
        with self._local_lock:
            if self._local_active >= limit:
                return False
            self._local_active += 1
            return True

    async def _release_local(self) -> None:
        if self.config.per_instance_limit <= 0:
            return
        with self._local_lock:
            self._local_active = max(0, self._local_active - 1)

    async def _get_redis(self) -> object | None:
        redis = self._redis_getter()
        if inspect.isawaitable(redis):
            redis = await redis
        return redis

    async def _try_acquire_global(self, *, scope: str) -> str | None:
        cfg = self.config
        if cfg.global_limit <= 0:
            return None

        redis = await self._get_redis()
        if redis is None:
            raise ChatConcurrencyExceeded(
                "Chat stream capacity gate unavailable",
                status_code=503,
                retry_after_seconds=max(1, int(cfg.wait_timeout_seconds or 1)),
            )

        now = self._clock()
        token = f"{scope}:{self._instance_id}:{uuid.uuid4().hex}"
        try:
            acquired = await redis.eval(
                self._ACQUIRE_LUA,
                1,
                cfg.key,
                token,
                now,
                cfg.slot_ttl_seconds,
                cfg.global_limit,
            )
            return token if int(acquired or 0) == 1 else None
        except AttributeError:
            return await self._try_acquire_global_without_lua(redis, token=token, now=now)
        except ChatConcurrencyExceeded:
            raise
        except Exception as exc:
            logger.warning("Chat stream Redis concurrency gate failed closed: %s", exc)
            raise ChatConcurrencyExceeded(
                "Chat stream capacity gate unavailable",
                status_code=503,
                retry_after_seconds=max(1, int(cfg.wait_timeout_seconds or 1)),
            ) from exc

    async def _try_acquire_global_without_lua(self, redis: object, *, token: str, now: float) -> str | None:
        cfg = self.config
        await redis.zremrangebyscore(cfg.key, 0, now - cfg.slot_ttl_seconds)
        current = int(await redis.zcard(cfg.key))
        if current >= cfg.global_limit:
            await redis.expire(cfg.key, cfg.slot_ttl_seconds)
            return None
        await redis.zadd(cfg.key, {token: now})
        await redis.expire(cfg.key, cfg.slot_ttl_seconds)
        return token

    async def _release(self, local_acquired: bool, redis_token: str | None) -> None:
        if redis_token:
            try:
                redis = await self._get_redis()
                if redis is not None:
                    await redis.zrem(self.config.key, redis_token)
            except Exception:
                logger.debug("Chat stream slot release failed", exc_info=True)
        if local_acquired:
            await self._release_local()

    async def _renew(self, redis_token: str) -> bool:
        cfg = self.config
        if cfg.global_limit <= 0:
            return False
        try:
            redis = await self._get_redis()
            if redis is None:
                return False
            now = self._clock()
            try:
                renewed = await redis.eval(
                    self._RENEW_LUA,
                    1,
                    cfg.key,
                    redis_token,
                    now,
                    cfg.slot_ttl_seconds,
                )
                return int(renewed or 0) == 1
            except AttributeError:
                return await self._renew_without_lua(redis, token=redis_token, now=now)
        except Exception:
            logger.debug("Chat stream slot renew failed", exc_info=True)
            return False

    async def _renew_without_lua(self, redis: object, *, token: str, now: float) -> bool:
        zset = getattr(redis, "zsets", None)
        if isinstance(zset, dict):
            members = zset.setdefault(self.config.key, {})
            if token not in members:
                return False
            await redis.zadd(self.config.key, {token: now})
            await redis.expire(self.config.key, self.config.slot_ttl_seconds)
            return True
        zscore = getattr(redis, "zscore", None)
        if zscore is None or await zscore(self.config.key, token) is None:
            return False
        await redis.zadd(self.config.key, {token: now})
        await redis.expire(self.config.key, self.config.slot_ttl_seconds)
        return True


_default_gate = ChatConcurrencyGate()


def chat_stream_local_active_count() -> int:
    return _default_gate.local_active


async def acquire_chat_stream_slot(*, scope: str = "chat") -> ChatConcurrencyLease:
    return await _default_gate.acquire(scope=scope)
