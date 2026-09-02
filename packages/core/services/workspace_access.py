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
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.permissions import Permission, user_staff_role_assignment


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


def workspace_resource_not_soft_deleted(
    workspace_id_column,
    *,
    entity_id: str,
):
    """Keep workspace-less/legacy rows, but hide rows owned by trashed workspaces."""

    return ~exists().where(
        Workspace.id == workspace_id_column,
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_not(None),
    )


async def lock_workspace_access_boundary(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
) -> Workspace | None:
    """Serialize Workspace lifecycle, access, and JSON-settings mutations."""

    return (
        await db.execute(
            select(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def lock_workspace_recipient_authorization(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str,
) -> None:
    """Freeze every persisted input to one Workspace read decision.

    Delivery holds these shared row locks through the provider call. Any
    Staff/role/membership/user revocation therefore commits either before the
    authorization check or after the send, never between them. Lock order is
    stable and starts after the caller has locked the Workspace row.
    """

    statements = (
        select(StaffRole.id)
        .where(StaffRole.entity_id == entity_id)
        .order_by(StaffRole.id)
        .with_for_update(read=True),
        select(Staff.id)
        .where(
            Staff.entity_id == entity_id,
            Staff.user_id == user_id,
        )
        .order_by(Staff.id)
        .with_for_update(read=True),
        select(UserMembership.id)
        .where(
            UserMembership.entity_id == entity_id,
            UserMembership.user_id == user_id,
        )
        .order_by(UserMembership.id)
        .with_for_update(read=True),
        select(User.id)
        .where(User.id == user_id)
        .with_for_update(read=True),
        select(WorkspaceStaff.id)
        .where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.user_id == user_id,
        )
        .order_by(WorkspaceStaff.id)
        .with_for_update(read=True),
    )
    for statement in statements:
        await db.execute(statement)


def is_entity_admin_role(role: str | None) -> bool:
    return str(role or "").strip().lower() in ENTITY_ADMIN_ROLES


async def resolve_workspace_read_access(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    role: str | None,
    can_read_entity_workspaces: bool | None = None,
) -> tuple[str | None, bool]:
    """Resolve entity-visible Workspace access from StaffRole when present."""
    resolved_role = str(role or "").strip().lower() or None
    if can_read_entity_workspaces is not None:
        return resolved_role, can_read_entity_workspaces
    if user_id:
        has_staff_record, _, assigned_role, permissions = await user_staff_role_assignment(
            db,
            user_id,
            entity_id,
        )
        if has_staff_record:
            permission_keys = {str(permission) for permission in permissions}
            return (
                str(assigned_role or "").strip().lower() or None,
                Permission.WORKSPACES_READ.value in permission_keys,
            )
    return resolved_role, resolved_role in ENTITY_WORKSPACE_READ_ROLES


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
            .order_by(
                WorkspaceStaff.updated_at.desc(),
                WorkspaceStaff.created_at.desc(),
            )
        )
    ).scalars().all()
    # The database now enforces one user membership per Workspace. Scanning is
    # retained for rolling upgrades; newest wins deterministically.
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
    entity_id = (
        await db.execute(
            select(Workspace.entity_id)
            .where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not entity_id:
        return False
    resolved_role, _ = await resolve_workspace_read_access(
        db,
        entity_id=str(entity_id),
        user_id=user_id,
        role=entity_role,
    )
    if is_entity_admin_role(resolved_role):
        return True
    role = await user_workspace_role(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return str(role or "").strip().lower() in WORKSPACE_ARTIFACT_WRITE_ROLES


async def user_can_manage_workspace(
    db: AsyncSession,
    *,
    workspace_id: str,
    user_id: str | None,
    entity_role: str | None = None,
) -> bool:
    """Return whether a user may administer a Workspace itself.

    Workspace administration is narrower than writing workspace artifacts:
    only an active workspace ``owner`` (or an entity owner/admin) may rename
    or otherwise change Workspace-level settings.  The existence check keeps
    this helper fail-closed for deleted or unknown ids even for entity admins.
    """
    entity_id = (
        await db.execute(
            select(Workspace.entity_id)
            .where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not entity_id:
        return False
    resolved_role, _ = await resolve_workspace_read_access(
        db,
        entity_id=str(entity_id),
        user_id=user_id,
        role=entity_role,
    )
    if is_entity_admin_role(resolved_role):
        return True
    membership = (
        await db.execute(
            select(WorkspaceStaff.role, WorkspaceStaff.expires_at)
            .where(
                WorkspaceStaff.workspace_id == workspace_id,
                WorkspaceStaff.user_id == user_id,
                WorkspaceStaff.status == "active",
            )
            .order_by(
                WorkspaceStaff.updated_at.desc(),
                WorkspaceStaff.created_at.desc(),
            )
            .limit(1)
        )
    ).one_or_none()
    return bool(
        membership
        and _expires_after_now(membership.expires_at)
        and str(membership.role or "").strip().lower() == "owner"
    )


async def manageable_workspace_ids_for_user(
    db: AsyncSession,
    *,
    workspaces: list[Workspace],
    entity_id: str,
    user_id: str | None,
    entity_role: str | None = None,
) -> set[str]:
    """Return manageable ids for an already-scoped Workspace batch.

    This preserves :func:`user_can_manage_workspace` semantics while avoiding
    a role-resolution and membership query for every row in list responses.
    """
    workspace_ids = {
        str(workspace.id)
        for workspace in workspaces
        if workspace.entity_id == entity_id and workspace.deleted_at is None
    }
    if not workspace_ids or not user_id:
        return set()
    resolved_role, _ = await resolve_workspace_read_access(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=entity_role,
    )
    if is_entity_admin_role(resolved_role):
        return workspace_ids

    rows = list((await db.execute(
        select(
            WorkspaceStaff.workspace_id,
            WorkspaceStaff.role,
            WorkspaceStaff.expires_at,
        )
        .where(
            WorkspaceStaff.workspace_id.in_(workspace_ids),
            WorkspaceStaff.user_id == user_id,
            WorkspaceStaff.status == "active",
        )
        .order_by(WorkspaceStaff.workspace_id.asc(), WorkspaceStaff.created_at.asc())
    )).all())
    resolved_memberships: dict[str, tuple[str | None, datetime | None]] = {}
    for row in rows:
        workspace_id = str(row.workspace_id)
        if workspace_id in resolved_memberships:
            continue
        if _expires_after_now(row.expires_at):
            resolved_memberships[workspace_id] = (row.role, row.expires_at)
    return {
        workspace_id
        for workspace_id, (role, _expires_at) in resolved_memberships.items()
        if str(role or "").strip().lower() == "owner"
    }


async def user_can_control_workspace_run(
    db: AsyncSession,
    *,
    run: Any,
    user_id: str | None,
    entity_role: str | None = None,
) -> bool:
    """Return whether a user may mutate an existing Workflow Run."""
    workspace_id = str(getattr(run, "workspace_id", "") or "")
    entity_id = str(getattr(run, "entity_id", "") or "")
    if not workspace_id or not entity_id:
        return False
    if user_id and str(getattr(run, "started_by", "") or "") == str(user_id):
        return await user_can_read_workspace_id(
            db,
            workspace_id=workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=entity_role,
        )
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
    can_read_entity_workspaces: bool | None = None,
) -> bool:
    if (
        not workspace
        or workspace.entity_id != entity_id
        or workspace.deleted_at is not None
    ):
        return False
    resolved_role, can_read_entity_workspaces = await resolve_workspace_read_access(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
        can_read_entity_workspaces=can_read_entity_workspaces,
    )
    if is_entity_admin_role(resolved_role):
        return True
    if await get_active_workspace_membership(
        db,
        workspace_id=workspace.id,
        user_id=user_id,
    ):
        return True
    return (
        workspace_access_mode(workspace) == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
        and can_read_entity_workspaces
    )


async def user_can_read_workspace_id(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
    can_read_entity_workspaces: bool | None = None,
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
            .execution_options(populate_existing=True)
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
        can_read_entity_workspaces=can_read_entity_workspaces,
    )


async def user_readable_workspace_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
    workspace_ids: set[str] | None = None,
    can_read_entity_workspaces: bool | None = None,
) -> set[str]:
    """Resolve readable Workspace ids in one query for list authorization."""
    resolved_role, can_read_entity_workspaces = await resolve_workspace_read_access(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
        can_read_entity_workspaces=can_read_entity_workspaces,
    )
    query = select(Workspace.id).where(
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    )
    if workspace_ids is not None:
        if not workspace_ids:
            return set()
        query = query.where(Workspace.id.in_(workspace_ids))
    if not is_entity_admin_role(resolved_role):
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
            can_read_entity_workspaces,
        )
        query = query.where(or_(active_membership, entity_visible))
    return {str(workspace_id) for workspace_id in (await db.execute(query)).scalars()}


async def user_writable_workspace_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_ids: set[str],
    user_id: str | None,
    role: str | None = None,
) -> set[str]:
    """Resolve writable Workspace ids once for a batch of Workflow Runs."""
    if not workspace_ids:
        return set()
    resolved_role, _ = await resolve_workspace_read_access(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    if is_entity_admin_role(resolved_role):
        query = select(Workspace.id).where(
            Workspace.id.in_(workspace_ids),
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        return {str(workspace_id) for workspace_id in (await db.execute(query)).scalars()}
    if not user_id:
        return set()
    query = (
        select(WorkspaceStaff.workspace_id)
        .join(Workspace, Workspace.id == WorkspaceStaff.workspace_id)
        .where(
            WorkspaceStaff.workspace_id.in_(workspace_ids),
            WorkspaceStaff.user_id == user_id,
            WorkspaceStaff.status == "active",
            WorkspaceStaff.role.in_(WORKSPACE_ARTIFACT_WRITE_ROLES),
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
            or_(
                WorkspaceStaff.expires_at.is_(None),
                WorkspaceStaff.expires_at > datetime.now(UTC),
            ),
        )
        .distinct()
    )
    return {str(workspace_id) for workspace_id in (await db.execute(query)).scalars()}


async def filter_workspaces_for_user(
    db: AsyncSession,
    *,
    workspaces: list[Workspace],
    user: User,
) -> list[Workspace]:
    resolved_role, can_read_entity_workspaces = await resolve_workspace_read_access(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    visible: list[Workspace] = []
    for workspace in workspaces:
        if await user_can_read_workspace_by_identity(
            db,
            workspace=workspace,
            entity_id=user.entity_id,
            user_id=user.id,
            role=resolved_role,
            can_read_entity_workspaces=can_read_entity_workspaces,
        ):
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
    resolved_role, _ = await resolve_workspace_read_access(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    if is_entity_admin_role(resolved_role):
        return True
    membership = await get_active_workspace_membership(
        db, workspace_id=workspace_id, user_id=user_id
    )
    if not membership:
        return False
    return str(membership.role or "").strip().lower() in WORKSPACE_ARTIFACT_WRITE_ROLES


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
    resolved_role, can_read_entity_workspaces = await resolve_workspace_read_access(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )
    if is_entity_admin_role(resolved_role):
        return None

    readable: set[str] = set()
    if user_id:
        member_rows = (
            await db.execute(
                select(WorkspaceStaff.workspace_id, WorkspaceStaff.expires_at)
                .join(Workspace, Workspace.id == WorkspaceStaff.workspace_id)
                .where(
                    WorkspaceStaff.user_id == user_id,
                    WorkspaceStaff.status == "active",
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).all()
        for ws_id, expires_at in member_rows:
            if ws_id and _expires_after_now(expires_at):
                readable.add(ws_id)

    if can_read_entity_workspaces:
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
