"""Who may change whose team access.

Owner and admin are protected roles (``PROTECTED_STAFF_ROLE_NAMES``): only an
owner may grant one, or act on a person who holds one. People may always act
on themselves (e.g. step down). Every endpoint that writes a role or acts on a
team member calls these guards after its own permission check, so the rule
cannot be bypassed through an alternate route.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import User, UserMembership
from packages.core.permissions import (
    is_protected_staff_role_name,
    staff_is_protected_management_target,
    user_is_effective_owner,
)

OWNER_ONLY = "Only an owner can grant or manage owner and admin access."


async def require_can_grant_role(
    db: AsyncSession,
    actor: User,
    *,
    role_id: str | None = None,
    role_name: str | None = None,
) -> None:
    """Granting owner/admin (by StaffRole id or legacy role name) needs an owner."""
    names = [role_name] if role_name else []
    if role_id:
        role = await db.get(StaffRole, role_id)
        if role is not None and role.entity_id == actor.entity_id:
            names.append(role.name)
    if any(is_protected_staff_role_name(name) for name in names) and not await user_is_effective_owner(db, actor):
        raise HTTPException(403, OWNER_ONLY)


async def _live_staff_for_user(db: AsyncSession, *, user_id: str, entity_id: str) -> Staff | None:
    return (await db.execute(
        select(Staff)
        .where(Staff.user_id == user_id, Staff.entity_id == entity_id, Staff.deleted_at.is_(None))
        .order_by(Staff.status != "active", Staff.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def require_can_manage_member(
    db: AsyncSession,
    actor: User,
    *,
    staff: Staff | None = None,
    user_id: str | None = None,
) -> None:
    """Acting on someone else who holds owner/admin needs an owner."""
    target_user_id = (staff.user_id if staff is not None else None) or user_id
    if target_user_id and target_user_id == actor.id:
        return
    if staff is None and user_id:
        staff = await _live_staff_for_user(db, user_id=user_id, entity_id=actor.entity_id)
    if staff is not None:
        protected = await staff_is_protected_management_target(db, staff)
    elif user_id:
        role = (await db.execute(
            select(UserMembership.role).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == actor.entity_id,
                UserMembership.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        protected = is_protected_staff_role_name(role)
    else:
        protected = False
    if protected and not await user_is_effective_owner(db, actor):
        raise HTTPException(403, OWNER_ONLY)


async def require_resettable_account(db: AsyncSession, actor: User, *, user_id: str) -> None:
    """Another person's password may be reset only if this is their sole company.

    A login is global: resetting it from here would hand over every other
    company the account belongs to.
    """
    if user_id == actor.id:
        return
    elsewhere = (await db.execute(
        select(UserMembership.id).where(
            UserMembership.user_id == user_id,
            UserMembership.entity_id != actor.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if elsewhere is not None:
        raise HTTPException(
            409,
            "This account also belongs to another company; ask them to reset "
            "their password from the sign-in page.",
        )
