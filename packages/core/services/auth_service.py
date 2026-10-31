"""Authentication service — register, login, JWT, password hashing."""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import bcrypt
import jwt
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.constants.plans import DEFAULT_PLAN_ID
from packages.core.i18n import SUPPORTED_LOCALES, get_locale
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services.user_avatars import real_avatar_url

logger = logging.getLogger(__name__)
settings = get_settings()

_COMMON_PASSWORDS = frozenset({
    "123456789012",
    "adminadminadmin",
    "letmeinletmein",
    "password1234",
    "qwertyqwerty",
    "welcome12345",
    "manor-demo",
})


def password_policy_enabled() -> bool:
    if settings.DEPLOYMENT_MODE.strip().lower() == "cloud":
        return True
    configured = os.getenv("PASSWORD_POLICY_ENFORCED")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return False


def validate_password_strength(password: str) -> None:
    """Enforce the production password baseline without logging the value."""
    if not password_policy_enabled():
        return
    minimum = max(12, int(os.getenv("PASSWORD_MIN_LENGTH", "12")))
    if len(password or "") < minimum:
        raise ValueError(f"Password must be at least {minimum} characters")
    if len(password.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    normalized = password.strip().lower()
    if normalized in _COMMON_PASSWORDS or normalized in {
        "password" * 2,
        "qwerty" * 2,
        "123456" * 2,
    }:
        raise ValueError("Choose a password that is not commonly breached")


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

_DEFAULT_EXPIRE = min(max(1, int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))), 1440)
_JWT_ISSUER = os.getenv("JWT_ISSUER", "manor-api")
_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "manor-api")


def create_access_token(
    user_id: str,
    entity_id: str,
    role: str,
    remember: bool = False,
    *,
    token_version: int = 0,
    expires_minutes: int | None = None,
    mfa_authenticated: bool = False,
    audience: str | None = None,
    scopes: Sequence[str] | None = None,
    actor: str | None = None,
    authorized_party: str | None = None,
) -> str:
    """Create a server-revocable API access token, valid for at most 24 hours.

    ``scopes`` (``scp``) are what an OAuth client was granted; ``actor``
    (``act: {"sub": ...}``) names an agent acting for the user (an MCP client),
    so a route that tells people from agents can; ``authorized_party`` (``azp``)
    is the OAuth client the token was issued to.

    ``audience`` binds the token to one resource (e.g. the MCP endpoint). Such a
    token fails ``decode_token`` and therefore every REST route.

    ``remember`` is retained for client compatibility, but no longer creates a
    seven-day bearer token. Persistent browser login uses a separate rotating refresh
    session; access tokens are capped at 24 hours.
    """
    del remember
    minutes = min(max(1, expires_minutes or _DEFAULT_EXPIRE), 1440)
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=minutes)
    payload = {
        "sub": user_id,
        "entity_id": entity_id,
        "role": role,
        "jti": secrets.token_urlsafe(24),
        "iat": now,
        "iss": _JWT_ISSUER,
        "aud": audience or _JWT_AUDIENCE,
        "token_version": int(token_version or 0),
        "amr": ["mfa"] if mfa_authenticated else [],
        "exp": expire,
    }
    if scopes is not None:
        payload["scp"] = [str(scope) for scope in scopes]
    if actor:
        payload["act"] = {"sub": str(actor)}
    if authorized_party:
        payload["azp"] = str(authorized_party)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


COLLABORATOR_SESSION_TYPE = "collaborator_session"
COLLABORATOR_SESSION_KINDS = frozenset({"client_trial", "grant"})


def collaborator_session_minutes() -> int:
    """Lifetime of a scoped collaborator token: MANOR_COLLABORATOR_SESSION_MINUTES, 2..60, default 15."""
    try:
        minutes = int(os.getenv("MANOR_COLLABORATOR_SESSION_MINUTES", "15"))
    except ValueError:
        minutes = 15
    return min(max(2, minutes), 60)


