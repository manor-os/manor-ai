"""RBAC permission system.

Roles: owner > admin > member > viewer
Each role inherits all permissions from lower roles.

Two check paths live here:

  * ``has_permission(role, permission)`` — sync, keyed by role string, uses
    the hardcoded ROLE_PERMISSIONS table. Fine for simple JWT-claim checks.
  * ``user_has_permission(db, user_id, entity_id, permission)`` — async,
    resolves the user's StaffRole in the given entity and checks its
    JSONB ``permissions`` array. Required for data-driven custom roles,
    multi-entity users, and gating per-integration access (MCP servers,
    entity-scope integrations like QuickBooks / Stripe).
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class Permission(str, Enum):
    # Entity management
    ENTITY_READ = "entity.read"
    ENTITY_UPDATE = "entity.update"

    # User management
    USERS_READ = "users.read"
    USERS_INVITE = "users.invite"
    USERS_MANAGE = "users.manage"  # change roles, deactivate

    # Tasks
    TASKS_READ = "tasks.read"
    TASKS_CREATE = "tasks.create"
    TASKS_UPDATE = "tasks.update"
    TASKS_DELETE = "tasks.delete"
    TASKS_ASSIGN = "tasks.assign"

    # Documents
    DOCS_READ = "docs.read"
    DOCS_UPLOAD = "docs.upload"
    DOCS_DELETE = "docs.delete"

    # Agents
    AGENTS_READ = "agents.read"
    AGENTS_CREATE = "agents.create"
    AGENTS_UPDATE = "agents.update"
    AGENTS_DELETE = "agents.delete"

    # Chat
    CHAT_USE = "chat.use"
    CHAT_VIEW_ALL = "chat.view_all"  # view other users' conversations

    # Admin
    ADMIN_SETTINGS = "admin.settings"
    ADMIN_AUDIT = "admin.audit"
    ADMIN_API_KEYS = "admin.api_keys"
    ADMIN_WEBHOOKS = "admin.webhooks"
    ADMIN_BILLING = "admin.billing"

    # Workspaces
    WORKSPACES_READ = "workspaces.read"
    WORKSPACES_CREATE = "workspaces.create"
    WORKSPACES_UPDATE = "workspaces.update"
    WORKSPACES_DELETE = "workspaces.delete"

    # Integrations (OAuth connections, API keys)
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_CONNECT = "integrations.connect"  # add a personal integration
    INTEGRATIONS_MANAGE = "integrations.manage"    # manage owned integrations
    INTEGRATIONS_SHARE = "integrations.share"      # share owned integrations

    # MCP — agent-initiated access to integrations
    MCP_USE_PERSONAL = "mcp.use_personal"          # call any of the user's own MCPs via agents
    MCP_QUICKBOOKS_USE = "mcp.quickbooks.use"      # entity QuickBooks
    MCP_STRIPE_USE = "mcp.stripe.use"              # entity Stripe


# Role -> permissions mapping (hardcoded defaults; seed into staff_roles at init)
ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "viewer": {
        Permission.ENTITY_READ,
        Permission.TASKS_READ,
        Permission.DOCS_READ,
        Permission.AGENTS_READ,
        Permission.CHAT_USE,
        Permission.WORKSPACES_READ,
        Permission.INTEGRATIONS_READ,
    },
    "member": {
        # Inherits viewer +
        Permission.TASKS_CREATE,
        Permission.TASKS_UPDATE,
        Permission.TASKS_ASSIGN,
        Permission.DOCS_UPLOAD,
        Permission.AGENTS_CREATE,
        Permission.INTEGRATIONS_CONNECT,
        Permission.MCP_USE_PERSONAL,
    },
    "admin": {
        # Inherits member +
        Permission.ENTITY_UPDATE,
        Permission.USERS_READ,
        Permission.USERS_INVITE,
        Permission.TASKS_DELETE,
        Permission.DOCS_DELETE,
        Permission.AGENTS_UPDATE,
        Permission.AGENTS_DELETE,
        Permission.WORKSPACES_CREATE,
        Permission.WORKSPACES_UPDATE,
        Permission.WORKSPACES_DELETE,
        Permission.ADMIN_SETTINGS,
        Permission.ADMIN_AUDIT,
        Permission.CHAT_VIEW_ALL,
        Permission.INTEGRATIONS_MANAGE,
        Permission.INTEGRATIONS_SHARE,
        Permission.MCP_QUICKBOOKS_USE,
        Permission.MCP_STRIPE_USE,
    },
    "owner": {
        # All permissions
        Permission.USERS_MANAGE,
        Permission.ADMIN_API_KEYS,
        Permission.ADMIN_WEBHOOKS,
        Permission.ADMIN_BILLING,
    },
}


_ROLE_HIERARCHY = ["viewer", "member", "admin", "owner"]
PROTECTED_STAFF_ROLE_NAMES = {"owner", "admin"}


def is_protected_staff_role_name(role_name: str | None) -> bool:
    return (role_name or "").strip().lower() in PROTECTED_STAFF_ROLE_NAMES


def _get_role_permissions(role: str) -> set[Permission]:
    """Get all permissions for a role (including inherited)."""
    if role not in _ROLE_HIERARCHY:
        return set()
    perms: set[Permission] = set()
    for r in _ROLE_HIERARCHY:
        perms |= ROLE_PERMISSIONS.get(r, set())
        if r == role:
            break
    return perms


def has_permission(role: str, permission: Permission) -> bool:
    """Check if a role string has a specific permission (sync, hardcoded table)."""
    return permission in _get_role_permissions(role)


def check_permission(role: str, permission: Permission) -> None:
    """Raise 403 if the role doesn't have the permission (sync)."""
    if not has_permission(role, permission):
        raise HTTPException(
            403, f"Permission denied: {permission.value} requires higher role"
        )


