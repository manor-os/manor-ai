"""WebSocket endpoint for real-time push notifications."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from sqlalchemy import and_, or_, select, tuple_

from packages.core import database as db_module
from packages.core.database_runtime import DatabaseFailureKind
from packages.core.services.auth_service import decode_token
from packages.core.services.external_collaboration_access import (
    COLLABORATOR_MEMBERSHIP_ROLE,
    collaborator_workspace_ids,
    live_collaborator_grants,
)
from packages.core.services.collaborator_session import (
    collaborator_session_matches,
    session_claims,
    session_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

# External Workspace collaborators
# --------------------------------
# A collaborator membership (role ``collaborator``, derived from a
# WorkspaceCollaboration grant) is admitted only while
# ``collaborator_workspace_ids`` -- the live grants, never the derived seat --
# is non-empty, and receives only events that belong to one of those
# Workspaces. Per relay target:
#
#   entity          never: presence, typing, notifications and legacy unscoped
#                   events are company-wide (``_entity_broadcast_recipients``
#                   excludes collaborators). Only a task_update or job_update
#                   whose row names a Workspace (``data.workspace_id``) also
#                   reaches collaborators live-granted for that Workspace.
#   workspace       only when its workspace_id is live-granted
#                   (``_workspace_broadcast_recipients``).
#   workspace_user  same, except ``_COLLABORATOR_WITHHELD_EVENTS``.
#   user            never. Every user-target event emitted today names no
#                   Workspace and concerns a company or personal surface the
#                   HTTP boundary closes to collaborators: task_update,
#                   job_update and goal_progress for rows outside any
#                   Workspace; chat_stream_snapshot and conversation_message
#                   for personal (global/DM) conversations; notification
#                   without a Workspace. None is about the socket's own session.
#   chat_room       never: rooms are a company surface.
#
# The grant set is re-read on every delivery, and every client frame
# re-checks it with the token version (the client heartbeats every 30 s), so an
# ended collaboration stops receiving at once and its socket closes on the
# next frame. A collaborator socket also never originates company-wide state:
# its presence and typing frames only refresh its own session, and it has no
# notification center to mark read.

# The notification center is a company surface (the HTTP boundary denies
# /notifications); a collaborator follows the Workspace through its own
# Workspace-targeted events instead.
_COLLABORATOR_WITHHELD_EVENTS = frozenset({"notification"})
# Entity-target events about one Task or scheduled Job row; see the notes above.
_WORKSPACE_ROW_ENTITY_EVENTS = frozenset({"task_update", "job_update"})


class ConnectionManager:
    """Manages active WebSocket connections per user."""

    def __init__(self, *, send_concurrency: int = 32, send_timeout: float = 2.0):
        if send_concurrency < 1 or not math.isfinite(send_timeout) or send_timeout <= 0:
            raise ValueError("WebSocket delivery requires positive limits")
        self._send_concurrency = send_concurrency
        self._send_timeout = send_timeout
        self._send_slots = asyncio.Semaphore(send_concurrency)
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        self._connections: dict[str, list[WebSocket]] = {}  # user_id -> [ws]
        # Candidate connections only, never an authorization cache. Every
        # content relay still resolves current membership and token versions.
        self._entity_users: dict[str, set[str]] = {}
        self._connection_entities: dict[WebSocket, str] = {}
        self._connection_token_versions: dict[WebSocket, int] = {}
        # A collaborator's scoped session: the one Workspace it pins and when it expires.
        self._connection_pins: dict[WebSocket, tuple[str, float | None]] = {}
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_lease_ids: dict[tuple[str, str], str] = {}
        self._session_start_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._session_start_lock_users: dict[tuple[str, str], int] = {}

    @asynccontextmanager
    async def _session_lifecycle_lock(
        self,
        key: tuple[str, str],
    ) -> AsyncIterator[None]:
        """Keep one lock alive while any lifecycle operation references it."""
        lock = self._session_start_locks.setdefault(key, asyncio.Lock())
        self._session_start_lock_users[key] = (
            self._session_start_lock_users.get(key, 0) + 1
        )
        try:
            async with lock:
                yield
        finally:
            remaining = self._session_start_lock_users.get(key, 1) - 1
            if remaining > 0:
                self._session_start_lock_users[key] = remaining
            else:
                self._session_start_lock_users.pop(key, None)
                if self._session_start_locks.get(key) is lock:
                    self._session_start_locks.pop(key, None)

    async def connect(
        self,
        user_id: str,
        entity_id: str,
        token_version: int,
        websocket: WebSocket,
        *,
        pinned_workspace_id: str | None = None,
        expires_at: float | None = None,
    ) -> bool:
        await websocket.accept()
        if pinned_workspace_id:
            self._connection_pins[websocket] = (pinned_workspace_id, expires_at)
        first_connection = not any(
            self._connection_entities.get(existing) == entity_id
            for existing in self._connections.get(user_id, [])
        )
        self._connections.setdefault(user_id, []).append(websocket)
        self._connection_entities[websocket] = entity_id
        self._connection_token_versions[websocket] = token_version
        self._send_locks[websocket] = asyncio.Lock()
        self._entity_users.setdefault(entity_id, set()).add(user_id)
        logger.info("WS connected: user=%s (total=%d)", user_id, self.count)
        return first_connection

    def disconnect(self, user_id: str, websocket: WebSocket) -> bool:
        conns = self._connections.get(user_id, [])
        entity_id = self._connection_entities.get(websocket)
        if websocket in conns:
            conns.remove(websocket)
        self._connection_entities.pop(websocket, None)
        self._connection_token_versions.pop(websocket, None)
        self._connection_pins.pop(websocket, None)
        self._send_locks.pop(websocket, None)
        if not conns:
            self._connections.pop(user_id, None)
        logger.info("WS disconnected: user=%s (total=%d)", user_id, self.count)
        if not entity_id:
            return not conns
        last_entity_connection = not any(
            self._connection_entities.get(existing) == entity_id
            for existing in conns
        )
        if last_entity_connection:
            users = self._entity_users.get(entity_id)
            if users is not None:
                users.discard(user_id)
                if not users:
                    self._entity_users.pop(entity_id, None)
        return last_entity_connection

    def set_session_id(self, entity_id: str, user_id: str, session_id: str) -> None:
        self._session_ids[(entity_id, user_id)] = session_id

    def get_session_id(self, entity_id: str, user_id: str) -> Optional[str]:
        return self._session_ids.get((entity_id, user_id))

    def get_session_lease_id(self, entity_id: str, user_id: str) -> Optional[str]:
        return self._session_lease_ids.get((entity_id, user_id))

    def pop_tracked_session(
        self,
        entity_id: str,
        user_id: str,
    ) -> tuple[str, str | None] | None:
        key = (entity_id, user_id)
        session_id = self._session_ids.pop(key, None)
        lease_id = self._session_lease_ids.pop(key, None)
        return (session_id, lease_id) if session_id else None

    def pop_session_id(self, entity_id: str, user_id: str) -> Optional[str]:
        tracked = self.pop_tracked_session(entity_id, user_id)
        return tracked[0] if tracked else None

    def discard_tracked_session(
        self,
        entity_id: str,
        user_id: str,
        *,
        session_id: str,
        lease_id: str | None,
    ) -> bool:
        """Drop one stale local mapping without releasing a different lease."""
        key = (entity_id, user_id)
        if (
            self._session_ids.get(key) != session_id
            or self._session_lease_ids.get(key) != lease_id
        ):
            return False
        self._session_ids.pop(key, None)
        self._session_lease_ids.pop(key, None)
        return True

    async def ensure_session_started(
        self,
        entity_id: str,
        user_id: str,
        starter: Callable[[str], Awaitable[str | None]],
        stopper: Callable[[str, str], Awaitable[None]],
    ) -> str | None:
        """Start at most one session for a connected Entity/User pair."""
        key = (entity_id, user_id)
        session_id = self._session_ids.get(key)
        if session_id:
            return session_id
        async with self._session_lifecycle_lock(key):
            try:
                session_id = self._session_ids.get(key)
                if session_id:
                    return session_id
                if not any(
                    self._connection_entities.get(existing) == entity_id
                    for existing in self._connections.get(user_id, [])
                ):
                    return None
                lease_id = self._session_lease_ids.setdefault(
                    key,
                    uuid.uuid4().hex,
                )
                session_id = await starter(lease_id)
                if session_id:
                    still_connected = any(
                        self._connection_entities.get(existing) == entity_id
                        for existing in self._connections.get(user_id, [])
                    )
                    if not still_connected:
                        await stopper(session_id, lease_id)
                        self._session_lease_ids.pop(key, None)
                        return None
                    self._session_ids[key] = session_id
                return session_id
            finally:
                if not self._session_ids.get(key) and not any(
                    self._connection_entities.get(existing) == entity_id
                    for existing in self._connections.get(user_id, [])
                ):
                    self._session_lease_ids.pop(key, None)

    async def close_session(
        self,
        entity_id: str,
        user_id: str,
        closer: Callable[[str, str | None], Awaitable[None]],
    ) -> None:
        """Close the tracked session unless the scope reconnected meanwhile."""
        key = (entity_id, user_id)
        async with self._session_lifecycle_lock(key):
            try:
                if any(
                    self._connection_entities.get(existing) == entity_id
                    for existing in self._connections.get(user_id, [])
                ):
                    return
                tracked = self.pop_tracked_session(entity_id, user_id)
                if tracked:
                    await closer(*tracked)
            finally:
                if not self._session_ids.get(key) and not any(
                    self._connection_entities.get(existing) == entity_id
                    for existing in self._connections.get(user_id, [])
                ):
                    self._session_lease_ids.pop(key, None)

    async def send_to_user(
        self,
        user_id: str,
        event: str,
        data: dict,
        *,
        entity_id: str | None = None,
        token_versions: dict[str, int] | None = None,
        workspace_id: str | None = None,
    ):
        """Send an event to all connections for a user."""
        if entity_id:
            targets = self._entity_connection_snapshot(
                entity_id,
                user_ids={user_id},
                token_versions=token_versions,
                workspace_id=workspace_id,
            )
        else:
            targets = [(user_id, ws) for ws in tuple(self._connections.get(user_id, []))]
        await self._send_to_connections(targets, event, data)

    def connected_entity_ids_for_user(self, user_id: str) -> set[str]:
        return {
            entity_id
            for ws in tuple(self._connections.get(user_id, []))
            if (entity_id := self._connection_entities.get(ws))
        }

    def connected_user_ids_for_entity(self, entity_id: str) -> set[str]:
        return set(self._entity_users.get(entity_id, ()))

    def _entity_connection_snapshot(
        self,
        entity_id: str,
        *,
        user_ids: set[str] | None = None,
        token_versions: dict[str, int] | None = None,
        workspace_id: str | None = None,
    ) -> list[tuple[str, WebSocket]]:
        """Connections of the entity to deliver to.

        A scoped collaborator connection receives only events of the
        Workspace it pins (``workspace_id``), and nothing once it expired.
        """
        if not entity_id:
            return []
        # A room's authorized audience is already resolved. Do not scan every
        # platform connection again for each Entity represented in that room.
        if user_ids is None:
            user_ids = self.connected_user_ids_for_entity(entity_id)
        candidates = ((user_id, self._connections.get(user_id, [])) for user_id in user_ids)
        return [
            (user_id, ws)
            for user_id, conns in candidates
            for ws in tuple(conns)
            if self._connection_entities.get(ws) == entity_id
            and (
                token_versions is None
                or self._connection_token_versions.get(ws) == token_versions.get(user_id)
            )
            and self._pin_admits(ws, workspace_id)
        ]

    def _pin_admits(self, websocket: WebSocket, workspace_id: str | None) -> bool:
        pin = self._connection_pins.get(websocket)
        if pin is None:
            return True
        pinned, expires_at = pin
        return workspace_id == pinned and (expires_at is None or time.time() < expires_at)

    async def _send_to_connections(
        self,
        targets: list[tuple[str, WebSocket]],
        event: str,
        data: dict,
    ) -> None:
        message = json.dumps({"event": event, "data": data})
        remaining = iter(targets)

        async def deliver() -> None:
            for _user_id, ws in remaining:
                lock = self._send_locks.get(ws)
                if lock is None:
                    continue  # disconnected after the authorized snapshot
                try:
                    # Serialize frames for each socket; the Redis relay owns
                    # event order. A bounded worker set avoids creating one
                    # Task per connection on a large fanout.
                    async with lock, self._send_slots:
                        if self._send_locks.get(ws) is not lock:
                            continue
                        async with asyncio.timeout(self._send_timeout):
                            await ws.send_text(message)
                except Exception:
                    # Endpoint cleanup owns removal and Session/Presence
                    # bookkeeping. Bound close too: an unresponsive peer must
                    # not stall Redis relay indefinitely during cleanup.
                    try:
                        async with asyncio.timeout(self._send_timeout):
                            await ws.close(code=1011, reason="Delivery failed")
                    except Exception:
                        pass

        async with asyncio.TaskGroup() as group:
            for _ in range(min(len(targets), self._send_concurrency)):
                group.create_task(deliver())

    async def broadcast_to_entity(
        self,
        entity_id: str,
        event: str,
        data: dict,
        *,
        user_ids: set[str] | None = None,
        token_versions: dict[str, int] | None = None,
        workspace_id: str | None = None,
    ):
        """Broadcast only to connections authenticated for the target entity."""
        targets = self._entity_connection_snapshot(
            entity_id,
            user_ids=user_ids,
            token_versions=token_versions,
            workspace_id=workspace_id,
        )
        await self._send_to_connections(targets, event, data)

    async def broadcast_resync(self) -> None:
        """Tell every local socket that events may have been lost.

        Content-free on purpose: no tenant data travels, so no authorization
        lookup is needed. Clients answer by refetching their own views.
        """
        targets = [
            (user_id, ws)
            for user_id, conns in tuple(self._connections.items())
            for ws in tuple(conns)
        ]
        if targets:
            await self._send_to_connections(targets, "resync", {})

    @property
    def count(self) -> int:
        return len(self._connection_entities)


# Global singleton
manager = ConnectionManager()
_session_geo_tasks: set[asyncio.Task[None]] = set()
_session_geo_lookup_semaphore = asyncio.Semaphore(8)


async def _resolve_active_websocket_identity(
    payload: dict,
    user_id: str,
) -> tuple[str, int, bool] | None:
    """Resolve the token against current account and membership state.

    Returns ``(entity_id, token_version, collaborator)``; a collaborator is
    admitted only while a live grant places them in some Workspace.
    """
    try:
        async with db_module.async_session() as db:
            from packages.core.services.auth_service import (
                get_user_by_id,
                get_user_membership,
            )

            user = await get_user_by_id(db, user_id)
            if user is None or user.status != "active":
                return None
            try:
                token_version = int(payload.get("token_version", -1))
            except (TypeError, ValueError):
                return None
            current_token_version = int(user.token_version or 0)
            if token_version != current_token_version:
                return None
            entity_id = str(payload.get("entity_id") or user.entity_id or "")
            if not entity_id:
                return None
            membership = await get_user_membership(
                db,
                user=user,
                entity_id=entity_id,
            )
            if membership is None or membership.status != "active":
                return None
            collaborator = membership.role == COLLABORATOR_MEMBERSHIP_ROLE
            # Only a scoped session admits a collaborator, and only while a
            # live grant covers the Workspace it pins (collaborator_session).
            if not collaborator_session_matches(payload, collaborator=collaborator):
                return None
            if collaborator and not session_scope(
                payload, await live_collaborator_grants(db, user_id=user_id, entity_id=entity_id)
            ):
                return None
            await db.commit()
            return entity_id, current_token_version, collaborator
    except Exception as exc:
        if DatabaseFailureKind.from_exception(exc) is not None:
            # Temporary DB overload/outage is retryable, not revoked identity.
            # Let the typed handler close with 1013 instead of logging out a
            # legitimate user as if their credentials were invalid.
            raise
        logger.warning("WS identity resolution failed for user=%s: %s", user_id, exc)
        return None


async def _notification_workspace_scope(
    db,
    *,
    entity_id: str,
    user_id: str,
) -> tuple[list[str], list[str]]:
    """Resolve the current user's Workspace filter for notification actions."""
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.workspace_access import readable_workspace_ids_for_user

    role = await resolve_effective_user_role_name(
        db,
        user_id=user_id,
        entity_id=entity_id,
    )
    readable = await readable_workspace_ids_for_user(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    if readable is None:
        return [], []
    return [entity_id], sorted(readable)


async def _initial_unread_count(*, entity_id: str, user_id: str) -> int:
    try:
        async with db_module.async_session() as db:
            from packages.core.services.notification_service import count_unread
            restricted_entity_ids, readable_workspace_ids = (
                await _notification_workspace_scope(
                    db,
                    entity_id=entity_id,
                    user_id=user_id,
                )
            )
            return await count_unread(
                db,
                entity_id,
                user_id,
                restricted_workspace_entity_ids=restricted_entity_ids,
                readable_workspace_ids=readable_workspace_ids,
            )
    except Exception as e:
        logger.debug("WS unread count unavailable: %s", e)
        return 0


async def _start_tracked_session(
    *,
    user_id: str,
    entity_id: str,
    websocket: WebSocket,
    lease_id: str,
) -> str | None:
    if not entity_id:
        return None
    try:
        async with db_module.async_session() as db:
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
                lease_id=lease_id,
            )
            await db.commit()
            geo_task = asyncio.create_task(
                _enrich_tracked_session_geo(
                    session_id=session_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    ip_address=ip_address,
                ),
                name=f"user-session-geo:{session_id}",
            )
            _session_geo_tasks.add(geo_task)
            geo_task.add_done_callback(_session_geo_tasks.discard)
            return session_id
    except Exception as exc:
        logger.warning("WS user session start unavailable: %s", exc)
        return None