def create_collaborator_session_token(
    *,
    user_id: str,
    entity_id: str,
    workspace_id: str,
    kind: str,
    ref: str,
    token_version: int = 0,
    mfa_authenticated: bool = False,
) -> tuple[str, datetime]:
    """A short-lived token for one customer Workspace, minted from the caller's home sign-in.

    ``cs`` pins the Workspace and names what authorized it (a client trial
    delivery or a collaboration grant). The person's persisted company is
    never changed; the token is held in memory by the embedded frame and
    re-minted before it expires. Returns the token and its expiry.
    """
    if kind not in COLLABORATOR_SESSION_KINDS:
        raise ValueError(f"unknown collaborator session kind: {kind!r}")
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=collaborator_session_minutes())
    payload = {
        "sub": user_id,
        "entity_id": entity_id,
        "role": "collaborator",
        "typ": COLLABORATOR_SESSION_TYPE,
        "cs": {"workspace_id": workspace_id, "kind": kind, "ref": ref},
        "jti": secrets.token_urlsafe(24),
        "iat": now,
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "token_version": int(token_version or 0),
        "amr": ["mfa"] if mfa_authenticated else [],
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM), expire




def decode_token(token: str, *, audience: str | None = None) -> Optional[dict]:
    """Decode a Manor access token for the REST API or, with ``audience``, one resource."""
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=_JWT_ISSUER,
            audience=audience or _JWT_AUDIENCE,
        )
    except jwt.PyJWTError:
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

# ── Registration ──

def _default_entity_settings() -> dict:
    from packages.core.governance.approval_policy_service import account_defaults
    return {"plan": DEFAULT_PLAN_ID, "approval_defaults": account_defaults()}


# "collaborator" is derived from a Workspace collaboration grant and carries no company permissions.
COLLABORATOR_ROLE = "collaborator"
COLLABORATOR_NOT_SWITCHABLE = {
    "code": "collaborator_membership_not_switchable",
    "message": "Open the customer Workspace from your own company",
}
_MEMBERSHIP_ROLES = {"owner", "admin", "member", "viewer", "external", "client", "collaborator"}


def require_assignable_role(role: str | None) -> str | None:
    """Refuse the derived collaborator role at a role input (a ValueError is a 422 in request models).

    Collaborator memberships are created and ended only by the Workspace
    collaboration sync (external_collaboration_access.sync_external_access);
    nobody assigns the role.
    """
    if (role or "").strip().lower() == "collaborator":
        raise ValueError("The collaborator role comes only from a Workspace collaboration and cannot be assigned.")
    return role


def normalize_membership_role(role: str | None, default: str = "member") -> str:
    value = (role or "").strip().lower()
    return value if value in _MEMBERSHIP_ROLES else default


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
) -> UserMembership | None:
    """Create or update the user's membership in an entity."""
    role_value = normalize_membership_role(role or user.role)
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
    if role_value == "collaborator":
        # Derived from a collaboration grant only (external_collaboration_access);
        # a login replaying a switched-in User.role must not create, reactivate
        # or make it primary.
        return membership
    membership_created = membership is None
    if membership_created:
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
        from packages.core.services.workspace_access import set_link_columns

        membership.role = role_value
        membership.status = status_value
        # Linking Staff or marking primary (workspace creation, login) is not an
        # authority change; it must not void grants this person issued.
        await set_link_columns(
            db,
            membership,
            staff_id=staff_id or membership.staff_id,
            is_primary=bool(membership.is_primary or is_primary),
        )
    await db.flush()
    if membership_created:
        from packages.core.governance.approval_policy_service import initialize_personal_approval
        await initialize_personal_approval(db, user_id=user.id, entity_id=entity_id)
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
    """Read one persisted membership without repairing authorization state."""
    stmt = select(UserMembership).where(
        UserMembership.user_id == user.id,
        UserMembership.entity_id == entity_id,
        UserMembership.deleted_at.is_(None),
    )
    if not include_inactive:
        stmt = stmt.where(UserMembership.status == "active")
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_user_memberships(
    db: AsyncSession,
    user: User,
    *,
    include_collaborator: bool = True,
) -> list[tuple[UserMembership, Entity | None]]:
    """List persisted memberships without creating or reactivating rows.

    ``include_collaborator=False`` leaves out memberships derived from a
    Workspace collaboration: they are not companies the person works in or
    can switch to.
    """
    query = (
        select(UserMembership, Entity)
        .join(Entity, Entity.id == UserMembership.entity_id, isouter=True)
        .where(
            UserMembership.user_id == user.id,
            UserMembership.status.in_(("active", "invited")),
            UserMembership.deleted_at.is_(None),
        )
        .order_by(UserMembership.is_primary.desc(), Entity.name.asc().nulls_last())
    )
    if not include_collaborator:
        query = query.where(UserMembership.role != COLLABORATOR_ROLE)
    return list((await db.execute(query)).all())


