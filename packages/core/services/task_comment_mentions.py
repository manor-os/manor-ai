"""Task comment @mentions — parse, validate, and notify mentioned staff.

Comments arrive with structured ``mentions: [{"type": "agent"|"user", "id"}]``
from the frontend (no free-text @ parsing on the backend). This module
validates them against the entity and fans staff mentions out through the
unified ``notify()`` gateway so delivery channels follow user preferences.
"""
from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Agent
# Indirection so tests can monkeypatch the gateway without touching notify.py.
from packages.core.services.notify import notify as _gateway_notify

logger = logging.getLogger(__name__)

_COMMENT_PREVIEW_CHARS = 240


def parse_mention_items(raw: Sequence | None) -> tuple[list[str], list[str]]:
    """Split raw mention payloads into (agent_ids, user_ids).

    Drops junk (non-dicts, empty/non-string ids, unknown types) and
    preserves first-seen order while deduping. Input is capped at 50 items.
    """
    agent_ids: list[str] = []
    user_ids: list[str] = []
    seen_agents: set[str] = set()
    seen_users: set[str] = set()
    for item in list(raw or [])[:50]:
        if not isinstance(item, dict):
            continue
        mention_id = item.get("id")
        if not isinstance(mention_id, str) or not mention_id.strip():
            continue
        mention_id = mention_id.strip()
        mention_type = item.get("type")
        if mention_type == "agent" and mention_id not in seen_agents:
            seen_agents.add(mention_id)
            agent_ids.append(mention_id)
        elif mention_type == "user" and mention_id not in seen_users:
            seen_users.add(mention_id)
            user_ids.append(mention_id)
    return agent_ids, user_ids


async def validate_mentions(
    db: AsyncSession,
    *,
    entity_id: str,
    raw: Sequence | None,
) -> tuple[list[dict], list[dict]]:
    """Validate raw mentions against this entity.

    Returns ``(agent_items, user_items)`` where each item is
    ``{"type", "id", "name"}`` — names are resolved at write time so the
    comment UI can render chips even if the agent/user is later deleted.
    Invalid ids are silently dropped.
    """
    agent_ids, user_ids = parse_mention_items(raw)

    agent_items: list[dict] = []
    if agent_ids:
        rows = (await db.execute(
            select(Agent.id, Agent.name).where(
                Agent.id.in_(agent_ids),
                Agent.entity_id == entity_id,
                Agent.deleted_at.is_(None),
            )
        )).all()
        by_id = {row.id: row.name for row in rows}
        agent_items = [
            {"type": "agent", "id": aid, "name": by_id[aid]}
            for aid in agent_ids if aid in by_id
        ]

    user_items: list[dict] = []
    if user_ids:
        # Entity membership check mirrors notification_routing.resolve_channel_targets.
        rows = (await db.execute(
            select(User.id, User.display_name, User.email).outerjoin(
                UserMembership,
                and_(
                    UserMembership.user_id == User.id,
                    UserMembership.entity_id == entity_id,
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                ),
            ).where(
                User.id.in_(user_ids),
                User.status == "active",
                User.deleted_at.is_(None),
                or_(
                    User.entity_id == entity_id,
                    UserMembership.id.is_not(None),
                ),
            )
        )).all()
        by_id = {row.id: (row.display_name or row.email) for row in rows}
        user_items = [
            {"type": "user", "id": uid, "name": by_id[uid]}
            for uid in user_ids if uid in by_id
        ]

    return agent_items, user_items


async def notify_mentioned_users(
    *,
    entity_id: str,
    author_user_id: str | None,
    author_label: str,
    mentioned_user_ids: Sequence[str],
    task_id: str,
    task_log_id: str | None,
    task_title: str,
    comment: str,
    workspace_id: str | None,
) -> None:
    """Send a mention notification to each staff user via the notify() gateway.

    Channels are NOT pinned — the gateway resolves them from user
    preferences (in-app bell always included; telegram/wechat/email per
    the user's settings). The comment author never notifies themselves.
    Per-user failures are swallowed so one broken delivery can't break
    the rest of the fan-out (or the comment itself).
    """
    preview = (comment or "").strip()[:_COMMENT_PREVIEW_CHARS]
    for user_id in mentioned_user_ids:
        if author_user_id and user_id == author_user_id:
            continue
        try:
            await _gateway_notify(
                entity_id,
                user_id,
                "task_comment_mention",
                f"{author_label} mentioned you in a task comment",
                body=f"{task_title}: {preview}" if preview else task_title,
                link=f"/tasks/{task_id}",
                meta={
                    "task_id": task_id,
                    "task_log_id": task_log_id,
                    "author_label": author_label,
                },
                workspace_id=workspace_id,
            )
        except Exception:
            logger.warning(
                "task_comment_mentions: notify failed for user=%s task=%s",
                user_id, task_id, exc_info=True,
            )