async def _enrich_tracked_session_geo(
    *,
    session_id: str,
    entity_id: str,
    user_id: str,
    ip_address: str | None,
) -> None:
    try:
        from packages.core.services.geo_ip import lookup_geo

        # Resolve the remote Geo-IP result before checking out an API database
        # connection. Cold lookups can take seconds and must remain best effort.
        async with _session_geo_lookup_semaphore:
            geo = await lookup_geo(ip_address)
        if not geo:
            return
        async with db_module.async_session() as db:
            from packages.core.services.user_session_service import (
                enrich_user_session_geo,
            )

            await enrich_user_session_geo(
                db,
                session_id=session_id,
                entity_id=entity_id,
                user_id=user_id,
                expected_ip_address=ip_address,
                geo=geo,
            )
            await db.commit()
    except Exception as exc:
        logger.debug("WS user session geo enrichment unavailable: %s", exc)


async def _ensure_tracked_session(
    *,
    user_id: str,
    entity_id: str,
    websocket: WebSocket,
) -> str | None:
    return await manager.ensure_session_started(
        entity_id,
        user_id,
        lambda lease_id: _start_tracked_session(
            user_id=user_id,
            entity_id=entity_id,
            websocket=websocket,
            lease_id=lease_id,
        ),
        lambda session_id, lease_id: _close_tracked_session(
            session_id=session_id,
            user_id=user_id,
            entity_id=entity_id,
            lease_id=lease_id,
        ),
    )


