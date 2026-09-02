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

import asyncio
import logging
from typing import Iterable, Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_TASK_UPDATE_QUEUE_KEY = "realtime_task_updates"
_TASK_UPDATE_LISTENER_KEY = "realtime_task_update_listener"
_TASK_UPDATE_PUBLISH_TASKS: set[asyncio.Task[None]] = set()
_NOTIFICATION_QUEUE_KEY = "realtime_notifications"
_NOTIFICATION_LISTENER_KEY = "realtime_notification_listener"
_NOTIFICATION_PUBLISH_TASKS: set[asyncio.Task[None]] = set()


# ── Per-user push ──────────────────────────────────────────────────────────

async def push_notification(
    user_id: str,
    notification: dict,
    *,
    entity_id: str,
    workspace_id: str | None = None,
):
    await _send_to_user(
        user_id,
        entity_id,
        "notification",
        notification,
        workspace_id=workspace_id,
    )


async def push_task_update(user_id: str, task: dict, *, entity_id: str):
    await _send_to_user(user_id, entity_id, "task_update", task)


async def push_job_update(user_id: str, job: dict, *, entity_id: str):
    """Scheduled job created / updated / deleted."""
    await _send_to_user(user_id, entity_id, "job_update", job)


async def push_goal_progress(user_id: str, goal: dict, *, entity_id: str):
    await _send_to_user(user_id, entity_id, "goal_progress", goal)


async def push_chat_stream_snapshot(user_id: str, snapshot: dict, *, entity_id: str):
    """In-progress state of a personal chat turn, for tabs that are not the streamer.

    A personal conversation streams over the SSE body of the POST that started
    it, so the transcript only exists in the tab that sent the message. Reload
    that page and the connection dies while the turn keeps running server-side —
    with no second channel the reader is stuck on the last DB checkpoint until
    they reload again. These snapshots are that second channel.

    Unlike the other helpers this is not a "go refetch" ping: ``snapshot``
    carries the reply itself (partial text, tool cards, assistant blocks), so
    the receiver renders straight from it. Keep the payload JSON-primitive —
    ``_redis_publish`` uses a bare ``json.dumps`` and a raise there is swallowed
    at DEBUG, which looks exactly like the feature not working.
    """
    await _send_to_user(user_id, entity_id, "chat_stream_snapshot", snapshot)


# ── Entity-wide broadcast ──────────────────────────────────────────────────

async def broadcast_task_update(
    entity_id: str,
    task: dict,
    *,
    workspace_id: Optional[str] = None,
):
    if workspace_id:
        await _broadcast_workspace(entity_id, workspace_id, "task_update", task)
        return
    await _broadcast(entity_id, "task_update", task)


