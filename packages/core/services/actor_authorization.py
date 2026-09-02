"""Read-only, current actor resolution for protected resource operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import Entity, User, UserMembership
from packages.core.permissions import Permission, has_permission


CredentialAdmissionValidator = Callable[
    [AsyncSession, "AuthenticatedUserCredential"],
    Awaitable[bool],
]


@dataclass(frozen=True)
class AuthenticatedUserCredential:
    """Identity facts verified at request admission, without cached authority."""

    user_id: str
    entity_id: str
    token_version: int | None
    expires_at_epoch: float | None = None
    impersonation_session_id: str | None = None
    admission_validator: CredentialAdmissionValidator | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_user(cls, user: User) -> "AuthenticatedUserCredential":
        admitted = getattr(user, "_authenticated_user_credential", None)
        if isinstance(admitted, cls) and admitted.user_id == str(user.id):
            return admitted
        return cls(
            user_id=str(user.id),
            entity_id=str(user.entity_id),
            token_version=int(getattr(user, "token_version", 0) or 0),
        )


@dataclass(frozen=True)
class ResolvedUserActor:
    """Current entity admission and role, valid only for one protected scope."""

    user_id: str
    entity_id: str
    display_role: str
    role: str | None
    can_read_documents: bool
    can_read_workspaces: bool


async def resolve_current_user_actor(
    db: AsyncSession,
    credential: AuthenticatedUserCredential,
    *,
    enforce_entity_suspension: bool = True,
) -> ResolvedUserActor | None:
    """Resolve current account and entity admission in one database snapshot.

    This query is deliberately read-only.  Authentication reads must never
    create/reactivate a Membership or rewrite the User's primary entity.  Staff
    history is authoritative when present, while rows without any explicit
    Membership/Staff retain the existing primary-entity compatibility path.
    """

    now = datetime.now(timezone.utc)
    if credential.expires_at_epoch is not None and now.timestamp() >= credential.expires_at_epoch:
        return None
    if credential.impersonation_session_id and credential.admission_validator is None:
        return None
    if credential.admission_validator is not None:
        if not await credential.admission_validator(db, credential):
            return None

    rows = list(
        (
            await db.execute(
                select(
                    User.id.label("user_id"),
                    User.entity_id.label("user_primary_entity_id"),
                    User.role.label("user_role"),
                    User.status.label("user_status"),
                    User.deleted_at.label("user_deleted_at"),
                    User.token_version.label("user_token_version"),
                    UserMembership.id.label("membership_id"),
                    UserMembership.role.label("membership_role"),
                    UserMembership.status.label("membership_status"),
                    UserMembership.deleted_at.label("membership_deleted_at"),
                    UserMembership.staff_id.label("membership_staff_id"),
                    Staff.id.label("staff_id"),
                    Staff.status.label("staff_status"),
                    Staff.deleted_at.label("staff_deleted_at"),
                    Staff.role_id.label("staff_role_id"),
                    Staff.meta.label("staff_meta"),
                    StaffRole.id.label("role_id"),
                    StaffRole.name.label("role_name"),
                    StaffRole.permissions.label("role_permissions"),
                    StaffRole.status.label("role_status"),
                    Entity.id.label("entity_id"),
                    Entity.deleted_at.label("entity_deleted_at"),
                    Entity.settings.label("entity_settings"),
                )
                .outerjoin(
                    UserMembership,
                    and_(
                        UserMembership.user_id == User.id,
                        UserMembership.entity_id == credential.entity_id,
                    ),
                )
                .outerjoin(
                    Staff,
                    and_(
                        Staff.user_id == User.id,
                        Staff.entity_id == credential.entity_id,
                    ),
                )
                .outerjoin(
                    StaffRole,
                    and_(
                        StaffRole.id == Staff.role_id,
                        StaffRole.entity_id == credential.entity_id,
                    ),
                )
                .outerjoin(Entity, Entity.id == credential.entity_id)
                .where(User.id == credential.user_id)
            )
        ).mappings().all()
    )
    if not rows:
        return None

    user = rows[0]
    if (
        user["user_status"] != "active"
        or user["user_deleted_at"] is not None
        or (
            credential.token_version is not None
            and int(user["user_token_version"] or 0) != credential.token_version
        )
    ):
        return None

    if (
        user["entity_id"] is None
        or user["entity_deleted_at"] is not None
        or (
            enforce_entity_suspension
            and (user["entity_settings"] or {}).get("platform_suspended_at")
        )
    ):
        return None

    memberships = {
        row["membership_id"]: row
        for row in rows
        if row["membership_id"] is not None
    }
    if memberships:
        active_memberships = [
            row
            for row in memberships.values()
            if row["membership_status"] == "active" and row["membership_deleted_at"] is None
        ]
        if len(active_memberships) != 1:
            return None
        membership_role = str(active_memberships[0]["membership_role"] or "").strip().lower() or None
    else:
        membership_role = None

    staff_rows = {
        row["staff_id"]: row
        for row in rows
        if row["staff_id"] is not None
    }
    staff = None
    if staff_rows:
        active_staff = [
            row
            for row in staff_rows.values()
            if row["staff_status"] == "active" and row["staff_deleted_at"] is None
        ]
        if len(active_staff) != 1:
            return None
        staff = active_staff[0]
        if memberships:
            membership_staff_id = active_memberships[0]["membership_staff_id"]
            if membership_staff_id and str(membership_staff_id) != str(staff["staff_id"]):
                return None
        if staff["staff_role_id"]:
            current_role = (
                str(staff["role_name"] or "").strip().lower()
                if staff["role_id"] is not None and staff["role_status"] == "active"
                else None
            )
        else:
            current_role = str((staff["staff_meta"] or {}).get("role") or "").strip().lower() or None
    else:
        current_role = membership_role

    if not memberships and not staff_rows:
        if str(user["user_primary_entity_id"]) != credential.entity_id:
            return None
        current_role = str(user["user_role"] or "").strip().lower() or None

    if staff is not None and staff["staff_role_id"]:
        if staff["role_id"] is not None and staff["role_status"] == "active":
            role_permission_values = staff["role_permissions"] or []
        else:
            role_permission_values = []
        permission_keys = {str(permission) for permission in role_permission_values}
        can_read_documents = Permission.DOCS_READ.value in permission_keys
        can_read_workspaces = Permission.WORKSPACES_READ.value in permission_keys
    else:
        can_read_documents = bool(current_role and has_permission(current_role, Permission.DOCS_READ))
        can_read_workspaces = bool(current_role and has_permission(current_role, Permission.WORKSPACES_READ))

    # ``User.role`` remains a compatibility/display field in API responses.
    # A custom StaffRole may carry the real authorization role while the
    # Membership still exposes the stable built-in role expected by clients.
    display_role = (
        membership_role
        or (
            str(user["user_role"] or "").strip().lower()
            if str(user["user_primary_entity_id"]) == credential.entity_id
            else ""
        )
        or current_role
        or ""
    )

    return ResolvedUserActor(
        user_id=credential.user_id,
        entity_id=credential.entity_id,
        display_role=display_role,
        role=current_role,
        can_read_documents=can_read_documents,
        can_read_workspaces=can_read_workspaces,
    )