async def _close_tracked_session(
    *,
    session_id: str,
    user_id: str,
    entity_id: str,
    lease_id: str | None,
) -> None:
    try:
        async with db_module.async_session() as db:
            from packages.core.services.user_session_service import close_user_session_compat

            await close_user_session_compat(
                db,
                session_id=session_id,
                entity_id=entity_id,
                user_id=user_id,
                lease_id=lease_id,
            )
            await db.commit()
    except Exception as exc:
        logger.debug("WS user session close unavailable: %s", exc)


async def _touch_tracked_session(
    *,
    user_id: str,
    entity_id: str,
    websocket: WebSocket,
    viewing: str | None = None,
) -> None:
    """Refresh the local lease, restarting once if its durable claim expired."""
    for _attempt in range(2):
        session_id = await _ensure_tracked_session(
            user_id=user_id,
            entity_id=entity_id,
            websocket=websocket,
        )
        if not session_id:
            return
        lease_id = manager.get_session_lease_id(entity_id, user_id)
        try:
            async with db_module.async_session() as db:
                from packages.core.services.user_session_service import (
                    touch_user_session_compat,
                )

                touched = await touch_user_session_compat(
                    db,
                    session_id=session_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    viewing=viewing,
                    lease_id=lease_id,
                )
                await db.commit()
        except Exception as exc:
            logger.debug("WS user session touch unavailable: %s", exc)
            return
        if touched:
            return
        manager.discard_tracked_session(
            entity_id,
            user_id,
            session_id=session_id,
            lease_id=lease_id,
        )


