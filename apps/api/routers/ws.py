"""WebSocket endpoint for real-time push notifications."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.services.auth_service import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages active WebSocket connections per user."""

    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}  # user_id -> [ws]
        self._session_ids: dict[str, str] = {}  # user_id -> active user_session_logs.id

    async def connect(self, user_id: str, websocket: WebSocket) -> bool:
        first_connection = not self._connections.get(user_id)
        await websocket.accept()
        self._connections.setdefault(user_id, []).append(websocket)
        logger.info("WS connected: user=%s (total=%d)", user_id, self.count)
        return first_connection

    def disconnect(self, user_id: str, websocket: WebSocket) -> bool:
        conns = self._connections.get(user_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self._connections.pop(user_id, None)
        logger.info("WS disconnected: user=%s (total=%d)", user_id, self.count)
        return not conns

    def set_session_id(self, user_id: str, session_id: str) -> None:
        self._session_ids[user_id] = session_id

    def get_session_id(self, user_id: str) -> Optional[str]:
        return self._session_ids.get(user_id)

    def pop_session_id(self, user_id: str) -> Optional[str]:
        return self._session_ids.pop(user_id, None)

    async def send_to_user(self, user_id: str, event: str, data: dict):
        """Send an event to all connections for a user."""
        message = json.dumps({"event": event, "data": data})
        conns = self._connections.get(user_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.remove(ws)

    async def broadcast_to_entity(self, entity_id: str, event: str, data: dict):
        """Broadcast to all users (requires entity->user mapping -- simplified for now)."""
        message = json.dumps({"event": event, "data": data})
        for conns in self._connections.values():
            for ws in conns:
                try:
                    await ws.send_text(message)
                except Exception:
                    pass

    @property
    def count(self) -> int:
        return sum(len(c) for c in self._connections.values())


# Global singleton
manager = ConnectionManager()


async def _resolve_entity_id(payload: dict, user_id: str) -> str:
    entity_id = str(payload.get("entity_id") or "")
    if entity_id:
        return entity_id
    try:
        async with async_session() as db:
            from packages.core.models.user import User

            entity_id = (await db.execute(
                select(User.entity_id).where(User.id == user_id)
            )).scalar_one_or_none() or ""
            if entity_id:
                logger.warning(
                    "WS token missing entity_id; resolved entity from users row for user=%s",
                    user_id,
                )
            return str(entity_id or "")
    except Exception as exc:
        logger.warning("WS entity resolution failed for user=%s: %s", user_id, exc)
        return ""


async def _start_tracked_session(
    *,
    user_id: str,
    entity_id: str,
    websocket: WebSocket,
) -> str | None:
    if not entity_id:
        return None
    try:
        async with async_session() as db:
            from packages.core.services.user_session_service import start_user_session_compat

            forwarded_for = websocket.headers.get("x-forwarded-for")
            ip_address = (
                forwarded_for.split(",", 1)[0].strip()
                if forwarded_for else (
                    websocket.client.host if websocket.client else None
                )
            )
            session_id = await start_user_session_compat(
                db,
                entity_id=entity_id,
                user_id=user_id,
                source="websocket",
                ip_address=ip_address,
                user_agent=websocket.headers.get("user-agent"),
            )
            await db.commit()
            manager.set_session_id(user_id, session_id)
            return session_id
    except Exception as exc:
        logger.warning("WS user session start unavailable: %s", exc)
        return None


# ── Redis pub/sub relay ──────────────────────────────────────────────
# Listens on the "manor:ws_broadcast" Redis channel so that messages
# published from the Celery worker (or any process) get relayed to
# connected WebSocket clients in this API process.

_redis_listener_task: Optional[asyncio.Task] = None


async def _redis_relay_loop():
    """Subscribe to Redis and relay events to WS clients."""
    try:
        import redis.asyncio as aioredis
        from packages.core.config import get_settings
        url = get_settings().REDIS_URL
        r = aioredis.from_url(url, decode_responses=True)
        await r.ping()
        pubsub = r.pubsub()
        await pubsub.subscribe("manor:ws_broadcast")
        logger.info("WS Redis relay subscribed to manor:ws_broadcast")
        async for raw_msg in pubsub.listen():
            if raw_msg["type"] != "message":
                continue
            try:
                payload = json.loads(raw_msg["data"])
                event = payload.get("event")
                data = payload.get("data", {})
                target = payload.get("target", "entity")
                if target == "user":
                    await manager.send_to_user(payload.get("user_id", ""), event, data)
                else:
                    await manager.broadcast_to_entity(payload.get("entity_id", ""), event, data)
            except Exception:
                pass
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Redis relay loop crashed, will not auto-restart")


def start_redis_relay():
    """Call once on API startup to begin listening."""
    global _redis_listener_task
    if _redis_listener_task is None or _redis_listener_task.done():
        _redis_listener_task = asyncio.create_task(_redis_relay_loop())


def stop_redis_relay():
    """Call on shutdown to clean up."""
    global _redis_listener_task
    if _redis_listener_task and not _redis_listener_task.done():
        _redis_listener_task.cancel()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    """
    WebSocket connection for real-time events.

    Connect with: ws://host/ws?token=<jwt_token>

    Events pushed:
      - notification: new notification created
      - task_update: task status changed
      - goal_progress: goal step completed
      - ping: keepalive (every 30s)

    Client can send:
      - {"type": "ping"} -- keepalive response
      - {"type": "mark_read", "notification_id": "..."} -- mark notification read
    """
    # Authenticate via JWT token in query param
    try:
        payload = decode_token(token)
        if not payload:
            await websocket.close(code=4001, reason="Invalid token")
            return
        user_id = payload.get("sub") or payload.get("user_id")
        if not user_id:
            await websocket.close(code=4001, reason="Invalid token")
            return
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    entity_id = await _resolve_entity_id(payload, user_id)
    first_connection = await manager.connect(user_id, websocket)
    if first_connection and entity_id:
        await _start_tracked_session(
            user_id=user_id,
            entity_id=entity_id,
            websocket=websocket,
        )

    # Send initial connected event with unread count
    unread = 0
    try:
        async with async_session() as db:
            from packages.core.services.notification_service import count_unread
            unread = await count_unread(db, entity_id, user_id)
    except Exception as e:
        logger.debug("WS unread count unavailable: %s", e)

    try:
        await websocket.send_text(json.dumps({
            "event": "connected",
            "data": {"user_id": user_id, "unread_notifications": unread},
        }))
    except (WebSocketDisconnect, Exception):
        last_connection = manager.disconnect(user_id, websocket)
        if last_connection and entity_id:
            session_id = manager.pop_session_id(user_id)
            if session_id:
                try:
                    async with async_session() as db:
                        from packages.core.services.user_session_service import close_user_session_compat

                        await close_user_session_compat(
                            db,
                            session_id=session_id,
                            entity_id=entity_id,
                            user_id=user_id,
                        )
                        await db.commit()
                except Exception as e:
                    logger.debug("WS user session close unavailable: %s", e)
        return

    # Keepalive + message loop
    try:
        while True:
            try:
                # Wait for client message with timeout (keepalive)
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                msg = json.loads(data)

                if msg.get("type") == "ping":
                    session_id = manager.get_session_id(user_id)
                    if not session_id and entity_id:
                        session_id = await _start_tracked_session(
                            user_id=user_id,
                            entity_id=entity_id,
                            websocket=websocket,
                        )
                    if session_id and entity_id:
                        try:
                            async with async_session() as db:
                                from packages.core.services.user_session_service import touch_user_session_compat

                                await touch_user_session_compat(
                                    db,
                                    session_id=session_id,
                                    entity_id=entity_id,
                                    user_id=user_id,
                                )
                                await db.commit()
                        except Exception as e:
                            logger.debug("WS user session touch unavailable: %s", e)
                    await websocket.send_text(json.dumps({"event": "pong", "data": {}}))
                elif msg.get("type") == "mark_read":
                    nid = msg.get("notification_id")
                    if nid:
                        async with async_session() as db:
                            from packages.core.services.notification_service import mark_read

                            await mark_read(db, nid, user_id)
                            await db.commit()
                elif msg.get("type") == "presence":
                    from packages.core.services.presence_service import update_presence
                    update_presence(
                        entity_id, user_id,
                        display_name=msg.get("display_name"),
                        status=msg.get("status", "online"),
                        viewing=msg.get("viewing"),
                        typing_in=msg.get("typing_in"),
                    )
                    session_id = manager.get_session_id(user_id)
                    if not session_id and entity_id:
                        session_id = await _start_tracked_session(
                            user_id=user_id,
                            entity_id=entity_id,
                            websocket=websocket,
                        )
                    if session_id and entity_id:
                        try:
                            async with async_session() as db:
                                from packages.core.services.user_session_service import touch_user_session_compat

                                await touch_user_session_compat(
                                    db,
                                    session_id=session_id,
                                    entity_id=entity_id,
                                    user_id=user_id,
                                    viewing=msg.get("viewing"),
                                )
                                await db.commit()
                        except Exception as e:
                            logger.debug("WS user session touch unavailable: %s", e)
                elif msg.get("type") == "typing":
                    from packages.core.services.presence_service import update_presence
                    update_presence(entity_id, user_id, typing_in=msg.get("conversation_id"))
                    # Broadcast typing indicator to others in the entity
                    await manager.broadcast_to_entity(entity_id, "typing", {
                        "user_id": user_id,
                        "conversation_id": msg.get("conversation_id"),
                    })

            except asyncio.TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_text(json.dumps({"event": "ping", "data": {}}))
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS error: %s", e)
    finally:
        from packages.core.services.presence_service import remove_presence
        remove_presence(entity_id, user_id)
        last_connection = manager.disconnect(user_id, websocket)
        if last_connection and entity_id:
            session_id = manager.pop_session_id(user_id)
            if session_id:
                try:
                    async with async_session() as db:
                        from packages.core.services.user_session_service import close_user_session_compat

                        await close_user_session_compat(
                            db,
                            session_id=session_id,
                            entity_id=entity_id,
                            user_id=user_id,
                        )
                        await db.commit()
                except Exception as e:
                    logger.debug("WS user session close unavailable: %s", e)
