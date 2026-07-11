"""Authentication service — register, login, JWT, password hashing."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.constants.plans import DEFAULT_PLAN_ID
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Password hashing ──

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    # Some accounts (e.g. OAuth-only) may have non-bcrypt placeholders.
    # Treat malformed hashes as auth failure instead of raising 500s.
    if not hashed or not isinstance(hashed, str):
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        logger.warning("Invalid password hash format encountered during verification")
        return False


# ── JWT ──

_DEFAULT_EXPIRE = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 24h
_REMEMBER_EXPIRE = int(os.getenv("JWT_REMEMBER_MINUTES", "10080"))  # 7 days


def create_access_token(user_id: str, entity_id: str, role: str, remember: bool = False) -> str:
    minutes = _REMEMBER_EXPIRE if remember else _DEFAULT_EXPIRE
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    payload = {
        "sub": user_id,
        "entity_id": entity_id,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)




def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


def mark_user_login(user: User, *, source: str = "auth_service") -> None:
    """Record a completed login after all auth gates have passed."""
    user.last_login_at = datetime.now(timezone.utc)

    if source == "auth.register":
        return

    from packages.core.services.event_emitter import emit
    emit(user.entity_id, "user.login", source=source,
         payload={
             "user_id": user.id,
             "email": user.email,
             "username": user.display_name or user.email.split("@")[0],
         })


# ── Avatar ──

def _generate_avatar_url(name: str) -> str:
    """Generate a deterministic avatar URL from a display name using DiceBear API."""
    import hashlib
    seed = hashlib.md5(name.encode()).hexdigest()[:8]
    # DiceBear initials style — generates SVG avatars server-side
    return f"https://api.dicebear.com/9.x/initials/svg?seed={seed}&backgroundColor=0f766e,14b8a6,0d9488,0ea5e9,6366f1,8b5cf6&backgroundType=gradientLinear&fontSize=40"


# ── Registration ──

def _default_entity_settings() -> dict[str, str]:
    return {"plan": DEFAULT_PLAN_ID}


_MEMBERSHIP_ROLES = {"owner", "admin", "member", "viewer", "external", "client"}


def _normalize_membership_role(role: str | None, default: str = "member") -> str:
    value = (role or "").strip().lower()
    return value if value in _MEMBERSHIP_ROLES else default


async def _has_any_membership(db: AsyncSession, user_id: str) -> bool:
    return (
        await db.execute(
            select(UserMembership.id)
            .where(UserMembership.user_id == user_id)
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _has_primary_membership(db: AsyncSession, user_id: str) -> bool:
    return (
        await db.execute(
            select(UserMembership.id)
            .where(
                UserMembership.user_id == user_id,
                UserMembership.is_primary.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _membership_for_entity(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
) -> UserMembership | None:
    return (
        await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()


async def _role_for_staff_membership(db: AsyncSession, staff) -> str:
    from packages.core.permissions import legacy_role_for_staff_role, legacy_role_from_role_name

    meta = dict(staff.meta or {})
    legacy_default = legacy_role_from_role_name(meta.get("role"), default="member")
    return await legacy_role_for_staff_role(
        db,
        getattr(staff, "role_id", None),
        staff.entity_id,
        default=legacy_default,
    )


async def ensure_user_membership(
    db: AsyncSession,
    *,
    user: User,
    entity_id: str,
    role: str | None = None,
    status: str = "active",
    staff_id: str | None = None,
    is_primary: bool = False,
) -> UserMembership:
    """Create or update the user's membership in an entity."""
    role_value = _normalize_membership_role(role or user.role)
    status_value = (status or "active").strip().lower()
    if status_value not in {"active", "invited", "inactive"}:
        status_value = "active"

    membership = (
        await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == user.id,
                UserMembership.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        membership = UserMembership(
            id=generate_ulid(),
            user_id=user.id,
            entity_id=entity_id,
            role=role_value,
            status=status_value,
            staff_id=staff_id,
            is_primary=is_primary,
        )
        db.add(membership)
    else:
        membership.role = role_value
        membership.status = status_value
        if staff_id:
            membership.staff_id = staff_id
        membership.is_primary = bool(membership.is_primary or is_primary)
    await db.flush()
    return membership


async def reconcile_staff_memberships_for_user(
    db: AsyncSession,
    user: User,
) -> list[UserMembership]:
    """Backfill memberships from legacy active Staff.user_id links."""
    from packages.core.models.staff import Staff

    rows = (
        await db.execute(
            select(Staff).where(
                Staff.user_id == user.id,
                Staff.deleted_at.is_(None),
                Staff.status.in_(("active", "invited")),
            )
        )
    ).scalars().all()

    memberships: list[UserMembership] = []
    for staff in rows:
        role = await _role_for_staff_membership(db, staff)
        mark_primary = (
            user.entity_id == staff.entity_id
            and not await _has_primary_membership(db, user.id)
        )
        memberships.append(
            await ensure_user_membership(
                db,
                user=user,
                entity_id=staff.entity_id,
                role=role,
                status="active" if staff.status == "active" else "invited",
                staff_id=staff.id,
                is_primary=mark_primary,
            )
        )
    return memberships


async def get_user_membership(
    db: AsyncSession,
    *,
    user: User,
    entity_id: str,
    include_inactive: bool = False,
) -> UserMembership | None:
    if await _membership_for_entity(db, user_id=user.id, entity_id=user.entity_id) is None:
        mark_primary = not await _has_any_membership(db, user.id)
        await ensure_user_membership(
            db,
            user=user,
            entity_id=user.entity_id,
            role=user.role,
            status=user.status if user.status in {"active", "invited"} else "inactive",
            is_primary=mark_primary,
        )
    await reconcile_staff_memberships_for_user(db, user)
    stmt = select(UserMembership).where(
        UserMembership.user_id == user.id,
        UserMembership.entity_id == entity_id,
    )
    if not include_inactive:
        stmt = stmt.where(UserMembership.status == "active")
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_user_memberships(
    db: AsyncSession,
    user: User,
) -> list[tuple[UserMembership, Entity | None]]:
    if await _membership_for_entity(db, user_id=user.id, entity_id=user.entity_id) is None:
        mark_primary = not await _has_any_membership(db, user.id)
        await ensure_user_membership(
            db,
            user=user,
            entity_id=user.entity_id,
            role=user.role,
            status=user.status if user.status in {"active", "invited"} else "inactive",
            is_primary=mark_primary,
        )
    await reconcile_staff_memberships_for_user(db, user)

    rows = (
        await db.execute(
            select(UserMembership, Entity)
            .join(Entity, Entity.id == UserMembership.entity_id, isouter=True)
            .where(
                UserMembership.user_id == user.id,
                UserMembership.status.in_(("active", "invited")),
            )
            .order_by(UserMembership.is_primary.desc(), Entity.name.asc().nulls_last())
        )
    ).all()
    return list(rows)


async def activate_user_membership(
    db: AsyncSession,
    *,
    user: User,
    membership: UserMembership,
) -> User:
    """Move the compatibility current-entity pointer to a membership."""
    user.entity_id = membership.entity_id
    user.role = _normalize_membership_role(membership.role)
    await db.flush()
    return user


async def register_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    entity_name: str = "",
    display_name: str = "",
) -> tuple[User, Entity]:
    """Register a new user + create their entity."""
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise ValueError("Email already registered")

    name_hint = display_name or email.split("@")[0]
    entity = Entity(
        id=generate_ulid(),
        name=entity_name or f"{name_hint}'s Organization",
        plan_id=DEFAULT_PLAN_ID,
        settings=_default_entity_settings(),
    )
    db.add(entity)
    await db.flush()

    name_display = display_name or email.split("@")[0]
    user = User(
        id=generate_ulid(),
        entity_id=entity.id,
        email=email,
        display_name=name_display,
        password_hash=hash_password(password),
        avatar_url=_generate_avatar_url(name_display),
        role="owner",
    )
    db.add(user)
    await db.flush()

    await ensure_user_membership(
        db,
        user=user,
        entity_id=entity.id,
        role="owner",
        status="active",
        is_primary=True,
    )

    # Provision JuiceFS entity filesystem (MANOR.md, index.md, log.md, .ai/)
    from packages.core.services.entity_fs import is_fs_enabled, provision_entity_filesystem
    if is_fs_enabled():
        try:
            provision_entity_filesystem(entity.id, entity.name)
        except Exception as e:
            logger.warning("Failed to provision entity filesystem: %s", e)

    return user, entity


