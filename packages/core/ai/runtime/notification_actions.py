"""Runtime-owned facade for agent-callable notification actions."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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


# The event ``type`` drives both the bell icon and notify()'s per-event
# channel routing. ``system`` is the one generic kind the event catalog
# defines (``EVENTS_BY_KIND["system"]`` → "Generic platform announcement");
# the web bell has an explicit icon/style for it (``TYPE_STYLES["system"]``),
# so a broadcast lands recognizably instead of on the unknown-type fallback.
_NOTIFY_MEMBERS_TYPE = "system"


async def runtime_notify_members_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """Fan out a notification to every targeted entity member.

    ``member_ids`` (optional) selects specific recipients; when omitted the
    notification goes to *all* active members of the acting entity. Recipients
    are always resolved through the same ``User`` + ``UserMembership``
    membership source that ``find_team_members`` / ``notify_user`` use, so
    unknown or cross-entity ids are silently dropped — an agent can't ping a
    user outside its own tenant.

    Each recipient is reached through the multi-channel ``notify()``
    primitive (same pipeline as ``notify_user``), so the broadcast lands on
    every member's in-app bell **and** any external channel (email, Telegram,
    …) their preferences opt into. Each ``notify()`` is isolated in its own
    try/except so one member's failure can't abort the fan-out — failures are
    reported per-member like the email path. Returns ``{delivered,
    member_ids, notification_type, failed}``.
    """

    raw_params = dict(params or {})
    title = str(raw_params.get("title") or "").strip()
    if not title:
        return _runtime_notification_err("title is required")

    body = raw_params.get("body")
    link = raw_params.get("link")
    body = body if isinstance(body, str) else None
    link = link if isinstance(link, str) else None

    # Normalize optional member_ids to a clean list. ``None`` means "all
    # active members"; an explicitly-empty list is a caller error.
    requested = raw_params.get("member_ids")
    member_id_filter: list[str] | None = None
    if requested is not None:
        if isinstance(requested, str):
            requested = [requested]
        if not isinstance(requested, (list, tuple, set)):
            return _runtime_notification_err("member_ids must be a list of user ids")
        member_id_filter = [str(m).strip() for m in requested if str(m).strip()]
        if not member_id_filter:
            return _runtime_notification_err("member_ids was provided but empty")

    from sqlalchemy import and_, or_, select

    from packages.core.database import async_session
    from packages.core.models.user import User, UserMembership
    from packages.core.services.notify import notify

    # Resolve the recipient set first (own session), then fan out through
    # notify() — which manages its own session/commit per call.
    async with async_session() as db:
        stmt = select(User.id).outerjoin(
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
        if member_id_filter is not None:
            stmt = stmt.where(User.id.in_(member_id_filter))
        recipient_ids = list((await db.execute(stmt)).scalars().all())

    meta = {"sent_by_agent": agent_id} if agent_id else None
    ws = workspace_id if isinstance(workspace_id, str) else None

    delivered_ids: list[str] = []
    failed: list[dict[str, str]] = []
    for member_id in recipient_ids:
        try:
            await notify(
                entity_id=entity_id,
                user_id=member_id,
                type=_NOTIFY_MEMBERS_TYPE,
                title=title,
                body=body,
                link=link,
                severity="info",
                workspace_id=ws,
                meta=dict(meta) if meta else None,
            )
            delivered_ids.append(member_id)
        except Exception as exc:
            logger.exception(
                "notify_members fan-out failed for member=%s", member_id
            )
            failed.append({"member_id": member_id, "error": str(exc)})

    return _runtime_notification_ok({
        "delivered": len(delivered_ids),
        "member_ids": delivered_ids,
        "notification_type": _NOTIFY_MEMBERS_TYPE,
        "failed": failed,
    })


async def runtime_find_team_members_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Find team members through the Runtime action boundary."""

    from sqlalchemy import and_, func, or_, select

    from packages.core.database import async_session
    from packages.core.models.user import User, UserMembership

    raw_params = dict(params or {})
    try:
        query = str(raw_params.get("query") or "").strip()
        limit = max(1, min(int(raw_params.get("limit") or 20), 50))
        offset = max(0, int(raw_params.get("offset") or 0))
        workspace_ids = _runtime_member_filter_values(
            raw_params.get("workspace_ids"),
            single=raw_params.get("workspace_id"),
            field_name="workspace_ids",
        )
        roles = _runtime_member_filter_values(
            raw_params.get("roles"),
            field_name="roles",
            lowercase=True,
        )
        datetime_ranges = {
            field_name: _runtime_member_datetime_range(
                raw_params.get(f"{field_name}_after"),
                raw_params.get(f"{field_name}_before"),
                field_name,
            )
            for field_name in ("created", "updated", "last_login")
        }
    except (TypeError, ValueError) as exc:
        return _runtime_notification_err(
            str(exc),
            code="invalid_team_member_filters",
        )

    async with async_session() as db:
        effective_role = func.lower(func.coalesce(UserMembership.role, User.role))
        conditions = [
            User.status == "active",
            User.deleted_at.is_(None),
            or_(
                User.entity_id == entity_id,
                UserMembership.id.is_not(None),
            ),
        ]
        if query:
            like = f"%{query}%"
            conditions.append(or_(
                User.display_name.ilike(like),
                User.first_name.ilike(like),
                User.last_name.ilike(like),
                User.email.ilike(like),
            ))
        if workspace_ids:
            from packages.core.models.workspace import WorkspaceStaff

            conditions.append(
                select(WorkspaceStaff.id).where(
                    WorkspaceStaff.user_id == User.id,
                    WorkspaceStaff.workspace_id.in_(workspace_ids),
                    WorkspaceStaff.status == "active",
                ).exists()
            )
        if roles:
            conditions.append(effective_role.in_(roles))
        for column, field_name in (
            (User.created_at, "created"),
            (User.updated_at, "updated"),
            (User.last_login_at, "last_login"),
        ):
            after, before = datetime_ranges[field_name]
            if after is not None:
                conditions.append(column >= after)
            if before is not None:
                conditions.append(column <= before)
        membership_join = and_(
            UserMembership.user_id == User.id,
            UserMembership.entity_id == entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
        stmt = select(User, effective_role.label("effective_role")).outerjoin(
            UserMembership,
            membership_join,
        ).where(*conditions).order_by(
            User.display_name.asc().nulls_last(),
            User.email.asc(),
            User.id.asc(),
        ).limit(limit).offset(offset)
        count_stmt = select(func.count()).select_from(User).outerjoin(
            UserMembership,
            membership_join,
        ).where(*conditions)
        rows = (await db.execute(stmt)).all()
        total = int((await db.execute(count_stmt)).scalar_one())

    items = [
        {
            "user_id": user.id,
            "display_name": user.display_name,
            "email": user.email,
            "role": role,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            "last_login_at": (
                user.last_login_at.isoformat() if user.last_login_at else None
            ),
        }
        for user, role in rows
    ]
    return _runtime_notification_ok({
        "members": items,
        "total": total,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "next_offset": offset + len(items) if offset + len(items) < total else None,
    })


def _runtime_member_filter_values(
    raw: Any,
    *,
    single: Any = None,
    field_name: str,
    lowercase: bool = False,
) -> tuple[str, ...]:
    if raw in (None, ""):
        values: list[Any] = []
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        raise ValueError(f"{field_name} must be an array")
    if single not in (None, ""):
        values.insert(0, single)
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if lowercase:
            text = text.lower()
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= 50:
            break
    return tuple(normalized)


def _runtime_member_datetime_range(
    after: Any,
    before: Any,
    field_name: str,
) -> tuple[datetime | None, datetime | None]:
    def parse(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(
                    str(value).strip().replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError(
                    f"{field_name} range endpoints must be ISO-8601 datetimes"
                ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    after_dt = parse(after)
    before_dt = parse(before)
    if after_dt is not None and before_dt is not None and after_dt > before_dt:
        raise ValueError(
            f"{field_name}_after must be less than or equal to {field_name}_before"
        )
    return after_dt, before_dt