# ── Redis pub/sub relay ──────────────────────────────────────────────
# Listens on the "manor:ws_broadcast" Redis channel so that messages
# published from the Celery worker (or any process) get relayed to
# connected WebSocket clients in this API process.

_redis_listener_task: Optional[asyncio.Task] = None


async def _entity_broadcast_recipients(
    *,
    entity_id: str,
    candidate_user_ids: set[str],
) -> dict[str, int]:
    """Return current token versions for active entity members.

    Collaborators are never entity recipients; see the module notes.
    """
    if not entity_id or not candidate_user_ids:
        return {}
    try:
        async with db_module.async_session() as db:
            from packages.core.models.user import User, UserMembership

            rows = (await db.execute(
                select(User.id, User.token_version)
                .join(UserMembership, UserMembership.user_id == User.id)
                .where(
                    User.id.in_(candidate_user_ids),
                    User.deleted_at.is_(None),
                    User.status == "active",
                    UserMembership.entity_id == entity_id,
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                    UserMembership.role != COLLABORATOR_MEMBERSHIP_ROLE,
                )
            )).all()
            return {
                str(user_id): int(token_version or 0)
                for user_id, token_version in rows
            }
    except Exception as exc:
        if DatabaseFailureKind.from_exception(exc) is not None:
            raise
        logger.warning(
            "Entity WS recipient resolution failed: entity=%s",
            entity_id,
            exc_info=True,
        )
        return {}