async def broadcast_task_runtime_update(
    entity_id: str,
    *,
    task_id: str,
    workspace_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    status: Optional[str] = None,
    title: Optional[str] = None,
    event: str = "runtime_updated",
):
    """Invalidate every client projection owned by a Task runtime change.

    Runtime services often update Task, Plan, and Step rows in one committed
    transition.  The browser only needs stable ids here: React Query re-reads
    the committed rows instead of trusting a second, partial copy of them in
    the websocket payload.
    """
    payload = {
        "id": task_id,
        "task_id": task_id,
        "event": event,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    if status:
        payload["status"] = status
    if title:
        payload["title"] = title
    await broadcast_task_update(
        entity_id,
        payload,
        workspace_id=workspace_id,
    )


async def broadcast_job_update(entity_id: str, job: dict):
    await _broadcast(entity_id, "job_update", job)


# ── Multi-target convenience ───────────────────────────────────────────────

async def push_task_update_multi(
    user_ids: Iterable[Optional[str]], task: dict, *, entity_id: str,
):
    """Send one task_update event to a set of users (dedup + drop None)."""
    seen: set[str] = set()
    for uid in user_ids:
        if uid and uid not in seen:
            seen.add(uid)
            await push_task_update(uid, task, entity_id=entity_id)


async def _publish_task_update(
    entity_id: str,
    task: dict,
    *,
    workspace_id: Optional[str],
    user_ids: tuple[Optional[str], ...],
) -> None:
    if not workspace_id:
        await push_task_update_multi(user_ids, task, entity_id=entity_id)
    await broadcast_task_update(
        entity_id,
        task,
        workspace_id=workspace_id,
    )


def _schedule_task_update_publish(
    loop: asyncio.AbstractEventLoop,
    entity_id: str,
    task: dict,
    workspace_id: Optional[str],
    user_ids: tuple[Optional[str], ...],
) -> None:
    if loop.is_closed():
        return

    def schedule() -> None:
        publish = loop.create_task(_publish_task_update(
            entity_id,
            task,
            workspace_id=workspace_id,
            user_ids=user_ids,
        ))
        _TASK_UPDATE_PUBLISH_TASKS.add(publish)

        def finished(done: asyncio.Task[None]) -> None:
            _TASK_UPDATE_PUBLISH_TASKS.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("Could not publish committed Task update", exc_info=True)

        publish.add_done_callback(finished)

    try:
        if asyncio.get_running_loop() is loop:
            schedule()
            return
    except RuntimeError:
        pass
    if loop.is_running():
        loop.call_soon_threadsafe(schedule)


def _task_update_transaction_descends_from(transaction, ancestor) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


def _publish_task_updates_after_commit(session) -> None:
    if session.in_nested_transaction():
        return
    queued = session.info.pop(_TASK_UPDATE_QUEUE_KEY, [])
    for loop, entity_id, task, workspace_id, user_ids, _owner in queued:
        _schedule_task_update_publish(
            loop,
            entity_id,
            task,
            workspace_id,
            user_ids,
        )


def _clear_task_updates_after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_TASK_UPDATE_QUEUE_KEY, None)


def _clear_task_updates_after_soft_rollback(session, previous_transaction) -> None:
    if not previous_transaction.nested:
        return
    queued = session.info.get(_TASK_UPDATE_QUEUE_KEY, [])
    session.info[_TASK_UPDATE_QUEUE_KEY] = [
        item
        for item in queued
        if not _task_update_transaction_descends_from(item[5], previous_transaction)
    ]


def queue_task_update_after_commit(
    db: AsyncSession,
    entity_id: str,
    task: dict,
    *,
    workspace_id: Optional[str] = None,
    user_ids: Iterable[Optional[str]] = (),
) -> None:
    """Publish a Task invalidation only after its owning transaction commits."""
    sync_session = db.sync_session
    owner = sync_session.get_nested_transaction()
    sync_session.info.setdefault(_TASK_UPDATE_QUEUE_KEY, []).append((
        asyncio.get_running_loop(),
        entity_id,
        dict(task),
        workspace_id,
        tuple(user_ids),
        owner,
    ))
    if sync_session.info.get(_TASK_UPDATE_LISTENER_KEY):
        return
    event.listen(sync_session, "after_commit", _publish_task_updates_after_commit)
    event.listen(sync_session, "after_rollback", _clear_task_updates_after_rollback)
    event.listen(
        sync_session,
        "after_soft_rollback",
        _clear_task_updates_after_soft_rollback,
    )
    sync_session.info[_TASK_UPDATE_LISTENER_KEY] = True


def _schedule_notification_publish(
    loop: asyncio.AbstractEventLoop,
    *,
    entity_id: str,
    user_id: str,
    notification: dict,
    workspace_id: str | None,
) -> None:
    if loop.is_closed():
        return

    def schedule() -> None:
        scope = {"entity_id": entity_id}
        if workspace_id:
            scope["workspace_id"] = workspace_id
        publish = loop.create_task(push_notification(
            user_id,
            notification,
            **scope,
        ))
        _NOTIFICATION_PUBLISH_TASKS.add(publish)

        def finished(done: asyncio.Task[None]) -> None:
            _NOTIFICATION_PUBLISH_TASKS.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning(
                    "Could not publish committed notification",
                    exc_info=True,
                )

        publish.add_done_callback(finished)

    try:
        if asyncio.get_running_loop() is loop:
            schedule()
            return
    except RuntimeError:
        pass
    if loop.is_running():
        loop.call_soon_threadsafe(schedule)


