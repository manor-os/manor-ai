"""Permission catalog + staff role CRUD + staff invitations.

This router owns the role-configuration surface used by the Team/Roles
page. Three concerns live here because they are all driven by the same
permission vocabulary:

  * GET  /permissions                 — vocabulary (enum grouped by module)
  * GET  /staff/roles                 — list entity roles (system + custom)
  * POST /staff/roles                 — create custom role
  * PUT  /staff/roles/{role_id}       — rename or edit permissions
  * DELETE /staff/roles/{role_id}     — delete (reassigns staff to fallback)
  * POST /staff/invite                — create pending staff + invite token
"""
from __future__ import annotations

import secrets
from urllib.parse import urlencode
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, require_plan
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace
from packages.core.permissions import (
    Permission,
    check_effective_permission,
)
from packages.core.services.auth_service import (
    create_access_token,
)
from packages.core.services.team_invite_service import (
    accept_team_invite_for_user,
    pending_staff_for_token,
)


router = APIRouter(prefix="/api/v1", tags=["permissions"])


# ── Permission catalog ───────────────────────────────────────────────────────

# Grouping drives the UI sections. Keep module labels short (single word
# where possible) so the admin checklist reads cleanly.
_PERMISSION_GROUPS = [
    ("Entity",        "entity."),
    ("Users",         "users."),
    ("Tasks",         "tasks."),
    ("Documents",     "docs."),
    ("Agents",        "agents."),
    ("Chat",          "chat."),
    ("Workspaces",    "workspaces."),
    ("Integrations",  "integrations."),
    ("MCP",           "mcp."),
    ("Admin",         "admin."),
]


class PermissionItem(BaseModel):
    key: str
    label: str  # human label derived from enum name


class PermissionGroup(BaseModel):
    name: str
    permissions: list[PermissionItem]


@router.get("/permissions", response_model=list[PermissionGroup])
async def list_permission_catalog(
    _user: User = Depends(get_current_user),
):
    """Return the full permission vocabulary, grouped for UI checklist."""
    groups: dict[str, list[PermissionItem]] = {name: [] for name, _ in _PERMISSION_GROUPS}

    for perm in Permission:
        for name, prefix in _PERMISSION_GROUPS:
            if perm.value.startswith(prefix):
                # Convert "tasks.create" -> "Create" (drop the module prefix,
                # replace dots/underscores with spaces, title-case)
                tail = perm.value[len(prefix):].replace("_", " ").replace(".", " ")
                label = tail[:1].upper() + tail[1:] if tail else perm.value
                groups[name].append(PermissionItem(key=perm.value, label=label))
                break

    return [
        PermissionGroup(name=name, permissions=groups[name])
        for name, _ in _PERMISSION_GROUPS
        if groups[name]
    ]


# ── Staff Role CRUD ──────────────────────────────────────────────────────────

_SYSTEM_ROLE_NAMES = {"owner", "admin", "member", "viewer"}


class RoleResponse(BaseModel):
    id: str
    name: str
    permissions: list[str]
    is_default: bool
    is_system: bool          # True for viewer/member/admin/owner
    staff_count: int         # how many active staff hold this role
    status: str = "active"


class RoleCreateRequest(BaseModel):
    name: str
    permissions: list[str] = []
    is_default: bool = False


class RoleUpdateRequest(BaseModel):
    name: Optional[str] = None
    permissions: Optional[list[str]] = None
    is_default: Optional[bool] = None


async def _role_to_response(db: AsyncSession, role: StaffRole) -> RoleResponse:
    count_q = await db.execute(
        select(func.count())
        .select_from(Staff)
        .where(
            Staff.role_id == role.id,
            Staff.status == "active",
            Staff.deleted_at.is_(None),
        )
    )
    count = count_q.scalar() or 0
    return RoleResponse(
        id=role.id,
        name=role.name,
        permissions=list(role.permissions or []),
        is_default=bool(role.is_default),
        is_system=role.name.lower() in _SYSTEM_ROLE_NAMES,
        staff_count=int(count),
        status=role.status,
    )


@router.get("/staff/roles", response_model=list[RoleResponse])
async def list_entity_roles(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(StaffRole).where(StaffRole.entity_id == user.entity_id)
    )
    roles = list(rows.scalars().all())
    # If the entity has no roles yet, synthesize the 4 system defaults
    # from the hardcoded permissions.py map. First request after entity
    # creation will hit this path.
    if not roles:
        roles = await _seed_default_roles_for_entity(db, user.entity_id)
    return [await _role_to_response(db, r) for r in roles]