# ── Data-driven path (staff_roles JSONB) ─────────────────────────────────────

async def user_has_permission(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
    permission: Permission | str,
) -> bool:
    """Check if a user has a permission in a given entity.

    Resolves via Staff -> StaffRole.permissions (JSONB array). Returns False
    if the user is not a staff member of the entity or has no role assigned.
    """
    # Local import to avoid circulars (models pull in permissions at import time)
    from packages.core.models.staff import Staff, StaffRole

    perm_value = permission.value if isinstance(permission, Permission) else str(permission)

    staff_row = (
        await db.execute(
            select(Staff).where(
                Staff.user_id == user_id,
                Staff.entity_id == entity_id,
                Staff.status == "active",
                Staff.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if not staff_row or not staff_row.role_id:
        return False

    role = (
        await db.execute(
            select(StaffRole).where(
                StaffRole.id == staff_row.role_id,
                StaffRole.entity_id == entity_id,
                StaffRole.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not role:
        return False

    return perm_value in (role.permissions or [])


async def effective_user_has_permission(
    db: "AsyncSession",
    user,
    permission: Permission | str,
) -> bool:
    """Check permission using Staff authority, otherwise legacy role.

    Invite-created team users honor the editable StaffRole permission set.
    Pre-StaffRole rows may use their Staff-owned metadata role. Only accounts
    with no linked Staff row keep the legacy ``User.role`` fallback.
    """
    entity_id = getattr(user, "entity_id", None)
    user_id = getattr(user, "id", None)
    perm_value = permission.value if isinstance(permission, Permission) else str(permission)

    if entity_id and user_id:
        has_staff_record, _, _, permissions = await user_staff_role_assignment(
            db,
            user_id,
            entity_id,
        )
        if has_staff_record:
            return perm_value in permissions

    return has_permission(getattr(user, "role", ""), permission)


async def check_effective_user_permission(
    db: "AsyncSession",
    user,
    permission: Permission | str,
) -> None:
    """Raise 403 if the user lacks the effective permission."""
    if not await effective_user_has_permission(db, user, permission):
        perm_value = permission.value if isinstance(permission, Permission) else permission
        raise HTTPException(403, f"Permission denied: {perm_value}")


async def user_staff_role_assignment(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
) -> tuple[bool, str | None, str | None, list[str]]:
    """Return the authoritative Staff assignment for one entity.

    The first value reports whether any linked Staff row exists. Active Staff
    supplies permissions; inactive or deleted Staff fails closed. Only users
    with no linked Staff history may fall back to legacy ``User.role`` during
    migration.
    """
    from packages.core.models.staff import Staff, StaffRole

    staff_rows = list(
        (
            await db.execute(
                select(Staff).where(
                    Staff.user_id == user_id,
                    Staff.entity_id == entity_id,
                )
            )
        )
        .scalars()
        .all()
    )

    if not staff_rows:
        return False, None, None, []
    active_staff_rows = [
        row
        for row in staff_rows
        if row.status == "active" and row.deleted_at is None
    ]
    if len(active_staff_rows) != 1:
        return True, None, None, []
    staff_row = active_staff_rows[0]
    if not staff_row.role_id:
        # Legacy Staff rows created before StaffRole seeding store their
        # system role in metadata.  This is still Staff-owned authority, not
        # a fallback to the independently mutable/stale User.role column.
        legacy_role = str((staff_row.meta or {}).get("role") or "").strip().lower()
        legacy_permissions = _get_role_permissions(legacy_role)
        if legacy_role:
            return (
                True,
                None,
                legacy_role,
                sorted(permission.value for permission in legacy_permissions),
            )
        return True, None, None, []

    role = (
        await db.execute(
            select(StaffRole).where(
                StaffRole.id == staff_row.role_id,
                StaffRole.entity_id == entity_id,
                StaffRole.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not role:
        return True, staff_row.role_id, None, []

    return True, role.id, role.name, list(role.permissions or [])


async def user_staff_role_summary(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
) -> tuple[str | None, str | None, list[str]]:
    """Return the configured StaffRole while preserving the legacy API shape."""
    _, role_id, role_name, permissions = await user_staff_role_assignment(
        db,
        user_id,
        entity_id,
    )
    return role_id, role_name, permissions


async def resolve_effective_user_role_name(
    db: "AsyncSession",
    *,
    user_id: str | None,
    entity_id: str | None,
    legacy_role: str | None = None,
) -> str:
    """Resolve one user's authoritative role inside a target entity.

    Staff is authoritative whenever any linked Staff history exists. Without
    Staff, an active entity membership wins over compatibility role values;
    legacy User.role is used only for accounts not yet represented there.
    """
    if not user_id or not entity_id:
        return (legacy_role or "").strip().lower()

    has_staff_record, _, assigned_role, _ = await user_staff_role_assignment(
        db,
        user_id,
        entity_id,
    )
    if has_staff_record:
        return (assigned_role or "").strip().lower()

    from packages.core.models.user import User, UserMembership

    membership = (
        await db.execute(
            select(
                UserMembership.role,
                UserMembership.status,
                UserMembership.deleted_at,
            ).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == entity_id,
            )
        )
    ).one_or_none()
    if membership is not None:
        membership_role, membership_status, membership_deleted_at = membership
        if membership_status == "active" and membership_deleted_at is None:
            return str(membership_role or "").strip().lower()
        return ""
    if legacy_role:
        return legacy_role.strip().lower()

    user_role = (
        await db.execute(
            select(User.role).where(
                User.id == user_id,
                User.entity_id == entity_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return str(user_role or "").strip().lower()


async def user_effective_permission_keys(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
    legacy_role: str | None = None,
) -> set[str]:
    """Return StaffRole keys, falling back only when no linked Staff exists."""
    has_staff_record, _, _, staff_role_perms = await user_staff_role_assignment(
        db,
        user_id,
        entity_id,
    )
    if has_staff_record:
        return {str(permission) for permission in staff_role_perms}
    return {permission.value for permission in _get_role_permissions(legacy_role or "")}


async def user_has_effective_permission(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
    legacy_role: str | None,
    permission: Permission | str,
) -> bool:
    perm_value = permission.value if isinstance(permission, Permission) else str(permission)
    return perm_value in await user_effective_permission_keys(
        db,
        user_id,
        entity_id,
        legacy_role,
    )


async def check_effective_permission(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
    legacy_role: str | None,
    permission: Permission | str,
) -> None:
    """Raise 403 unless the authoritative effective role grants permission."""
    if not await user_has_effective_permission(
        db,
        user_id,
        entity_id,
        legacy_role,
        permission,
    ):
        perm_value = permission.value if isinstance(permission, Permission) else permission
        raise HTTPException(403, f"Permission denied: {perm_value}")


def legacy_role_from_role_name(role_name: str | None, default: str = "member") -> str:
    """Map a StaffRole name to the legacy JWT role claim when possible."""
    normalized = (role_name or "").strip().lower()
    if normalized in set(_ROLE_HIERARCHY):
        return normalized
    return default


async def legacy_role_for_staff_role(
    db: "AsyncSession",
    role_id: str | None,
    entity_id: str,
    default: str = "member",
) -> str:
    """Best-effort legacy role for a Staff.role_id.

    Custom StaffRoles intentionally map to ``default``; their configured
    permission keys are evaluated via ``user_effective_permission_keys``.
    """
    if not role_id:
        return default

    from packages.core.models.staff import StaffRole

    role = (
        await db.execute(
            select(StaffRole).where(
                StaffRole.id == role_id,
                StaffRole.entity_id == entity_id,
                StaffRole.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not role:
        return default
    return legacy_role_from_role_name(role.name, default=default)


async def assigned_staff_role_name(
    db: "AsyncSession",
    *,
    user_id: str | None,
    entity_id: str | None,
) -> str | None:
    """Return the active Staff-authoritative role name in one entity."""
    if entity_id and user_id:
        _, _, role_name, _ = await user_staff_role_assignment(
            db,
            user_id,
            entity_id,
        )
        if role_name:
            return str(role_name).strip().lower()

    return None


async def effective_user_role_name(db: "AsyncSession", user) -> str:
    """Return StaffRole name, falling back only without a linked Staff row."""
    return await resolve_effective_user_role_name(
        db,
        user_id=getattr(user, "id", None),
        entity_id=getattr(user, "entity_id", None),
        legacy_role=getattr(user, "role", None),
    )


async def user_is_effective_entity_admin(db: "AsyncSession", user) -> bool:
    """Return whether the actor's effective role is an entity administrator.

    A linked ``Staff`` assignment is authoritative when present. Inactive and
    deleted Staff fail closed so stale ``User.role`` values cannot retain
    administrator privileges after authority is revoked.
    """
    return await effective_user_role_name(db, user) in {"owner", "admin"}


async def user_is_effective_owner(db: "AsyncSession", user) -> bool:
    return await effective_user_role_name(db, user) == "owner"


async def staff_effective_role_name(db: "AsyncSession", staff) -> str | None:
    """Resolve a staff row's assigned role name from StaffRole or legacy meta."""
    from packages.core.models.staff import StaffRole

    role_id = getattr(staff, "role_id", None)
    if role_id:
        role = await db.get(StaffRole, role_id)
        if role and role.entity_id == getattr(staff, "entity_id", None):
            return (role.name or "").strip().lower() or None

    meta = getattr(staff, "meta", None) or {}
    return (meta.get("role") or "").strip().lower() or None


async def staff_is_protected_management_target(db: "AsyncSession", staff) -> bool:
    """Return true when modifying this staff row requires entity ownership."""
    from packages.core.models.user import UserMembership

    if is_protected_staff_role_name(await staff_effective_role_name(db, staff)):
        return True

    user_id = getattr(staff, "user_id", None)
    if not user_id:
        return False
    membership = (
        await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == getattr(staff, "entity_id", None),
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return bool(membership and is_protected_staff_role_name(getattr(membership, "role", None)))


async def check_user_permission(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
    permission: Permission | str,
) -> None:
    """Raise 403 if the user lacks the permission in this entity (async)."""
    if not await user_has_permission(db, user_id, entity_id, permission):
        perm_value = permission.value if isinstance(permission, Permission) else permission
        raise HTTPException(403, f"Permission denied: {perm_value}")
