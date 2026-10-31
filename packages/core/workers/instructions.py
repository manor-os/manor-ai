"""Out-of-band instructions the server queues for one worker.

Delivered in the worker's next heartbeat's ``instructions`` (a long-polling
worker is woken at once). Each instruction is ``{"type", "payload"}`` and is
delivered at most once; an undelivered one expires with its list. Without
Redis nothing is queued and the caller is told so.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_KEY = "manor:worker:instructions:{worker_id}"
_TTL_SECONDS = 120
_MAX_PER_BEAT = 10


async def _redis():
    from packages.core.cache import get_redis

    return await get_redis()


async def queue_instruction(worker_id: str, kind: str, payload: dict[str, Any]) -> bool:
    """Queue one instruction for ``worker_id`` and wake it; False when it could not be queued."""

    from packages.core.workers.wake import wake_worker

    try:
        redis = await _redis()
        if redis is None:
            return False
        key = _KEY.format(worker_id=worker_id)
        await redis.rpush(key, json.dumps({"type": kind, "payload": payload}))
        await redis.expire(key, _TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the caller tells its user it could not be sent
        logger.warning("[workers.instructions] queue for %s failed: %s", worker_id, exc)
        return False
    await wake_worker(worker_id)
    return True


async def drain_instructions(worker_id: str) -> list[dict[str, Any]]:
    """The instructions queued for ``worker_id`` (removed as they are read)."""

    try:
        redis = await _redis()
        if redis is None:
            return []
        key = _KEY.format(worker_id=worker_id)
        raw = await redis.lpop(key, _MAX_PER_BEAT)
    except Exception as exc:  # noqa: BLE001 - delivered on a later beat
        logger.warning("[workers.instructions] drain for %s failed: %s", worker_id, exc)
        return []
    out = []
    for item in raw or []:
        try:
            value = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            out.append({"type": value["type"], "payload": value.get("payload") or {}})
    return out
