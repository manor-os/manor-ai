"""Shared helpers for accepting staff invitation tokens."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import OAuthAccount, User
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.auth_service import (
    activate_user_membership,
    ensure_user_membership,
    hash_password,
    verify_password,
)


_SYSTEM_ROLE_NAMES = {"owner", "admin", "member", "viewer"}


@dataclass
class TeamInviteAcceptResult:
    user: User
    staff: Staff


def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


async def _pending_staff_for_token(db: AsyncSession, token: str) -> Staff:
    candidate = await db.execute(
        select(Staff).where(
            Staff.status == "invited",
            Staff.deleted_at.is_(None),
        )
    )
    for row in candidate.scalars():
        if (row.meta or {}).get("invite_token") == token:
            return row
    raise HTTPException(400, "Invalid or expired invite token.")


async def pending_staff_for_token(db: AsyncSession, token: str) -> Staff | None:
    """Return the pending staff invite for a token without raising."""
    if not token:
        return None
    try:
        return await _pending_staff_for_token(db, token)
    except HTTPException:
        return None


async def _load_invited_staff(
    db: AsyncSession,
    *,
    token: str,
    email: str | None,
) -> Staff:
    staff = await _pending_staff_for_token(db, token)
    if not staff.email:
        raise HTTPException(400, "Invite has no email on file; cannot accept.")
    if email and _normalize_email(email) != _normalize_email(staff.email):
        raise HTTPException(400, "Invite email does not match.")
    return staff


async def _legacy_role_for_staff(db: AsyncSession, staff: Staff) -> str:
    role_name = "member"
    if staff.role_id:
        role_row = await db.get(StaffRole, staff.role_id)
        if role_row and role_row.entity_id == staff.entity_id:
            role_name = (role_row.name or "member").lower()
            if role_name not in _SYSTEM_ROLE_NAMES:
                role_name = "member"
    return role_name


async def _user_for_staff_email(db: AsyncSession, staff: Staff) -> User | None:
    email = _normalize_email(staff.email)
    result = await db.execute(
        select(User).where(
            func.lower(User.email) == email,
            User.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _activate_staff_membership(
    db: AsyncSession,
    *,
    staff: Staff,
    user: User,
    name: str | None = None,
    phone: str | None = None,
    avatar_url: str | None = None,
) -> None:
    staff.user_id = user.id
    staff.status = "active"
    if name:
        staff.name = name
    if phone:
        staff.phone = phone
    if avatar_url and not staff.avatar_url:
        staff.avatar_url = avatar_url

    meta = dict(staff.meta or {})
    workspace_ids = meta.pop("workspace_ids", []) or []
    if not isinstance(workspace_ids, list):
        workspace_ids = []
    meta.pop("invite_token", None)
    staff.meta = meta

    valid_workspace_ids: set[str] = set()
    if workspace_ids:
        valid_workspace_ids = set((await db.execute(
            select(Workspace.id).where(
                Workspace.id.in_(workspace_ids),
                Workspace.entity_id == staff.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all())

    for ws_id in workspace_ids:
        if ws_id not in valid_workspace_ids:
            continue
        existing = (
            await db.execute(
                select(WorkspaceStaff).where(
                    WorkspaceStaff.workspace_id == ws_id,
                    WorkspaceStaff.staff_id == staff.id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.user_id = user.id
            existing.status = "active"
            continue
        db.add(WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=ws_id,
            staff_id=staff.id,
            user_id=user.id,
            role=None,
            status="active",
        ))

    role = await _legacy_role_for_staff(db, staff)
    membership = await ensure_user_membership(
        db,
        user=user,
        entity_id=staff.entity_id,
        role=role,
        status="active",
        staff_id=staff.id,
        is_primary=(user.entity_id == staff.entity_id),
    )
    await activate_user_membership(db, user=user, membership=membership)


async def accept_team_invite_for_user(
    db: AsyncSession,
    *,
    token: str,
    user: User,
    name: str | None = None,
    phone: str | None = None,
) -> TeamInviteAcceptResult:
    """Consume a pending staff invite for an already-authenticated user."""
    staff = await _load_invited_staff(db, token=token, email=user.email)
    if staff.user_id and staff.user_id != user.id:
        raise HTTPException(409, "Invite has already been accepted by another account.")

    await _activate_staff_membership(
        db,
        staff=staff,
        user=user,
        name=name,
        phone=phone,
    )
    await db.flush()
    return TeamInviteAcceptResult(user=user, staff=staff)


async def decline_team_invite_for_user(
    db: AsyncSession,
    *,
    token: str,
    user: User,
) -> Staff:
    """Mark a pending invite as declined for the authenticated invitee."""
    staff = await _load_invited_staff(db, token=token, email=user.email)
    if staff.user_id and staff.user_id != user.id:
        raise HTTPException(409, "Invite has already been accepted by another account.")
    meta = dict(staff.meta or {})
    meta.pop("invite_token", None)
    staff.meta = meta
    staff.status = "declined"
    await db.flush()
    return staff


async def accept_team_invite_with_password(
    db: AsyncSession,
    *,
    token: str,
    password: str,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> TeamInviteAcceptResult:
    staff = await _load_invited_staff(db, token=token, email=email)
    user = await _user_for_staff_email(db, staff)

    if user is None:
        user = User(
            id=generate_ulid(),
            entity_id=staff.entity_id,
            email=staff.email,
            password_hash=hash_password(password),
            role=await _legacy_role_for_staff(db, staff),
            display_name=name or staff.name,
            status="active",
        )
        db.add(user)
        await db.flush()
    elif user.status == "invited":
        user.password_hash = hash_password(password)
        user.status = "active"
        if name:
            user.display_name = name
        await db.flush()
    elif not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid email or password.")

    await _activate_staff_membership(
        db,
        staff=staff,
        user=user,
        name=name,
        phone=phone,
    )
    await db.flush()
    return TeamInviteAcceptResult(user=user, staff=staff)


async def _upsert_oauth_account(
    db: AsyncSession,
    *,
    user: User,
    provider: str,
    provider_user_id: str,
    access_token: str | None,
    refresh_token: str | None,
) -> None:
    oauth = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )).scalar_one_or_none()
    if oauth:
        if oauth.user_id != user.id:
            raise HTTPException(409, "Google account is already linked to another user.")
        if access_token:
            oauth.access_token = access_token
        if refresh_token:
            oauth.refresh_token = refresh_token
        await db.flush()
        return

    db.add(OAuthAccount(
        id=generate_ulid(),
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        access_token=access_token,
        refresh_token=refresh_token,
    ))
    await db.flush()


async def accept_team_invite_with_oauth(
    db: AsyncSession,
    *,
    token: str,
    provider: str,
    provider_user_id: str,
    email: str,
    display_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    avatar_url: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
) -> TeamInviteAcceptResult:
    staff = await _load_invited_staff(db, token=token, email=email)
    user = await _user_for_staff_email(db, staff)
    name_hint = display_name or staff.name or staff.email.split("@")[0]

    if user is not None:
        if user.status in {"pending", "invited"}:
            user.status = "active"
        if display_name and not user.display_name:
            user.display_name = display_name
        if first_name and not user.first_name:
            user.first_name = first_name
        if last_name and not user.last_name:
            user.last_name = last_name
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        await _upsert_oauth_account(
            db,
            user=user,
            provider=provider,
            provider_user_id=provider_user_id,
            access_token=access_token,
            refresh_token=refresh_token,
        )
        await _activate_staff_membership(
            db,
            staff=staff,
            user=user,
            name=display_name,
            avatar_url=avatar_url,
        )
        await db.flush()
        return TeamInviteAcceptResult(user=user, staff=staff)

    user = User(
        id=generate_ulid(),
        entity_id=staff.entity_id,
        email=staff.email,
        display_name=name_hint,
        first_name=first_name,
        last_name=last_name,
        avatar_url=avatar_url,
        password_hash="oauth_no_password",
        role=await _legacy_role_for_staff(db, staff),
        status="active",
    )
    db.add(user)
    await db.flush()

    await _upsert_oauth_account(
        db,
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        access_token=access_token,
        refresh_token=refresh_token,
    )
    await _activate_staff_membership(
        db,
        staff=staff,
        user=user,
        name=display_name,
        avatar_url=avatar_url,
    )
    await db.flush()
    return TeamInviteAcceptResult(user=user, staff=staff)
