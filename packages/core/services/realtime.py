"""Real-time event push -- used by services to notify connected clients.

Pattern: every service mutation that creates / updates / deletes a
user-visible resource should call the matching push helper just after
the commit. The client's `useWebSocket` hook in
``apps/web/src/lib/websocket.ts`` turns the event into a React Query
invalidation, so the list rehydrates without waiting for a poll.

Helpers come in two flavours:
  - ``push_*``        — targets a single user (the owner / actor).
  - ``broadcast_*``   — fans out to every connected socket for an
                        entity. Use when the change is visible to the
                        whole org and you don't have a specific user
                        in mind (e.g. agent-driven mutations).

All helpers are best-effort: they swallow exceptions so a dropped WS
never breaks the write path.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# ── Per-user push ──────────────────────────────────────────────────────────

async def push_notification(user_id: str, notification: dict):
    await _send_to_user(user_id, "notification", notification)


async def push_task_update(user_id: str, task: dict):
    await _send_to_user(user_id, "task_update", task)


async def push_job_update(user_id: str, job: dict):
    """Scheduled job created / updated / deleted."""
    await _send_to_user(user_id, "job_update", job)


async def push_goal_progress(user_id: str, goal: dict):
    await _send_to_user(user_id, "goal_progress", goal)


# ── Entity-wide broadcast ──────────────────────────────────────────────────

async def broadcast_task_update(entity_id: str, task: dict):
    await _broadcast(entity_id, "task_update", task)


async def broadcast_job_update(entity_id: str, job: dict):
    await _broadcast(entity_id, "job_update", job)


# ── Multi-target convenience ───────────────────────────────────────────────

async def push_task_update_multi(
    user_ids: Iterable[Optional[str]], task: dict,
):
    """Send one task_update event to a set of users (dedup + drop None)."""
    seen: set[str] = set()
    for uid in user_ids:
        if uid and uid not in seen:
            seen.add(uid)
            await push_task_update(uid, task)


# ── Internals ──────────────────────────────────────────────────────────────

async def _send_to_user(user_id: str, event: str, data: dict):
    if not user_id:
        return
    try:
        await _redis_publish({"target": "user", "user_id": user_id, "event": event, "data": data})
    except Exception as e:
        logger.debug("Could not push %s to user %s: %s", event, user_id, e)


async def _broadcast(entity_id: str, event: str, data: dict):
    if not entity_id:
        return
    try:
        await _redis_publish({"target": "entity", "entity_id": entity_id, "event": event, "data": data})
    except Exception as e:
        logger.debug("Could not broadcast %s to entity %s: %s", event, entity_id, e)


async def _redis_publish(payload: dict):
    """Publish a WS event via Redis pub/sub so the API relay picks it up."""
    import json
    from packages.core.cache import _get_redis
    r = await _get_redis()
    if r:
        await r.publish("manor:ws_broadcast", json.dumps(payload))
