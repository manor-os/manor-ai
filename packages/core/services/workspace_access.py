"""Workspace visibility helpers.

Runtime resolution decides which tools/context are bound to a workspace; these
helpers decide whether the acting user can see that workspace in the first
place.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.staff import Staff
from packages.core.models.user import User
from packages.core.models.workspace import Workspace, WorkspaceStaff


WORKSPACE_ACCESS_MODE_KEY = "access_mode"
WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE = "entity_visible"
WORKSPACE_ACCESS_MODE_MEMBERS_ONLY = "members_only"
WORKSPACE_ACCESS_MODES = {
    WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE,
    WORKSPACE_ACCESS_MODE_MEMBERS_ONLY,
}
ENTITY_ADMIN_ROLES = {"owner", "admin"}
ENTITY_WORKSPACE_READ_ROLES = {"owner", "admin", "member", "viewer"}


def workspace_access_mode(workspace: Workspace) -> str:
    settings = dict(getattr(workspace, "settings", None) or {})
    mode = str(settings.get(WORKSPACE_ACCESS_MODE_KEY) or "").strip()
    if mode in WORKSPACE_ACCESS_MODES:
        return mode
    return WORKSPACE_ACCESS_MODE_MEMBERS_ONLY


def settings_with_default_workspace_access(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    next_settings = dict(settings or {})
    next_settings.setdefault(WORKSPACE_ACCESS_MODE_KEY, WORKSPACE_ACCESS_MODE_MEMBERS_ONLY)
    return next_settings


def is_entity_admin_role(role: str | None) -> bool:
    return str(role or "").strip().lower() in ENTITY_ADMIN_ROLES


def _expires_after_now(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    now = datetime.now(UTC)
    if expires_at.tzinfo is None:
        return expires_at > now.replace(tzinfo=None)
    return expires_at > now


async def get_active_workspace_membership(
    db: AsyncSession,
    *,
    workspace_id: str,
    user_id: str | None,
) -> WorkspaceStaff | None:
    if not user_id:
        return None
    row = (
        await db.execute(
            select(WorkspaceStaff)
            .where(
                WorkspaceStaff.workspace_id == workspace_id,
                WorkspaceStaff.user_id == user_id,
                WorkspaceStaff.status == "active",
            )
            .order_by(WorkspaceStaff.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not row or not _expires_after_now(row.expires_at):
        return None
    return row


async def user_workspace_role(
    db: AsyncSession,
    *,
    workspace_id: str,
    user_id: str | None,
) -> str | None:
    row = await get_active_workspace_membership(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return row.role if row else None


async def user_can_read_workspace(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user: User,
) -> bool:
    return await user_can_read_workspace_by_identity(
        db,
        workspace=workspace,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )


async def user_can_read_workspace_by_identity(
    db: AsyncSession,
    *,
    workspace: Workspace,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
) -> bool:
    if not workspace or workspace.entity_id != entity_id:
        return False
    if is_entity_admin_role(role):
        return True
    if await get_active_workspace_membership(
        db,
        workspace_id=workspace.id,
        user_id=user_id,
    ):
        return True
    return (
        workspace_access_mode(workspace) == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
        and str(role or "").strip().lower() in ENTITY_WORKSPACE_READ_ROLES
    )


async def user_can_read_workspace_id(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
) -> bool:
    workspace = (
        await db.execute(
            select(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not workspace:
        return False
    return await user_can_read_workspace_by_identity(
        db,
        workspace=workspace,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )


async def filter_workspaces_for_user(
    db: AsyncSession,
    *,
    workspaces: list[Workspace],
    user: User,
) -> list[Workspace]:
    visible: list[Workspace] = []
    for workspace in workspaces:
        if await user_can_read_workspace(db, workspace=workspace, user=user):
            visible.append(workspace)
    return visible


async def ensure_workspace_owner_membership(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
    added_by: str | None = None,
) -> WorkspaceStaff | None:
    """Make a user the workspace owner for newly created workspaces.

    Workspace creation can happen from several services, not only the REST
    create endpoint. This keeps those paths from creating a members-only
    workspace that the creator cannot manage.
    """
    if not user_id:
        return None

    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.entity_id == entity_id,
                User.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not user:
        return None

    staff = (
        await db.execute(
            select(Staff).where(
                Staff.entity_id == entity_id,
                Staff.user_id == user_id,
                Staff.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if staff is None:
        staff = Staff(
            id=generate_ulid(),
            entity_id=entity_id,
            kind="employee",
            name=user.display_name or user.email.split("@")[0],
            email=user.email,
            avatar_url=user.avatar_url,
            user_id=user.id,
            meta={"role": user.role},
            status="active",
        )
        db.add(staff)
        await db.flush()

    membership = (
        await db.execute(
            select(WorkspaceStaff)
            .where(
                WorkspaceStaff.workspace_id == workspace_id,
                or_(
                    WorkspaceStaff.user_id == user_id,
                    WorkspaceStaff.staff_id == staff.id,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if membership is None:
        membership = WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=workspace_id,
            staff_id=staff.id,
            user_id=user_id,
            role="owner",
            added_by=added_by or user_id,
            added_at=datetime.now(UTC),
            status="active",
        )
        db.add(membership)
    else:
        membership.staff_id = membership.staff_id or staff.id
        membership.user_id = membership.user_id or user_id
        membership.role = "owner"
        membership.status = "active"
        if not membership.added_by:
            membership.added_by = added_by or user_id
        if not membership.added_at:
            membership.added_at = datetime.now(UTC)
    await db.flush()
    return membership
