"""Runtime-owned facade for agent-callable notification actions."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_RUNTIME_VALID_NOTIFICATION_SEVERITIES = {"info", "warn", "critical"}


def _runtime_notification_ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, default=str)


def _runtime_notification_err(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


async def runtime_notify_user_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """Send a user notification through the Runtime action boundary."""

    raw_params = dict(params or {})
    target_user_id = str(raw_params.get("user_id") or "").strip()
    title = str(raw_params.get("title") or "").strip()
    if not target_user_id:
        return _runtime_notification_err("user_id is required")
    if not title:
        return _runtime_notification_err("title is required")

    body = raw_params.get("body")
    link = raw_params.get("link")
    severity = str(raw_params.get("severity") or "info").lower()
    if severity not in _RUNTIME_VALID_NOTIFICATION_SEVERITIES:
        severity = "info"
    kind = str(raw_params.get("kind") or "agent_message").strip() or "agent_message"

    from sqlalchemy import and_, or_, select

    from packages.core.database import async_session
    from packages.core.models.user import User, UserMembership

    async with async_session() as db:
        row = (await db.execute(
            select(User).outerjoin(
                UserMembership,
                and_(
                    UserMembership.user_id == User.id,
                    UserMembership.entity_id == entity_id,
                    UserMembership.status == "active",
                ),
            ).where(
                User.id == target_user_id,
                User.status == "active",
                or_(
                    User.entity_id == entity_id,
                    UserMembership.id.is_not(None),
                ),
            )
        )).scalar_one_or_none()
        if row is None:
            return _runtime_notification_err(
                "user_not_found_or_not_in_entity",
                user_id=target_user_id,
            )

    from packages.core.services.notify import notify

    try:
        await notify(
            entity_id=entity_id,
            user_id=target_user_id,
            type=kind,
            title=title,
            body=body if isinstance(body, str) else None,
            link=link if isinstance(link, str) else None,
            severity=severity,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
            meta={"sent_by_agent": agent_id} if agent_id else None,
        )
    except Exception as exc:
        logger.exception(
            "notify_user tool failed: target=%s kind=%s",
            target_user_id,
            kind,
        )
        return _runtime_notification_err(str(exc))

    return _runtime_notification_ok({
        "user_id": target_user_id,
        "kind": kind,
        "severity": severity,
    })


async def runtime_find_team_members_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Find team members through the Runtime action boundary."""

    from sqlalchemy import and_, or_, select

    from packages.core.database import async_session
    from packages.core.models.user import User, UserMembership

    raw_params = dict(params or {})
    query = (raw_params.get("query") or "").strip()
    workspace_id = raw_params.get("workspace_id")
    raw_limit = raw_params.get("limit")
    try:
        limit = max(1, min(int(raw_limit) if raw_limit is not None else 20, 50))
    except (TypeError, ValueError):
        limit = 20

    async with async_session() as db:
        stmt = select(User).outerjoin(
            UserMembership,
            and_(
                UserMembership.user_id == User.id,
                UserMembership.entity_id == entity_id,
                UserMembership.status == "active",
            ),
        ).where(
            User.status == "active",
            or_(
                User.entity_id == entity_id,
                UserMembership.id.is_not(None),
            ),
        )
        if query:
            like = f"%{query.lower()}%"
            stmt = stmt.where(
                or_(
                    User.display_name.ilike(like),
                    User.first_name.ilike(like),
                    User.last_name.ilike(like),
                    User.email.ilike(like),
                )
            )
        if workspace_id and isinstance(workspace_id, str):
            from packages.core.models.workspace import WorkspaceStaff

            stmt = stmt.join(
                WorkspaceStaff,
                WorkspaceStaff.user_id == User.id,
            ).where(WorkspaceStaff.workspace_id == workspace_id)
        stmt = stmt.order_by(User.display_name.asc()).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()

    items = [
        {
            "user_id": user.id,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
        }
        for user in rows
    ]
    return _runtime_notification_ok({"members": items, "count": len(items)})
