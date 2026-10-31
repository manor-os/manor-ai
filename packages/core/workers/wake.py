"""Wake a long-polling worker when work addressed to it is queued.

An idle worker's heartbeat may wait (``HeartbeatRequest.wait_seconds``) for
work targeted at it instead of polling on a timer; queuing such work pushes a
token onto the worker's list in Redis and the waiting heartbeat checks out at
once. A token nobody consumed expires with the list, so a worker that was not
waiting at worst checks out one extra time. Without Redis nobody waits.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_KEY = "manor:worker:wake:{worker_id}"
# Longer than any wait, so a wake pushed just before a heartbeat starts waiting is still there.
_WAKE_TTL_SECONDS = 30
MAX_WAIT_SECONDS = 25


def _key(worker_id: str) -> str:
    return _KEY.format(worker_id=worker_id)


async def _redis():
    from packages.core.cache import get_redis

    return await get_redis()


async def wake_worker(worker_id: str) -> None:
    """Wake ``worker_id`` if it is waiting. Never raises: the worker polls anyway."""

    if not worker_id:
        return
    try:
        redis = await _redis()
        if redis is None:
            return
        key = _key(worker_id)
        await redis.lpush(key, "1")
        await redis.expire(key, _WAKE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a missed wake costs one poll interval
        logger.warning("[workers.wake] wake %s failed: %s", worker_id, exc)


async def wait_for_wake(worker_id: str, timeout: float) -> bool | None:
    """Wait up to ``timeout`` seconds for work addressed to ``worker_id``.

    True when woken, False when the wait ran out, None when nothing could wait
    (no Redis): the caller then keeps its usual poll interval.
    """

    try:
        redis = await _redis()
        if redis is None:
            return None
        from packages.core.cache import blocking_slice_seconds

        key = _key(worker_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1, min(MAX_WAIT_SECONDS, int(timeout)))
        woken = None
        # One BLPOP longer than the client's read timeout fails instead of
        # waiting: the worker then polls on its timer and every instruction
        # (an open live view) is late.
        while woken is None:
            wait = blocking_slice_seconds(redis, deadline - loop.time())
            if wait <= 0:
                break
            woken = await redis.blpop([key], timeout=wait)
        if woken is not None:
            # One check-out takes everything queued: later wakes are spent too.
            await redis.delete(key)
        return woken is not None
    except Exception as exc:  # noqa: BLE001 - the heartbeat answers as if nothing came
        logger.warning("[workers.wake] wait %s failed: %s", worker_id, exc)
        return None
