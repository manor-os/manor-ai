"""WebSocket endpoint for real-time push notifications."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import and_, or_, select

from packages.core import database as db_module
from packages.core.services.auth_service import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages active WebSocket connections per user."""

    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}  # user_id -> [ws]
        self._connection_entities: dict[WebSocket, str] = {}
        self._connection_token_versions: dict[WebSocket, int] = {}
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
    ) -> bool:
        await websocket.accept()
        first_connection = not any(
            self._connection_entities.get(existing) == entity_id
            for existing in self._connections.get(user_id, [])
        )
        self._connections.setdefault(user_id, []).append(websocket)
        self._connection_entities[websocket] = entity_id
        self._connection_token_versions[websocket] = token_version
        logger.info("WS connected: user=%s (total=%d)", user_id, self.count)
        return first_connection

    def disconnect(self, user_id: str, websocket: WebSocket) -> bool:
        conns = self._connections.get(user_id, [])
        entity_id = self._connection_entities.get(websocket)
        if websocket in conns:
            conns.remove(websocket)
        self._connection_entities.pop(websocket, None)
        self._connection_token_versions.pop(websocket, None)
        if not conns:
            self._connections.pop(user_id, None)
        logger.info("WS disconnected: user=%s (total=%d)", user_id, self.count)
        if not entity_id:
            return not conns
        return not any(
            self._connection_entities.get(existing) == entity_id
            for existing in conns
        )

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
    ):
        """Send an event to all connections for a user."""
        if entity_id:
            targets = self._entity_connection_snapshot(
                entity_id,
                user_ids={user_id},
                token_versions=token_versions,
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
        if not entity_id:
            return set()
        return {
            user_id
            for user_id, conns in tuple(self._connections.items())
            if any(
                self._connection_entities.get(ws) == entity_id
                for ws in tuple(conns)
            )
        }

    def _entity_connection_snapshot(
        self,
        entity_id: str,
        *,
        user_ids: set[str] | None = None,
        token_versions: dict[str, int] | None = None,
    ) -> list[tuple[str, WebSocket]]:
        if not entity_id:
            return []
        return [
            (user_id, ws)
            for user_id, conns in tuple(self._connections.items())
            if user_ids is None or user_id in user_ids
            for ws in tuple(conns)
            if self._connection_entities.get(ws) == entity_id
            and (
                token_versions is None
                or self._connection_token_versions.get(ws) == token_versions.get(user_id)
            )
        ]

    async def _send_to_connections(
        self,
        targets: list[tuple[str, WebSocket]],
        event: str,
        data: dict,
    ) -> None:
        message = json.dumps({"event": event, "data": data})
        for _user_id, ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                # The endpoint receive loop owns manager removal and the
                # corresponding Session/Presence cleanup.  Removing the
                # socket here would discard its entity metadata before that
                # cleanup can determine whether this was the last pair.
                try:
                    await ws.close(code=1011, reason="Delivery failed")
                except Exception:
                    pass

    async def broadcast_to_entity(
        self,
        entity_id: str,
        event: str,
        data: dict,
        *,
        user_ids: set[str] | None = None,
        token_versions: dict[str, int] | None = None,
    ):
        """Broadcast only to connections authenticated for the target entity."""
        targets = self._entity_connection_snapshot(
            entity_id,
            user_ids=user_ids,
            token_versions=token_versions,
        )
        await self._send_to_connections(targets, event, data)

    @property
    def count(self) -> int:
        return sum(len(c) for c in self._connections.values())


# Global singleton
manager = ConnectionManager()
_session_geo_tasks: set[asyncio.Task[None]] = set()
_session_geo_lookup_semaphore = asyncio.Semaphore(8)


async def _resolve_active_websocket_identity(
    payload: dict,
    user_id: str,
) -> tuple[str, int] | None:
    """Resolve the token against current account and membership state."""
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
            await db.commit()
            return entity_id, current_token_version
    except Exception as exc:
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
    """Return current token versions for active entity members."""
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
                )
            )).all()
            return {
                str(user_id): int(token_version or 0)
                for user_id, token_version in rows
            }
    except Exception:
        logger.warning(
            "Entity WS recipient resolution failed: entity=%s",
            entity_id,
            exc_info=True,
        )
        return {}