def _claim_epoch(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _session_expired(expires_at: float | None) -> bool:
    return expires_at is not None and time.time() >= expires_at


async def _collaborator_connection_version(
    *, entity_id: str, user_id: str, claims: dict | None = None,
) -> int | None:
    """Current token version of a collaborator socket, or None once its access ended.

    The collaborator counterpart of the per-frame ``_entity_broadcast_recipients``
    check: the membership must still be an active collaborator one and a live
    grant must still place the person in some Workspace.
    """
    async with db_module.async_session() as db:
        from packages.core.models.user import User, UserMembership

        row = (await db.execute(
            select(User.token_version)
            .join(UserMembership, UserMembership.user_id == User.id)
            .where(
                User.id == user_id,
                User.deleted_at.is_(None),
                User.status == "active",
                UserMembership.entity_id == entity_id,
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
                UserMembership.role == COLLABORATOR_MEMBERSHIP_ROLE,
            )
        )).first()
        if row is None:
            return None
        grants = await live_collaborator_grants(db, user_id=user_id, entity_id=entity_id)
        # A scoped session's socket: the grant it names must still be live on its Workspace.
        if not (session_scope(claims, grants) if claims is not None else grants):
            return None
        return int(row.token_version or 0)


async def _workspace_broadcast_recipients(
    *,
    entity_id: str,
    workspace_id: str,
    candidate_user_ids: set[str],
    include_collaborators: bool = True,
) -> dict[str, int]:
    """Return current token versions for readable Workspace recipients.

    A collaborator qualifies only through a live grant for this Workspace,
    never through the derived seat or a membership role.
    """
    if not entity_id or not workspace_id or not candidate_user_ids:
        return {}
    try:
        async with db_module.async_session() as db:
            from packages.core.models.staff import Staff, StaffRole
            from packages.core.models.user import User, UserMembership
            from packages.core.models.workspace import Workspace, WorkspaceStaff
            from packages.core.permissions import Permission, has_permission
            from packages.core.services.workspace_access import (
                ENTITY_WORKSPACE_READ_ROLES,
                WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE,
                workspace_access_mode,
            )

            workspace = (await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                ).limit(1)
            )).scalar_one_or_none()
            if workspace is None:
                return {}
            identities = (await db.execute(
                select(User, UserMembership)
                .join(
                    UserMembership,
                    UserMembership.user_id == User.id,
                )
                .where(
                    User.id.in_(candidate_user_ids),
                    User.deleted_at.is_(None),
                    User.status == "active",
                    UserMembership.entity_id == entity_id,
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                )
            )).all()

            identity_by_user = {
                user.id: (user, membership)
                for user, membership in identities
            }
            staff_rows = (await db.execute(
                select(Staff, StaffRole)
                .outerjoin(
                    StaffRole,
                    and_(
                        StaffRole.id == Staff.role_id,
                        StaffRole.entity_id == entity_id,
                        StaffRole.status == "active",
                    ),
                )
                .where(
                    Staff.user_id.in_(identity_by_user),
                    Staff.entity_id == entity_id,
                )
            )).all()
            staff_by_user: dict[str, list[tuple[Staff, StaffRole | None]]] = {}
            for staff, staff_role in staff_rows:
                staff_by_user.setdefault(staff.user_id, []).append((staff, staff_role))

            active_workspace_members = set((await db.execute(
                select(WorkspaceStaff.user_id).where(
                    WorkspaceStaff.workspace_id == workspace.id,
                    WorkspaceStaff.user_id.in_(identity_by_user),
                    WorkspaceStaff.status == "active",
                    or_(
                        WorkspaceStaff.expires_at.is_(None),
                        WorkspaceStaff.expires_at > datetime.now(UTC),
                    ),
                )
            )).scalars().all())

            allowed: dict[str, int] = {}
            entity_visible = (
                workspace_access_mode(workspace)
                == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
            )
            for user_id, (user, membership) in identity_by_user.items():
                if membership.role == COLLABORATOR_MEMBERSHIP_ROLE:
                    if include_collaborators and workspace.id in await collaborator_workspace_ids(
                        db, user_id=user_id, entity_id=entity_id
                    ):
                        allowed[user_id] = int(user.token_version or 0)
                    continue
                staff_history = staff_by_user.get(user_id, [])
                active_staff = [
                    assignment
                    for assignment in staff_history
                    if assignment[0].status == "active"
                    and assignment[0].deleted_at is None
                ]
                if not staff_history:
                    resolved_role = str(membership.role or "").strip().lower()
                    can_read_entity_workspaces = resolved_role in ENTITY_WORKSPACE_READ_ROLES
                    can_read_all_workspaces = has_permission(
                        resolved_role,
                        Permission.WORKSPACES_READ_ALL,
                    )
                elif len(active_staff) != 1:
                    resolved_role = ""
                    can_read_entity_workspaces = False
                    can_read_all_workspaces = False
                elif active_staff[0][0].role_id:
                    staff, staff_role = active_staff[0]
                    resolved_role = str(staff_role.name if staff_role else "").strip().lower()
                    can_read_entity_workspaces = bool(
                        staff_role
                        and Permission.WORKSPACES_READ.value in (staff_role.permissions or [])
                    )
                    can_read_all_workspaces = bool(
                        staff_role
                        and Permission.WORKSPACES_READ_ALL.value
                        in (staff_role.permissions or [])
                    )
                else:
                    staff, _staff_role = active_staff[0]
                    resolved_role = str((staff.meta or {}).get("role") or "").strip().lower()
                    can_read_entity_workspaces = has_permission(
                        resolved_role,
                        Permission.WORKSPACES_READ,
                    )
                    can_read_all_workspaces = has_permission(
                        resolved_role,
                        Permission.WORKSPACES_READ_ALL,
                    )

                if (
                    can_read_all_workspaces
                    or user_id in active_workspace_members
                    or (entity_visible and can_read_entity_workspaces)
                ):
                    allowed[user_id] = int(user.token_version or 0)
            return allowed
    except Exception:
        logger.warning(
            "Workspace WS recipient resolution failed: entity=%s workspace=%s",
            entity_id,
            workspace_id,
            exc_info=True,
        )
        return {}


async def _workspace_broadcast_user_ids(
    *,
    entity_id: str,
    workspace_id: str,
    candidate_user_ids: set[str],
) -> set[str]:
    """Return connected users currently allowed to read a Workspace."""
    return set(await _workspace_broadcast_recipients(
        entity_id=entity_id,
        workspace_id=workspace_id,
        candidate_user_ids=candidate_user_ids,
    ))


async def _relay_user_target(payload: dict) -> None:
    """Relay a personal event only inside its authenticated Entity scope."""
    user_id = str(payload.get("user_id") or "")
    entity_id = str(payload.get("entity_id") or "")
    event = payload.get("event")
    if not user_id or not entity_id or not event:
        logger.warning("Dropping unscoped WS user event")
        return
    recipients = await _entity_broadcast_recipients(
        entity_id=entity_id,
        candidate_user_ids={user_id},
    )
    await manager.send_to_user(
        user_id,
        event,
        payload.get("data", {}),
        entity_id=entity_id,
        token_versions=recipients,
    )


