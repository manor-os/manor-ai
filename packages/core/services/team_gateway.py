"""Team gateway helpers.

This module is the entity-scoped doorway for team-member reads.  Callers that
need per-member company data should resolve members here first, then hand the
allowed member set to a domain-specific gateway such as usage.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import User, UserMembership
from packages.core.permissions import (
    Permission,
    user_has_effective_permission,
)


@dataclass(frozen=True)
class TeamGatewayMember:
    staff_id: str
    entity_id: str
    user_id: str | None
    membership_status: str | None
    kind: str
    status: str
    name: str
    email: str | None
    avatar_url: str | None
    title: str | None
    role_id: str | None
    role_name: str | None


async def can_view_team_usage(db: AsyncSession, user: User) -> bool:
    """Return whether ``user`` can view company-wide team usage/activity."""

    for permission in (
        Permission.ADMIN_AUDIT,
        Permission.ADMIN_BILLING,
        Permission.USERS_MANAGE,
    ):
        if await user_has_effective_permission(
            db,
            user.id,
            user.entity_id,
            user.role,
            permission,
        ):
            return True
    return False


async def require_team_usage_access(db: AsyncSession, user: User) -> None:
    if not await can_view_team_usage(db, user):
        raise HTTPException(403, "Permission denied: team usage requires owner/admin access")


async def list_team_gateway_members(
    db: AsyncSession,
    *,
    entity_id: str,
) -> list[TeamGatewayMember]:
    """List entity staff with linked login and role metadata.

    The gateway intentionally does not resolve unlinked staff by email.  A
    user's company usage is visible only after the invite/account flow creates
    a durable Staff.user_id/UserMembership relationship.
    """

    rows = (
        await db.execute(
            select(Staff, User, UserMembership, StaffRole)
            .outerjoin(User, User.id == Staff.user_id)
            .outerjoin(
                UserMembership,
                (UserMembership.user_id == Staff.user_id)
                & (UserMembership.entity_id == Staff.entity_id),
            )
            .outerjoin(
                StaffRole,
                (StaffRole.id == Staff.role_id)
                & (StaffRole.entity_id == Staff.entity_id),
            )
            .where(
                Staff.entity_id == entity_id,
                Staff.deleted_at.is_(None),
            )
            .order_by(Staff.created_at.desc())
        )
    ).all()

    members: list[TeamGatewayMember] = []
    for staff, linked_user, membership, role in rows:
        display_name = (
            (linked_user.display_name if linked_user else None)
            or staff.name
            or (linked_user.email.split("@")[0] if linked_user and linked_user.email else None)
            or staff.email
            or "Team member"
        )
        display_email = staff.email or (linked_user.email if linked_user else None)
        avatar_url = (linked_user.avatar_url if linked_user else None) or staff.avatar_url
        members.append(
            TeamGatewayMember(
                staff_id=staff.id,
                entity_id=staff.entity_id,
                user_id=staff.user_id,
                membership_status=membership.status if membership else None,
                kind=staff.kind or "employee",
                status=staff.status or "active",
                name=display_name,
                email=display_email,
                avatar_url=avatar_url,
                title=staff.title,
                role_id=staff.role_id,
                role_name=role.name if role else None,
            )
        )
    return members