def _publish_notifications_after_commit(session) -> None:
    if session.in_nested_transaction():
        return
    queued = session.info.pop(_NOTIFICATION_QUEUE_KEY, [])
    for loop, entity_id, user_id, notification, workspace_id, _owner in queued:
        _schedule_notification_publish(
            loop,
            entity_id=entity_id,
            user_id=user_id,
            notification=notification,
            workspace_id=workspace_id,
        )


def _clear_notifications_after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_NOTIFICATION_QUEUE_KEY, None)


def _clear_notifications_after_soft_rollback(session, previous_transaction) -> None:
    if not previous_transaction.nested:
        return
    queued = session.info.get(_NOTIFICATION_QUEUE_KEY, [])
    session.info[_NOTIFICATION_QUEUE_KEY] = [
        item
        for item in queued
        if not _task_update_transaction_descends_from(item[5], previous_transaction)
    ]


def queue_notification_after_commit(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    notification: dict,
    workspace_id: str | None = None,
) -> None:
    """Push one notification only after its database transaction commits."""
    sync_session = db.sync_session
    owner = sync_session.get_nested_transaction()
    sync_session.info.setdefault(_NOTIFICATION_QUEUE_KEY, []).append((
        asyncio.get_running_loop(),
        entity_id,
        user_id,
        dict(notification),
        workspace_id,
        owner,
    ))
    if sync_session.info.get(_NOTIFICATION_LISTENER_KEY):
        return
    event.listen(sync_session, "after_commit", _publish_notifications_after_commit)
    event.listen(sync_session, "after_rollback", _clear_notifications_after_rollback)
    event.listen(
        sync_session,
        "after_soft_rollback",
        _clear_notifications_after_soft_rollback,
    )
    sync_session.info[_NOTIFICATION_LISTENER_KEY] = True


# ── Internals ──────────────────────────────────────────────────────────────

async def _send_to_user(
    user_id: str,
    entity_id: str,
    event: str,
    data: dict,
    *,
    workspace_id: str | None = None,
):
    if not user_id or not entity_id:
        return
    try:
        await _redis_publish({
            "target": "workspace_user" if workspace_id else "user",
            "user_id": user_id,
            "entity_id": entity_id,
            **({"workspace_id": workspace_id} if workspace_id else {}),
            "event": event,
            "data": data,
        })
    except Exception as e:
        logger.debug("Could not push %s to user %s: %s", event, user_id, e)


async def _broadcast(entity_id: str, event: str, data: dict):
    if not entity_id:
        return
    try:
        await _redis_publish({"target": "entity", "entity_id": entity_id, "event": event, "data": data})
    except Exception as e:
        logger.debug("Could not broadcast %s to entity %s: %s", event, entity_id, e)


async def _broadcast_workspace(
    entity_id: str,
    workspace_id: str,
    event: str,
    data: dict,
):
    if not entity_id or not workspace_id:
        return
    try:
        await _redis_publish({
            "target": "workspace",
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "event": event,
            "data": data,
        })
    except Exception as e:
        logger.debug(
            "Could not broadcast %s to workspace %s: %s",
            event,
            workspace_id,
            e,
        )


async def _redis_publish(payload: dict):
    """Publish a WS event via Redis pub/sub so the API relay picks it up."""
    import json
    from packages.core.cache import _get_redis
    r = await _get_redis()
    if r:
        await r.publish("manor:ws_broadcast", json.dumps(payload))