async def _relay_workspace_user_target(payload: dict) -> None:
    """Relay a personal event only while the user can read its Workspace."""
    user_id = str(payload.get("user_id") or "")
    entity_id = str(payload.get("entity_id") or "")
    workspace_id = str(payload.get("workspace_id") or "")
    event = payload.get("event")
    if not user_id or not entity_id or not workspace_id or not event:
        logger.warning("Dropping unscoped WS workspace-user event")
        return
    recipients = await _workspace_broadcast_recipients(
        entity_id=entity_id,
        workspace_id=workspace_id,
        candidate_user_ids={user_id},
        include_collaborators=event not in _COLLABORATOR_WITHHELD_EVENTS,
    )
    await manager.send_to_user(
        user_id,
        event,
        payload.get("data", {}),
        entity_id=entity_id,
        token_versions=recipients,
        workspace_id=workspace_id,
    )


async def _room_message_snapshot(db, room_id: str, message_id: str):
    """Read one committed message once, before member-scoped WS fanout."""
    from packages.core.models.chat_room import (
        ChatRoom, ChatRoomAgentRun, ChatRoomFile, ChatRoomMessage,
    )
    from packages.core.models.user import User

    row = (await db.execute(select(ChatRoomMessage, User).join(
        ChatRoom, ChatRoom.id == ChatRoomMessage.room_id,
    ).outerjoin(
        User, User.id == ChatRoomMessage.sender_user_id,
    ).where(
        ChatRoomMessage.id == message_id, ChatRoomMessage.room_id == room_id,
        ChatRoom.status == "active",
    ))).one_or_none()
    if row is None or row[0].deleted_at is not None:
        return None
    message, sender = row
    sender_name = message.sender_agent_name or (
        (sender.display_name or sender.email) if sender else "Deleted user"
    )
    previous = (await db.execute(select(
        ChatRoomMessage.id, ChatRoomMessage.created_at,
    ).where(
        ChatRoomMessage.room_id == room_id,
        tuple_(ChatRoomMessage.created_at, ChatRoomMessage.id) < (message.created_at, message.id),
    ).order_by(ChatRoomMessage.created_at.desc(), ChatRoomMessage.id.desc()).limit(1))).one_or_none()
    reply = None
    if message.reply_to_message_id:
        reply = (await db.execute(select(ChatRoomMessage, User).outerjoin(
            User, User.id == ChatRoomMessage.sender_user_id,
        ).where(
            ChatRoomMessage.id == message.reply_to_message_id,
            ChatRoomMessage.room_id == room_id,
        ))).one_or_none()
    files = (await db.execute(select(ChatRoomFile).where(
        ChatRoomFile.message_id == message.id, ChatRoomFile.deleted_at.is_(None),
    ))).scalars().all()
    runs = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.source_message_id == message.id,
    ).order_by(ChatRoomAgentRun.created_at, ChatRoomAgentRun.id))).scalars().all()
    data = {
        "id": message.id, "room_id": room_id,
        "sender_user_id": message.sender_user_id, "sender_name": sender_name,
        "sender_agent_binding_id": message.sender_agent_binding_id,
        "sender_agent_name": message.sender_agent_name,
        "sender_agent_avatar_url": message.sender_agent_avatar_url,
        "reply_to_message_id": message.reply_to_message_id,
        "agent_run_status": message.agent_run_status,
        "content": message.content, "deleted_at": None,
        "mentioned_user_ids": message.mentioned_user_ids or [],
        "reactions": [],
        "agent_runs": [{
            "id": run.id, "binding_id": run.binding_id,
            "agent_name": run.agent_name, "status": run.status,
            **({"approval_kind": (
                "reply" if isinstance(run.checkpoint, dict) and "pending_reply" in run.checkpoint else "file"
            )} if run.status == "waiting_approval" else {}),
            **({"error_code": run.error_code} if run.error_code == "llm_configuration_missing" else {}),
        } for run in runs],
        "created_at": message.created_at.isoformat(),
        "attachments": [{
            "id": file.id, "message_id": message.id,
            "name": file.name, "size": file.size, "mime_type": file.mime_type,
            "sender_name": sender_name, "created_at": file.created_at.isoformat(),
            "download_url": f"/api/v1/chat/rooms/{room_id}/files/{file.id}",
        } for file in files],
    }
    reply_view = None
    if reply:
        target, target_sender = reply
        reply_view = {
            "id": target.id,
            "sender_name": target.sender_agent_name or (
                (target_sender.display_name or target_sender.email) if target_sender else "Deleted user"
            ),
            "content": target.content[:160] if target.deleted_at is None else "Message removed",
        }
    return data, previous, (reply[0].created_at if reply else None), reply_view