async def activate_user_membership(
    db: AsyncSession,
    *,
    user: User,
    membership: UserMembership,
) -> User:
    """Move the compatibility current-entity pointer to a membership.

    Never to a collaborator membership: a provider works in a customer
    Workspace through a scoped session from their own company.
    """
    if membership.role == COLLABORATOR_ROLE:
        raise ValueError("A collaborator membership is not a company to switch into")
    user.entity_id = membership.entity_id
    user.role = normalize_membership_role(membership.role)
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
    validate_password_strength(password)
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
    require_assignable_role(role)
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
        membership.role = normalize_membership_role(role)
        if user.entity_id == entity_id:
            user.role = membership.role
    else:
        user.role = normalize_membership_role(role)

    # A linked Staff row is the authority; moving only the membership would
    # leave the person's permissions unchanged.
    from packages.core.models.staff import Staff
    from packages.core.services.people_service import assign_staff_role

    staff_rows = (
        await db.execute(
            select(Staff).where(
                Staff.user_id == user_id,
                Staff.entity_id == entity_id,
                Staff.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    for staff in staff_rows:
        await assign_staff_role(db, staff, role_name=normalize_membership_role(role))
    await db.flush()
    return user


async def change_password(db: AsyncSession, user_id: str, old_password: str, new_password: str) -> bool:
    validate_password_strength(new_password)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not verify_password(old_password, user.password_hash):
        return False
    user.password_hash = hash_password(new_password)
    user.token_version = int(user.token_version or 0) + 1
    await db.flush()
    return True


async def invite_user(
    db: AsyncSession, entity_id: str, email: str, role: str = "member",
) -> User:
    """Invite a new user to an existing entity."""
    import secrets
    require_assignable_role(role)
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
    locale: str | None = None,
    email_verified: bool = False,
) -> tuple[User, bool]:
    """Find or create a user from OAuth provider login.

    Provider sign-in admission and exact-address verification are separate.
    The request language initializes new users only; existing preferences persist.
    """
    from packages.core.models.user import OAuthAccount
    from packages.core.services.oauth_account_credentials import store_oauth_account_tokens
    from packages.core.services.user_email_proof import mark_email_verified

    # Check existing OAuth link
    result = await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    oauth = result.scalar_one_or_none()
    if oauth:
        store_oauth_account_tokens(
            oauth,
            access_token=access_token,
            refresh_token=refresh_token,
            requester_id=oauth.user_id or "oauth_login",
        )
        await db.flush()
        user_result = await db.execute(select(User).where(User.id == oauth.user_id))
        user = user_result.scalar_one()
        if email_verified is True:
            mark_email_verified(user, email)
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

    # An unverified Google/Apple email cannot claim an existing identity (including
    # its stored proof), or reserve a new identity for another person's address.
    # Existing provider+subject links above do not depend on a new email claim.
    # Enterprise SAML uses its existing, separately configured IdP trust contract.
    if provider in {"google", "apple"} and email_verified is not True:
        raise ValueError("provider_email_verification_required")

    # Check if user with this email exists — link OAuth to existing account
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        # Ensure existing user is active (e.g. was pending email verification)
        if user.status == "pending":
            user.status = "active"
        if email_verified is True:
            mark_email_verified(user, email)
        # A real photo replaces nothing-or-the-old-generated-placeholder only.
        if avatar_url and not real_avatar_url(user.avatar_url):
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
        )
        store_oauth_account_tokens(
            oauth,
            access_token=access_token,
            refresh_token=refresh_token,
            preserve_existing_refresh=False,
            requester_id=user.id,
        )
        db.add(oauth)
        await db.flush()
        return user, False

    # Create new entity + user + oauth
    initial_locale = (locale or get_locale()).strip().lower().replace("_", "-").split("-", 1)[0]
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
        locale=initial_locale if initial_locale in SUPPORTED_LOCALES else "en",
        avatar_url=avatar_url or None,
        role="owner",
        status="active",
    )
    db.add(user)
    if email_verified is True:
        mark_email_verified(user, email)
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
    )
    store_oauth_account_tokens(
        oauth,
        access_token=access_token,
        refresh_token=refresh_token,
        preserve_existing_refresh=False,
        requester_id=user.id,
    )
    db.add(oauth)
    await db.flush()

    return user, True
