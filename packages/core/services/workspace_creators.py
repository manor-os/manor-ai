"""Who created, or now owns, an installed Workspace.

Precedence: ``settings.created_by_user_id``; then the earliest owner seat;
then the earliest seat. Seats from external collaboration grants never count.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User
from packages.core.models.workspace import WorkspaceStaff


async def workspace_creator_users(db: AsyncSession, workspaces: list[Any]) -> dict[str, User]:
    workspace_ids = [ws.id for ws in workspaces if getattr(ws, "id", None)]
    if not workspace_ids:
        return {}

    configured_ids: dict[str, str] = {}
    for ws in workspaces:
        settings = ws.settings if isinstance(getattr(ws, "settings", None), dict) else {}
        user_id = settings.get("created_by_user_id")
        if isinstance(user_id, str) and user_id:
            configured_ids[ws.id] = user_id

    # Seats derived from an external collaboration grant never stand in for
    # the customer's own creator/owner.
    staff_rows = list((await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id.in_(workspace_ids),
            WorkspaceStaff.collaboration_id.is_(None),
        )
    )).scalars().all())
    staff_rows.sort(
        key=lambda row: (
            row.workspace_id,
            (row.added_at or row.created_at).isoformat()
            if (row.added_at or row.created_at)
            else "9999",
        )
    )

    candidate_ids = set(configured_ids.values())
    for row in staff_rows:
        if row.user_id:
            candidate_ids.add(row.user_id)
        if row.added_by:
            candidate_ids.add(row.added_by)
    if not candidate_ids:
        return {}

    users = list((await db.execute(
        select(User).where(User.id.in_(list(candidate_ids)), User.deleted_at.is_(None))
    )).scalars().all())
    users_by_id = {user.id: user for user in users}

    result: dict[str, User] = {}
    for workspace_id, user_id in configured_ids.items():
        if user := users_by_id.get(user_id):
            result[workspace_id] = user

    for row in staff_rows:
        if row.workspace_id in result or row.role != "owner":
            continue
        candidate_id = row.user_id or row.added_by
        if candidate_id and (user := users_by_id.get(candidate_id)):
            result[row.workspace_id] = user

    for row in staff_rows:
        if row.workspace_id in result:
            continue
        candidate_id = row.user_id or row.added_by
        if candidate_id and (user := users_by_id.get(candidate_id)):
            result[row.workspace_id] = user

    return result
