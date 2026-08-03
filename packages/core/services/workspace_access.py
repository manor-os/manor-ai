"""Workspace visibility helpers.

Runtime resolution decides which tools/context are bound to a workspace; these
helpers decide whether the acting user can see that workspace in the first
place.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, or_, select
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
WORKSPACE_ARTIFACT_WRITE_ROLES = {"owner", "editor", "contributor"}
# Workspace-level roles that may read but never create/modify workspace content.
WORKSPACE_READONLY_ROLES = {"viewer"}


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
    rows = (
        await db.execute(
            select(WorkspaceStaff)
            .where(
                WorkspaceStaff.workspace_id == workspace_id,
                WorkspaceStaff.user_id == user_id,
                WorkspaceStaff.status == "active",
            )
            .order_by(WorkspaceStaff.created_at.asc())
        )
    ).scalars().all()
    # Return the first still-valid membership. Scanning all active rows (rather
    # than only the oldest) avoids wrongly denying a user who has an old
    # expired-but-active row alongside a newer valid one.
    for row in rows:
        if _expires_after_now(row.expires_at):
            return row
    return None


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


async def user_can_write_workspace_artifacts(
    db: AsyncSession,
    *,
    workspace_id: str,
    user_id: str | None,
    entity_role: str | None = None,
) -> bool:
    if is_entity_admin_role(entity_role):
        return True
    role = await user_workspace_role(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return str(role or "").strip().lower() in WORKSPACE_ARTIFACT_WRITE_ROLES


async def user_can_control_workspace_run(
    db: AsyncSession,
    *,
    run: Any,
    user_id: str | None,
    entity_role: str | None = None,
) -> bool:
    """Return whether a user may mutate an existing Workflow Run."""
    if user_id and str(getattr(run, "started_by", "") or "") == str(user_id):
        return True
    if is_entity_admin_role(entity_role):
        return True
    workspace_id = str(getattr(run, "workspace_id", "") or "")
    if not workspace_id:
        return False
    return await user_can_write_workspace_artifacts(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        entity_role=entity_role,
    )


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


async def user_readable_workspace_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
    workspace_ids: set[str] | None = None,
) -> set[str]:
    """Resolve readable Workspace ids in one query for list authorization."""
    query = select(Workspace.id).where(
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    )
    if workspace_ids is not None:
        if not workspace_ids:
            return set()
        query = query.where(Workspace.id.in_(workspace_ids))
    if not is_entity_admin_role(role):
        active_membership = exists(
            select(WorkspaceStaff.id).where(
                WorkspaceStaff.workspace_id == Workspace.id,
                WorkspaceStaff.user_id == user_id,
                WorkspaceStaff.status == "active",
                or_(
                    WorkspaceStaff.expires_at.is_(None),
                    WorkspaceStaff.expires_at > datetime.now(UTC),
                ),
            )
        )
        entity_visible = and_(
            Workspace.settings[WORKSPACE_ACCESS_MODE_KEY].astext
            == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE,
            str(role or "").strip().lower() in ENTITY_WORKSPACE_READ_ROLES,
        )
        query = query.where(or_(active_membership, entity_visible))
    return {str(workspace_id) for workspace_id in (await db.execute(query)).scalars()}


async def user_writable_workspace_ids(
    db: AsyncSession,
    *,
    workspace_ids: set[str],
    user_id: str | None,
    role: str | None = None,
) -> set[str]:
    """Resolve writable Workspace ids once for a batch of Workflow Runs."""
    if not workspace_ids:
        return set()
    if is_entity_admin_role(role):
        return set(workspace_ids)
    if not user_id:
        return set()
    query = select(WorkspaceStaff.workspace_id).where(
        WorkspaceStaff.workspace_id.in_(workspace_ids),
        WorkspaceStaff.user_id == user_id,
        WorkspaceStaff.status == "active",
        WorkspaceStaff.role.in_(WORKSPACE_ARTIFACT_WRITE_ROLES),
        or_(
            WorkspaceStaff.expires_at.is_(None),
            WorkspaceStaff.expires_at > datetime.now(UTC),
        ),
    ).distinct()
    return {str(workspace_id) for workspace_id in (await db.execute(query)).scalars()}


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


async def user_can_write_workspace_id(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
) -> bool:
    """Whether the user may create or modify content inside this workspace.

    Distinct from reading: an ``entity_visible`` workspace is readable by the
    whole organization, but only actual members may write to it. Membership
    roles in :data:`WORKSPACE_READONLY_ROLES` (``viewer``) are read-only.
    Entity owner/admin keep the firm-wide override.
    """
    if is_entity_admin_role(role):
        return True
    workspace = (
        await db.execute(
            select(Workspace.id)
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
    membership = await get_active_workspace_membership(
        db, workspace_id=workspace_id, user_id=user_id
    )
    if not membership:
        return False
    return str(membership.role or "").strip().lower() not in WORKSPACE_READONLY_ROLES


async def readable_workspace_ids_for_user(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    role: str | None,
) -> set[str] | None:
    """Workspace ids this user may read, or ``None`` for an unrestricted caller.

    Returns ``None`` for an entity owner/admin (firm-wide visibility — no
    restriction should be applied) so callers can skip filtering entirely.
    Otherwise returns the set of readable workspace ids: the user's active,
    non-expired memberships plus every ``entity_visible`` workspace (when the
    role is allowed entity-wide read). The set may be empty — that is distinct
    from ``None`` and means "no workspaces are readable" (the caller should
    then surface only entity-level, workspace-less rows).

    Used to scope entity-wide list endpoints (tasks/goals/plans) so a
    ``members_only`` workspace's rows don't leak to non-members through the
    no-``workspace_id`` default.
    """
    if is_entity_admin_role(role):
        return None

    readable: set[str] = set()
    if user_id:
        member_rows = (
            await db.execute(
                select(WorkspaceStaff.workspace_id, WorkspaceStaff.expires_at)
                .where(
                    WorkspaceStaff.user_id == user_id,
                    WorkspaceStaff.status == "active",
                )
            )
        ).all()
        for ws_id, expires_at in member_rows:
            if ws_id and _expires_after_now(expires_at):
                readable.add(ws_id)

    if str(role or "").strip().lower() in ENTITY_WORKSPACE_READ_ROLES:
        visible_rows = (
            await db.execute(
                select(Workspace.id, Workspace.settings).where(
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).all()
        for ws_id, settings in visible_rows:
            if workspace_access_mode(
                Workspace(settings=settings or {})
            ) == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE:
                readable.add(ws_id)

    return readable


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
