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
    INTEGRATIONS_MANAGE = "integrations.manage"    # manage entity-scope integrations

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
            )
        )
    ).scalar_one_or_none()

    if not staff_row or not staff_row.role_id:
        return False

    role = await db.get(StaffRole, staff_row.role_id)
    if not role or role.status != "active":
        return False

    return perm_value in (role.permissions or [])


async def effective_user_has_permission(
    db: "AsyncSession",
    user,
    permission: Permission | str,
) -> bool:
    """Check permission using StaffRole when present, otherwise legacy role.

    Invite-created team users should honor the editable StaffRole permission
    set. Older owner/admin accounts may not have a linked Staff row yet, so
    they keep the legacy ``User.role`` fallback.
    """
    from packages.core.models.staff import Staff, StaffRole

    entity_id = getattr(user, "entity_id", None)
    user_id = getattr(user, "id", None)
    perm_value = permission.value if isinstance(permission, Permission) else str(permission)

    if entity_id and user_id:
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

        role_id = getattr(staff_row, "role_id", None)
        if staff_row and role_id:
            role = await db.get(StaffRole, role_id)
            if role and role.entity_id == entity_id and role.status == "active":
                return perm_value in (role.permissions or [])

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


async def user_staff_role_summary(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
) -> tuple[str | None, str | None, list[str]]:
    """Return the active StaffRole attached to a user in an entity.

    The tuple is ``(role_id, role_name, permissions)``. Empty values mean the
    user has no active Staff/StaffRole link for that entity.
    """
    from packages.core.models.staff import Staff, StaffRole

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
        return None, None, []

    role = await db.get(StaffRole, staff_row.role_id)
    if not role or role.status != "active":
        return staff_row.role_id, None, []

    return role.id, role.name, list(role.permissions or [])


async def user_effective_permission_keys(
    db: "AsyncSession",
    user_id: str,
    entity_id: str,
    legacy_role: str | None = None,
) -> set[str]:
    """Union legacy ``User.role`` permissions with configured StaffRole keys."""
    perms = {p.value for p in _get_role_permissions(legacy_role or "")}
    _, _, staff_role_perms = await user_staff_role_summary(db, user_id, entity_id)
    perms.update(str(p) for p in staff_role_perms)
    return perms


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
    """Raise 403 unless legacy role or StaffRole grants the permission."""
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


async def effective_user_role_name(db: "AsyncSession", user) -> str:
    """Return the actor's effective role name, preferring StaffRole."""
    from packages.core.models.staff import Staff, StaffRole

    entity_id = getattr(user, "entity_id", None)
    user_id = getattr(user, "id", None)

    if entity_id and user_id:
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
        role_id = getattr(staff_row, "role_id", None)
        if staff_row and role_id:
            role = await db.get(StaffRole, role_id)
            if role and role.entity_id == entity_id and role.status == "active":
                return (role.name or "").strip().lower()

    return (getattr(user, "role", "") or "").strip().lower()


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