async def _relay_chat_room_target(payload: dict) -> None:
    """Fan out a room invalidation only to currently active room members.

    Room membership crosses Entity boundaries. Resolve both the room ACL and
    each socket's current Entity membership/token version at delivery time.
    """
    room_id = str(payload.get("room_id") or "")
    if not room_id or payload.get("event") != "chat_room_changed":
        logger.warning("Dropping invalid WS chat room event")
        return
    if not manager._connections:
        return
    try:
        async with db_module.async_session() as db:
            from packages.core.models.chat_room import ChatRoom, ChatRoomMember
            from packages.core.models.user import User, UserMembership

            rows = (await db.execute(
                select(User.id, User.token_version, UserMembership.entity_id, ChatRoomMember.visible_from)
                .join(ChatRoomMember, ChatRoomMember.user_id == User.id)
                .join(ChatRoom, ChatRoom.id == ChatRoomMember.room_id)
                .join(UserMembership, UserMembership.user_id == User.id)
                .where(
                    ChatRoom.id == room_id,
                    ChatRoomMember.status == "active",
                    User.status == "active",
                    User.deleted_at.is_(None),
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                    # A collaborator socket in a customer company never
                    # carries rooms; their home-company socket still does.
                    UserMembership.role != COLLABORATOR_MEMBERSHIP_ROLE,
                )
            )).all()
            data = payload.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) and data.get("kind") == "message" else None
            snapshot = await _room_message_snapshot(db, room_id, message_id) if isinstance(message_id, str) else None
    except Exception:
        logger.warning("Chat room WS recipient resolution failed: room=%s", room_id, exc_info=True)
        return
    recipients_by_entity: dict[tuple[str, str | None, bool], dict[str, int]] = {}
    message = snapshot[0] if snapshot else None
    previous = snapshot[1] if snapshot else None
    reply_created_at = snapshot[2] if snapshot else None
    connected: dict[str, set[str]] = {}
    for user_id, token_version, entity_id, visible_from in rows:
        if user_id not in manager._connections:
            continue
        if user_id not in connected:
            connected[user_id] = manager.connected_entity_ids_for_user(user_id)
        if entity_id in connected[user_id]:
            if message is not None:
                if datetime.fromisoformat(message["created_at"]) < visible_from:
                    continue
                previous_id = previous[0] if previous and previous[1] >= visible_from else None
                show_reply = bool(reply_created_at and reply_created_at >= visible_from)
            else:
                previous_id, show_reply = None, False
            recipients_by_entity.setdefault((entity_id, previous_id, show_reply), {})[user_id] = int(token_version or 0)
    requested_kind = data.get("kind") if isinstance(data, dict) else None
    kind = requested_kind if requested_kind in ("retracted", "reaction") else "changed"
    for (entity_id, previous_id, show_reply), recipients in recipients_by_entity.items():
        if message is not None:
            event_data = {
                "room_id": room_id, "kind": "message", "message": {
                    **message, "reply_to": snapshot[3] if show_reply else None,
                }, "previous_message_id": previous_id,
            }
        else:
            event_data = {"room_id": room_id, "kind": kind}
        await manager.broadcast_to_entity(
            entity_id,
            "chat_room_changed",
            event_data,
            user_ids=set(recipients),
            token_versions=recipients,
        )


async def _relay_event(payload: dict) -> None:
    """Deliver one published event to the connections its target authorizes now."""
    event = payload.get("event")
    data = payload.get("data", {})
    target = payload.get("target", "entity")
    if target == "user":
        await _relay_user_target(payload)
    elif target == "chat_room":
        await _relay_chat_room_target(payload)
    elif target == "workspace_user":
        await _relay_workspace_user_target(payload)
    elif target == "workspace":
        entity_id = str(payload.get("entity_id") or "")
        workspace_id = str(payload.get("workspace_id") or "")
        recipients = await _workspace_broadcast_recipients(
            entity_id=entity_id,
            workspace_id=workspace_id,
            candidate_user_ids=manager.connected_user_ids_for_entity(entity_id),
        )
        await manager.broadcast_to_entity(
            entity_id,
            event,
            data,
            user_ids=set(recipients),
            token_versions=recipients,
            workspace_id=workspace_id,
        )
    else:
        entity_id = str(payload.get("entity_id") or "")
        candidates = manager.connected_user_ids_for_entity(entity_id)
        recipients = await _entity_broadcast_recipients(
            entity_id=entity_id,
            candidate_user_ids=candidates,
        )
        workspace_id = (
            str(data.get("workspace_id") or "")
            if event in _WORKSPACE_ROW_ENTITY_EVENTS and isinstance(data, dict)
            else ""
        )
        if workspace_id and candidates - set(recipients):
            # A Task or Job row of a Workspace also reaches the collaborators
            # whose live grant covers that Workspace.
            recipients = {
                **await _workspace_broadcast_recipients(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    candidate_user_ids=candidates - set(recipients),
                ),
                **recipients,
            }
        await manager.broadcast_to_entity(
            entity_id,
            event,
            data,
            user_ids=set(recipients),
            token_versions=recipients,
            workspace_id=workspace_id or None,
        )


async def _redis_relay_loop():
    """Subscribe to Redis and relay events to WS clients with reconnect."""
    import redis.asyncio as aioredis
    from packages.core.config import get_settings

    retry_delay = 1.0
    # Redis pub/sub has no replay: events published while this relay was down
    # never reach sockets that stayed connected through the outage.
    missed_events = False
    while True:
        r = None
        pubsub = None
        saw_message = False
        try:
            r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
            await r.ping()
            pubsub = r.pubsub()
            await pubsub.subscribe("manor:ws_broadcast")
            logger.info("WS Redis relay subscribed to manor:ws_broadcast")
            if missed_events:
                missed_events = False
                try:
                    await manager.broadcast_resync()
                except Exception:
                    logger.debug("Could not send WS resync after relay recovery", exc_info=True)
            async for raw_msg in pubsub.listen():
                if raw_msg["type"] != "message":
                    continue
                # A subscription acknowledgement only proves that Redis
                # accepted the subscribe command. Reset the retry budget only
                # after real traffic has traversed the stream; this preserves
                # exponential backoff for connect-then-immediate-disconnect
                # loops without penalizing a healthy relay after a real event.
                if not saw_message:
                    retry_delay = 1.0
                    saw_message = True
                try:
                    await _relay_event(json.loads(raw_msg["data"]))
                except Exception:
                    logger.debug("Could not relay one Redis WS event", exc_info=True)
            raise ConnectionError("Redis WS pubsub stream ended")
        except asyncio.CancelledError:
            return
        except Exception:
            missed_events = True
            logger.exception("Redis relay loop disconnected; retrying in %.1fs", retry_delay)
        finally:
            for resource in (pubsub, r):
                if resource is None:
                    continue
                close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
                if close is not None:
                    try:
                        result = close()
                        if result is not None:
                            await result
                    except Exception:
                        logger.debug("Could not close Redis relay resource", exc_info=True)
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30.0)


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