async def _workspace_broadcast_recipients(
    *,
    entity_id: str,
    workspace_id: str,
    candidate_user_ids: set[str],
) -> dict[str, int]:
    """Return current token versions for readable Workspace recipients."""
    if not entity_id or not workspace_id or not candidate_user_ids:
        return {}
    try:
        async with db_module.async_session() as db:
            from packages.core.models.staff import Staff, StaffRole
            from packages.core.models.user import User, UserMembership
            from packages.core.models.workspace import Workspace, WorkspaceStaff
            from packages.core.permissions import Permission, has_permission
            from packages.core.services.workspace_access import (
                ENTITY_ADMIN_ROLES,
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
                elif len(active_staff) != 1:
                    resolved_role = ""
                    can_read_entity_workspaces = False
                elif active_staff[0][0].role_id:
                    staff, staff_role = active_staff[0]
                    resolved_role = str(staff_role.name if staff_role else "").strip().lower()
                    can_read_entity_workspaces = bool(
                        staff_role
                        and Permission.WORKSPACES_READ.value in (staff_role.permissions or [])
                    )
                else:
                    staff, _staff_role = active_staff[0]
                    resolved_role = str((staff.meta or {}).get("role") or "").strip().lower()
                    can_read_entity_workspaces = has_permission(
                        resolved_role,
                        Permission.WORKSPACES_READ,
                    )

                if (
                    resolved_role in ENTITY_ADMIN_ROLES
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
    )
    await manager.send_to_user(
        user_id,
        event,
        payload.get("data", {}),
        entity_id=entity_id,
        token_versions=recipients,
    )


async def _redis_relay_loop():
    """Subscribe to Redis and relay events to WS clients with reconnect."""
    import redis.asyncio as aioredis
    from packages.core.config import get_settings

    retry_delay = 1.0
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
                    payload = json.loads(raw_msg["data"])
                    event = payload.get("event")
                    data = payload.get("data", {})
                    target = payload.get("target", "entity")
                    if target == "user":
                        await _relay_user_target(payload)
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
                        )
                    else:
                        entity_id = str(payload.get("entity_id") or "")
                        recipients = await _entity_broadcast_recipients(
                            entity_id=entity_id,
                            candidate_user_ids=manager.connected_user_ids_for_entity(entity_id),
                        )
                        await manager.broadcast_to_entity(
                            entity_id,
                            event,
                            data,
                            user_ids=set(recipients),
                            token_versions=recipients,
                        )
                except Exception:
                    logger.debug("Could not relay one Redis WS event", exc_info=True)
            raise ConnectionError("Redis WS pubsub stream ended")
        except asyncio.CancelledError:
            return
        except Exception:
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

    identity = await _resolve_active_websocket_identity(payload, user_id)
    if identity is None:
        await websocket.close(code=4001, reason="Inactive or revoked identity")
        return
    entity_id, token_version = identity
    first_connection = await manager.connect(
        user_id,
        entity_id,
        token_version,
        websocket,
    )
    if first_connection:
        await _ensure_tracked_session(
            user_id=user_id,
            entity_id=entity_id,
            websocket=websocket,
        )

    # Send initial connected event with unread count
    unread = 0
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
            unread = await count_unread(
                db,
                entity_id,
                user_id,
                restricted_workspace_entity_ids=restricted_entity_ids,
                readable_workspace_ids=readable_workspace_ids,
            )
    except Exception as e:
        logger.debug("WS unread count unavailable: %s", e)

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
                current_versions = await _entity_broadcast_recipients(
                    entity_id=entity_id,
                    candidate_user_ids={user_id},
                )
                if current_versions.get(user_id) != token_version:
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