# ── Authentication ──

async def authenticate_user(
    db: AsyncSession, *, email: str = "", username: str = "", password: str
) -> Optional[User]:
    """Verify credentials by email or legacy username/display_name.

    Returns ``None`` for soft-deleted
    accounts — callers should fall through to a restore path before
    surfacing the generic "invalid credentials" error."""
    user = await get_user_by_login(db, email or username)
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


# ── User queries ──

async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    """Active (non-soft-deleted) user lookup. Soft-deleted users get a
    fresh 401 — they need to log in again to enter the restore flow."""
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def get_user_by_id_including_deleted(
    db: AsyncSession, user_id: str,
) -> Optional[User]:
    """Lookup that returns soft-deleted users too. Used by the restore
    flow and admin tools — never by request authentication."""
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    """Active user by email. The login flow uses this; soft-deleted
    users are not returned, which means a deleted account behaves like
    a non-existent one until restored."""
    result = await db.execute(
        select(User).where(User.email == email, User.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def get_user_by_login(
    db: AsyncSession,
    login: str,
    *,
    include_deleted: bool = False,
) -> Optional[User]:
    """Lookup by canonical email or the pre-entity-era ``username`` alias.

    The current schema stores the user-facing name in ``display_name``;
    older clients/tests still post ``username`` to login. Treat that as
    a compatibility alias without introducing a second identity column.
    """
    ident = (login or "").strip()
    if not ident:
        return None
    ident_email = ident.lower()
    clauses = [User.email == ident_email]
    if "@" not in ident:
        clauses.append(User.display_name == ident)
    stmt = select(User).where(or_(*clauses)).order_by(User.created_at.asc()).limit(1)
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_user_by_email_including_deleted(
    db: AsyncSession, email: str,
) -> Optional[User]:
    """Email lookup that surfaces soft-deleted users. Used by the
    "restore my account" entry point on the login page."""
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def list_users(db: AsyncSession, entity_id: str) -> list[User]:
    result = await db.execute(
        select(User)
        .join(UserMembership, UserMembership.user_id == User.id, isouter=True)
        .where(
            or_(
                User.entity_id == entity_id,
                UserMembership.entity_id == entity_id,
            ),
            User.deleted_at.is_(None),
            or_(UserMembership.status.is_(None), UserMembership.status != "inactive"),
        )
        .distinct()
        .order_by(User.created_at.asc())
    )
    return list(result.scalars().all())


async def get_user(db: AsyncSession, user_id: str, entity_id: str) -> Optional[User]:
    result = await db.execute(
        select(User)
        .join(UserMembership, UserMembership.user_id == User.id, isouter=True)
        .where(
            User.id == user_id,
            User.deleted_at.is_(None),
            or_(
                User.entity_id == entity_id,
                UserMembership.entity_id == entity_id,
            ),
            or_(UserMembership.status.is_(None), UserMembership.status != "inactive"),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_user_role(db: AsyncSession, user_id: str, entity_id: str, role: str) -> Optional[User]:
    user = await get_user(db, user_id, entity_id)
    if not user:
        return None
    membership = await get_user_membership(
        db,
        user=user,
        entity_id=entity_id,
        include_inactive=True,
    )
    if membership:
        membership.role = _normalize_membership_role(role)
        if user.entity_id == entity_id:
            user.role = membership.role
    else:
        user.role = _normalize_membership_role(role)
    await db.flush()
    return user


async def deactivate_user(db: AsyncSession, user_id: str, entity_id: str) -> bool:
    user = await get_user(db, user_id, entity_id)
    if not user:
        return False
    membership = await get_user_membership(
        db,
        user=user,
        entity_id=entity_id,
        include_inactive=True,
    )
    if membership:
        membership.status = "inactive"
    if user.entity_id == entity_id:
        user.status = "inactive"
    await db.flush()
    return True


async def change_password(db: AsyncSession, user_id: str, old_password: str, new_password: str) -> bool:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not verify_password(old_password, user.password_hash):
        return False
    user.password_hash = hash_password(new_password)
    await db.flush()
    return True


async def invite_user(
    db: AsyncSession, entity_id: str, email: str, role: str = "member",
) -> User:
    """Invite a new user to an existing entity."""
    import secrets
    temp_password = secrets.token_urlsafe(16)

    # Check if email already exists
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise ValueError("Email already registered")

    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(temp_password),
        avatar_url=_generate_avatar_url(email.split("@")[0]),
        role=role,
        status="invited",
    )
    db.add(user)
    await db.flush()
    await ensure_user_membership(
        db,
        user=user,
        entity_id=entity_id,
        role=role,
        status="invited",
        is_primary=True,
    )
    return user


# ── OAuth ──

async def get_or_create_oauth_user(
    db: AsyncSession, provider: str, provider_user_id: str,
    email: str, display_name: str = None,
    first_name: str = None, last_name: str = None,
    avatar_url: str = None,
    access_token: str = None, refresh_token: str = None,
) -> tuple[User, bool]:
    """Find or create a user from OAuth provider login.

    OAuth users are always 'active' (Google already verified their email).
    """
    from packages.core.models.user import OAuthAccount

    # Check existing OAuth link
    result = await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    oauth = result.scalar_one_or_none()
    if oauth:
        if access_token:
            oauth.access_token = access_token
        if refresh_token:
            oauth.refresh_token = refresh_token
        await db.flush()
        user_result = await db.execute(select(User).where(User.id == oauth.user_id))
        user = user_result.scalar_one()
        # Update avatar/name if changed on Google side
        if avatar_url and user.avatar_url != avatar_url:
            user.avatar_url = avatar_url
        if display_name and not user.display_name:
            user.display_name = display_name
        await ensure_user_membership(
            db,
            user=user,
            entity_id=user.entity_id,
            role=user.role,
            status="active" if user.status == "active" else user.status,
            is_primary=True,
        )
        await db.flush()
        return user, False

    # Check if user with this email exists — link OAuth to existing account
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        # Ensure existing user is active (e.g. was pending email verification)
        if user.status == "pending":
            user.status = "active"
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        if first_name and not user.first_name:
            user.first_name = first_name
        if last_name and not user.last_name:
            user.last_name = last_name
        await ensure_user_membership(
            db,
            user=user,
            entity_id=user.entity_id,
            role=user.role,
            status="active" if user.status == "active" else user.status,
            is_primary=True,
        )
        oauth = OAuthAccount(
            id=generate_ulid(), user_id=user.id,
            provider=provider, provider_user_id=provider_user_id,
            access_token=access_token, refresh_token=refresh_token,
        )
        db.add(oauth)
        await db.flush()
        return user, False

    # Create new entity + user + oauth
    name_hint = display_name or email.split("@")[0]
    entity = Entity(
        id=generate_ulid(),
        name=name_hint,
        plan_id=DEFAULT_PLAN_ID,
        settings=_default_entity_settings(),
    )
    db.add(entity)
    await db.flush()

    user = User(
        id=generate_ulid(), entity_id=entity.id,
        email=email, display_name=display_name or email.split("@")[0],
        first_name=first_name, last_name=last_name,
        password_hash="oauth_no_password",
        avatar_url=avatar_url or _generate_avatar_url(display_name or email.split("@")[0]),
        role="owner",
        status="active",  # Google already verified the email
    )
    db.add(user)
    await db.flush()
    await ensure_user_membership(
        db,
        user=user,
        entity_id=entity.id,
        role="owner",
        status="active",
        is_primary=True,
    )

    # Provision JuiceFS entity filesystem
    from packages.core.services.entity_fs import is_fs_enabled, provision_entity_filesystem
    if is_fs_enabled():
        try:
            provision_entity_filesystem(entity.id, entity.name)
        except Exception as e:
            logger.warning("Failed to provision entity filesystem: %s", e)

    oauth = OAuthAccount(
        id=generate_ulid(), user_id=user.id,
        provider=provider, provider_user_id=provider_user_id,
        access_token=access_token, refresh_token=refresh_token,
    )
    db.add(oauth)
    await db.flush()

    return user, True