def _handshake_bearer_token(authorization: str | None) -> str | None:
    """Return the credential of an ``Authorization: Bearer <jwt>`` handshake header."""
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip() or None


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    """
    WebSocket connection for real-time events.

    Connect with: ws://host/ws?token=<jwt_token>, or send the same JWT as
    ``Authorization: Bearer <jwt_token>`` on the handshake (native clients). A
    query token takes precedence; both paths share one validation.

    Events pushed:
      - notification: new notification created
      - task_update: task status changed
      - goal_progress: goal step completed
      - resync: events may have been lost (relay recovered); refetch views
      - ping: keepalive (every 30s)

    Client can send:
      - {"type": "ping"} -- keepalive response
      - {"type": "mark_read", "notification_id": "..."} -- mark notification read
    """
    # Authenticate the JWT from the query string, or from the handshake header
    # when no query token is given (mobile keeps tokens out of URLs and logs).
    token = token or _handshake_bearer_token(websocket.headers.get("authorization"))
    if not token:
        await websocket.close(code=4001, reason="Invalid token")
        return
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

    identity = await _resolve_active_websocket_identity(payload, user_id)
    if identity is None:
        await websocket.close(code=4001, reason="Inactive or revoked identity")
        return
    entity_id, token_version, collaborator = identity
    # A collaborator's scoped session: events of its pinned Workspace only,
    # and the socket closes when the session expires (the frame re-mints and
    # reconnects).
    pinned_workspace_id = session_claims(payload)[0] if collaborator else None
    session_expires_at = _claim_epoch(payload.get("exp")) if collaborator else None
    first_connection = await manager.connect(
        user_id,
        entity_id,
        token_version,
        websocket,
        pinned_workspace_id=pinned_workspace_id,
        expires_at=session_expires_at,
    )
    if first_connection:
        await _ensure_tracked_session(
            user_id=user_id,
            entity_id=entity_id,
            websocket=websocket,
        )

    # Send initial connected event with unread count
    # A collaborator has no notification center (see the module notes).
    unread = 0 if collaborator else await _initial_unread_count(
        entity_id=entity_id,
        user_id=user_id,
    )

    try:
        await websocket.send_text(json.dumps({
            "event": "connected",
            "data": {"user_id": user_id, "unread_notifications": unread},
        }))
    except (WebSocketDisconnect, Exception):
        last_connection = manager.disconnect(user_id, websocket)
        if last_connection:
            await manager.close_session(
                entity_id,
                user_id,
                lambda session_id, lease_id: _close_tracked_session(
                    session_id=session_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    lease_id=lease_id,
                ),
            )
        return

    # Keepalive + message loop
    try:
        while True:
            try:
                # Wait for client message with timeout (keepalive)
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if _session_expired(session_expires_at):
                    await websocket.close(code=4001, reason="Session expired")
                    break
                if collaborator:
                    current_version = await _collaborator_connection_version(
                        entity_id=entity_id,
                        user_id=user_id,
                        claims=payload,
                    )
                else:
                    current_version = (await _entity_broadcast_recipients(
                        entity_id=entity_id,
                        candidate_user_ids={user_id},
                    )).get(user_id)
                if current_version != token_version:
                    await websocket.close(code=4003, reason="Identity no longer active")
                    break
                msg = json.loads(data)

                if msg.get("type") == "ping":
                    await _touch_tracked_session(
                        user_id=user_id,
                        entity_id=entity_id,
                        websocket=websocket,
                    )
                    await websocket.send_text(json.dumps({"event": "pong", "data": {}}))
                elif collaborator:
                    # Presence, typing and mark_read are company-wide state.
                    if msg.get("type") == "presence":
                        await _touch_tracked_session(
                            user_id=user_id,
                            entity_id=entity_id,
                            websocket=websocket,
                            viewing=msg.get("viewing"),
                        )
                elif msg.get("type") == "mark_read":
                    nid = msg.get("notification_id")
                    if nid:
                        async with db_module.async_session() as db:
                            from packages.core.services.notification_service import mark_read

                            restricted_entity_ids, readable_workspace_ids = (
                                await _notification_workspace_scope(
                                    db,
                                    entity_id=entity_id,
                                    user_id=user_id,
                                )
                            )
                            await mark_read(
                                db,
                                nid,
                                user_id,
                                entity_ids=[entity_id],
                                restricted_workspace_entity_ids=restricted_entity_ids,
                                readable_workspace_ids=readable_workspace_ids,
                            )
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
                    await _touch_tracked_session(
                        user_id=user_id,
                        entity_id=entity_id,
                        websocket=websocket,
                        viewing=msg.get("viewing"),
                    )
                elif msg.get("type") == "typing":
                    from packages.core.services.presence_service import update_presence
                    update_presence(entity_id, user_id, typing_in=msg.get("conversation_id"))
                    # Broadcast typing indicator to others in the entity
                    recipients = await _entity_broadcast_recipients(
                        entity_id=entity_id,
                        candidate_user_ids=manager.connected_user_ids_for_entity(entity_id),
                    )
                    await manager.broadcast_to_entity(
                        entity_id,
                        "typing",
                        {
                            "user_id": user_id,
                            "conversation_id": msg.get("conversation_id"),
                        },
                        user_ids=set(recipients),
                        token_versions=recipients,
                    )

            except asyncio.TimeoutError:
                if _session_expired(session_expires_at):
                    await websocket.close(code=4001, reason="Session expired")
                    break
                # Send keepalive ping
                try:
                    await websocket.send_text(json.dumps({"event": "ping", "data": {}}))
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        failure = DatabaseFailureKind.from_exception(e)
        if failure is not None:
            logger.warning("WS temporarily unavailable: reason=%s exception_type=%s", failure.value, type(e).__name__)
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="Database temporarily unavailable. Retry later.")
        else:
            logger.error("WS error: %s", e)
    finally:
        last_connection = manager.disconnect(user_id, websocket)
        if last_connection:
            from packages.core.services.presence_service import remove_presence
            remove_presence(entity_id, user_id)
            await manager.close_session(
                entity_id,
                user_id,
                lambda session_id, lease_id: _close_tracked_session(
                    session_id=session_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    lease_id=lease_id,
                ),
            )