@router.post("/staff/roles", response_model=RoleResponse, status_code=201)
async def create_role(
    req: RoleCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_permission(db, user, Permission.USERS_MANAGE)

    if req.name.lower() in _SYSTEM_ROLE_NAMES:
        raise HTTPException(
            400, "Cannot reuse a system role name. Pick a different name."
        )

    # Ensure every submitted permission key is a real Permission enum value.
    _validate_permission_keys(req.permissions)

    role = StaffRole(
        id=generate_ulid(),
        entity_id=user.entity_id,
        name=req.name,
        permissions=req.permissions,
        is_default=req.is_default,
        status="active",
    )
    db.add(role)
    await db.flush()
    return await _role_to_response(db, role)


@router.put("/staff/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: str,
    req: RoleUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_permission(db, user, Permission.USERS_MANAGE)

    role = await _load_role(db, role_id, user.entity_id)
    is_system = role.name.lower() in _SYSTEM_ROLE_NAMES

    if req.name is not None:
        if is_system:
            raise HTTPException(400, "System role names cannot be changed.")
        if req.name.lower() in _SYSTEM_ROLE_NAMES:
            raise HTTPException(400, "That name collides with a system role.")
        role.name = req.name

    if req.permissions is not None:
        _validate_permission_keys(req.permissions)
        role.permissions = req.permissions

    if req.is_default is not None:
        role.is_default = req.is_default

    await db.flush()
    return await _role_to_response(db, role)


class RoleDeleteRequest(BaseModel):
    reassign_to_role_id: Optional[str] = None
    # If provided, all staff currently assigned to the deleted role are
    # moved to this one. Otherwise staff are left with role_id = NULL.


@router.delete("/staff/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: str,
    req: Optional[RoleDeleteRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_permission(db, user, Permission.USERS_MANAGE)

    role = await _load_role(db, role_id, user.entity_id)
    if role.name.lower() in _SYSTEM_ROLE_NAMES:
        raise HTTPException(400, "System roles cannot be deleted.")

    fallback_id = req.reassign_to_role_id if req else None
    if fallback_id:
        # Confirm the fallback role exists and is in the same entity.
        fb = await _load_role(db, fallback_id, user.entity_id)
        if fb.id == role.id:
            raise HTTPException(400, "Cannot reassign to the role being deleted.")
    else:
        fallback_id = None

    # Reassign or null-out staff.role_id for affected rows.
    staff_rows = (
        await db.execute(
            select(Staff).where(
                Staff.role_id == role.id,
                Staff.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    for s in staff_rows:
        s.role_id = fallback_id

    await db.delete(role)
    await db.flush()


# ── Staff invite flow ────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    email: str
    role_id: Optional[str] = None    # one of the entity's roles
    workspace_ids: list[str] = Field(default_factory=list)  # optional workspace assignments
    name: Optional[str] = None        # optional hint; invitee fills on accept


class InviteResponse(BaseModel):
    staff_id: str
    invite_token: str
    invite_url: str
    email: str
    role_id: Optional[str]
    status: str  # "invited"
    email_sent: bool = False


class AcceptInviteRequest(BaseModel):
    token: str
    name: Optional[str] = None    # override the placeholder set on invite
    phone: Optional[str] = None


class AcceptInviteResponse(BaseModel):
    access_token: Optional[str] = None
    user_id: str
    entity_id: str
    staff_id: str
    role: str


class InviteInfoResponse(BaseModel):
    email: str
    name: Optional[str] = None
    entity_name: str


@router.post("/staff/invite", response_model=InviteResponse, status_code=201)
async def invite_staff(
    req: InviteRequest,
    _gate=Depends(require_plan("users")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a pending Staff row, send the invite email, and notify inviter."""
    await _require_permission(db, user, Permission.USERS_INVITE)

    invite_email = (req.email or "").strip().lower()
    if not invite_email:
        raise HTTPException(400, "Email is required.")

    # Prevent duplicate pending invites for the same email within the entity.
    existing = await db.execute(
        select(Staff).where(
            Staff.entity_id == user.entity_id,
            func.lower(Staff.email) == invite_email,
            Staff.deleted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A staff member with that email already exists.")

    # Validate role (if provided)
    if req.role_id:
        await _load_role(db, req.role_id, user.entity_id)

    workspace_ids = list(dict.fromkeys(req.workspace_ids or []))
    if workspace_ids:
        valid_workspace_ids = set((await db.execute(
            select(Workspace.id).where(
                Workspace.id.in_(workspace_ids),
                Workspace.entity_id == user.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all())
        if any(ws_id not in valid_workspace_ids for ws_id in workspace_ids):
            raise HTTPException(404, "Workspace not found")

    token = secrets.token_urlsafe(32)

    staff = Staff(
        id=generate_ulid(),
        entity_id=user.entity_id,
        kind="employee",
        name=req.name or invite_email.split("@")[0],
        email=invite_email,
        role_id=req.role_id,
        status="invited",
        meta={"invite_token": token, "workspace_ids": workspace_ids},
    )
    db.add(staff)
    await db.flush()

    invite_url = _build_invite_url(token, invite_email)
    entity = await db.get(Entity, user.entity_id)
    entity_name = entity.name if entity and entity.name else "your team"
    inviter_name = user.display_name or user.email or "A Manor AI admin"
    email_sent = await _send_staff_invite_email_best_effort(
        to=invite_email,
        entity_name=entity_name,
        inviter_name=inviter_name,
        invite_url=invite_url,
    )
    await _notify_staff_invite_sent(
        db=db,
        entity_id=user.entity_id,
        user_id=user.id,
        invitee_email=invite_email,
        invite_url=invite_url,
        email_sent=email_sent,
    )
    invitee_user = await _find_user_by_email(db, invite_email)
    if invitee_user and invitee_user.id != user.id:
        await _notify_staff_invite_received(
            db=db,
            invitee_user=invitee_user,
            entity_id=user.entity_id,
            entity_name=entity_name,
            inviter_name=inviter_name,
            inviter_user_id=user.id,
            invite_token=token,
            invitee_email=invite_email,
            invite_url=invite_url,
        )

    return InviteResponse(
        staff_id=staff.id,
        invite_token=token,
        invite_url=invite_url,
        email=invite_email,
        role_id=req.role_id,
        status=staff.status,
        email_sent=email_sent,
    )


# ── Accept invite ────────────────────────────────────────────────────────────

async def _find_pending_invite_staff(db: AsyncSession, token: str) -> Optional[Staff]:
    return await pending_staff_for_token(db, token)


@router.get("/auth/invite-info", response_model=InviteInfoResponse)
async def invite_info(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Return public, non-sensitive context for the regular login/register flow."""
    staff = await _find_pending_invite_staff(db, token)
    if staff is None:
        raise HTTPException(400, "Invalid or expired invite token.")
    if not staff.email:
        raise HTTPException(400, "Invite has no email on file; cannot accept.")

    entity = await db.get(Entity, staff.entity_id)
    return InviteInfoResponse(
        email=staff.email,
        name=staff.name,
        entity_name=entity.name if entity and entity.name else "your team",
    )


async def _redeem_staff_invite_for_user(
    db: AsyncSession,
    *,
    token: str,
    user: User,
    name: Optional[str] = None,
    phone: Optional[str] = None,
) -> AcceptInviteResponse:
    """Link a pending staff invite to an already-created account.

    Account creation and authentication intentionally happen elsewhere
    (/auth/register or /auth/login). The invite token only authorizes
    membership linkage for the email address it was sent to.
    """
    accepted = await accept_team_invite_for_user(
        db,
        token=token,
        user=user,
        name=name,
        phone=phone,
    )
    access_token = create_access_token(user.id, user.entity_id, user.role)

    return AcceptInviteResponse(
        access_token=access_token,
        user_id=user.id,
        entity_id=user.entity_id,
        staff_id=accepted.staff.id,
        role=user.role,
    )


@router.post("/auth/accept-invite", response_model=AcceptInviteResponse)
async def accept_invite(
    req: AcceptInviteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Consume an invite token for the currently authenticated account."""
    return await _redeem_staff_invite_for_user(
        db,
        token=req.token,
        user=user,
        name=req.name,
        phone=req.phone,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

_VALID_PERMISSION_KEYS = {p.value for p in Permission}


async def _require_permission(db: AsyncSession, user: User, permission: Permission) -> None:
    await check_effective_permission(
        db,
        user.id,
        user.entity_id,
        user.role,
        permission,
    )


def _build_invite_url(token: str, email: str | None = None) -> str:
    import os

    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    params = {"invite_token": token, "next": "/team"}
    if email:
        params["email"] = email
    return f"{app_url}/login?{urlencode(params)}"


def _build_invite_path(token: str, email: str | None = None) -> str:
    params = {"invite_token": token, "next": "/team"}
    if email:
        params["email"] = email
    return f"/login?{urlencode(params)}"


async def _find_user_by_email(db: AsyncSession, email: str | None) -> User | None:
    value = (email or "").strip().lower()
    if not value:
        return None
    return (
        await db.execute(
            select(User).where(
                func.lower(User.email) == value,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _send_staff_invite_email_best_effort(
    *, to: str, entity_name: str, inviter_name: str, invite_url: str,
) -> bool:
    try:
        from packages.core.services.email_service import send_staff_invite_email

        return bool(await send_staff_invite_email(
            to=to,
            entity_name=entity_name,
            inviter_name=inviter_name,
            invite_url=invite_url,
        ))
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "staff invite email failed for %s", to, exc_info=True,
        )
        return False


async def _notify_staff_invite_sent(
    *,
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    invitee_email: str,
    invite_url: str,
    email_sent: bool,
) -> None:
    body = (
        f"Invitation email sent to {invitee_email}."
        if email_sent
        else f"Invite created for {invitee_email}, but the email was not sent. Copy the invite link and send it manually."
    )
    try:
        from packages.core.services.notification_service import create_notification

        await create_notification(
            db,
            entity_id,
            user_id,
            "team_invite_sent",
            "Team invite created",
            body=body,
            link="/team",
            meta={
                "invitee_email": invitee_email,
                "invite_url": invite_url,
                "email_sent": email_sent,
            },
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "staff invite notification failed for %s", invitee_email, exc_info=True,
        )


async def _notify_staff_invite_received(
    *,
    db: AsyncSession,
    invitee_user: User,
    entity_id: str,
    entity_name: str,
    inviter_name: str,
    inviter_user_id: str,
    invite_token: str,
    invitee_email: str,
    invite_url: str,
) -> None:
    """Notify an existing Manor account that a company invite is waiting.

    This deliberately does not create or activate a company membership. The
    account is linked only when the invitee accepts the token.
    """
    try:
        from packages.core.services.notification_service import create_notification

        await create_notification(
            db,
            invitee_user.entity_id,
            invitee_user.id,
            "team_invite_received",
            f"Invitation to join {entity_name}",
            body=f"{inviter_name} invited you to join {entity_name}.",
            link=_build_invite_path(invite_token, invitee_email),
            meta={
                "entity_id": entity_id,
                "entity_name": entity_name,
                "inviter_name": inviter_name,
                "inviter_user_id": inviter_user_id,
                "invite_token": invite_token,
                "invitee_email": invitee_email,
                "invite_url": invite_url,
            },
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "staff invite recipient notification failed for %s",
            invitee_email,
            exc_info=True,
        )


def _validate_permission_keys(keys: list[str]) -> None:
    unknown = [k for k in keys if k not in _VALID_PERMISSION_KEYS]
    if unknown:
        raise HTTPException(
            400, f"Unknown permission keys: {', '.join(unknown)}"
        )


async def _load_role(db: AsyncSession, role_id: str, entity_id: str) -> StaffRole:
    result = await db.execute(
        select(StaffRole).where(
            StaffRole.id == role_id, StaffRole.entity_id == entity_id
        )
    )
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(404, "Role not found.")
    return role


async def _seed_default_roles_for_entity(
    db: AsyncSession, entity_id: str
) -> list[StaffRole]:
    """Seed the 4 system roles for a fresh entity using permissions.py defaults."""
    from packages.core.permissions import _get_role_permissions

    seeded: list[StaffRole] = []
    for name in ("viewer", "member", "admin", "owner"):
        perms = sorted(p.value for p in _get_role_permissions(name))
        role = StaffRole(
            id=generate_ulid(),
            entity_id=entity_id,
            name=name,
            permissions=perms,
            is_default=(name == "member"),
            status="active",
        )
        db.add(role)
        seeded.append(role)
    await db.flush()
    return seeded
