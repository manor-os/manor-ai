"""Authentication endpoints — register, login, current user, OAuth, user management."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, OAuthAccount, User, UserMembership
from packages.core.services.auth_service import (
    authenticate_user,
    change_password,
    create_access_token,
    deactivate_user,
    activate_user_membership,
    get_or_create_oauth_user,
    get_user_by_login,
    get_user_membership,
    hash_password,
    invite_user,
    list_user_memberships,
    list_users,
    mark_user_login,
    register_user,
    update_user_role,
)
from packages.core.services.totp_service import (
    disable_2fa,
    setup_2fa,
    verify_2fa_login,
    verify_and_enable_2fa,
)
from apps.api.deps import get_current_user, require_plan
from packages.core.i18n import SUPPORTED_LOCALES, get_locale
from packages.core.permissions import (
    _get_role_permissions,
    legacy_role_for_staff_role,
    user_effective_permission_keys,
    user_staff_role_summary,
)
from packages.core.services.captcha_service import CAPTCHA_ENABLED, verify_captcha
from packages.core.services.email_verification_service import (
    create_verification,
    resend_verification,
    verify_email,
)
from packages.core.services.email_service import send_verification_email
from packages.core.services.settings_service import update_user_preferences
from packages.core.config import get_settings

logger = logging.getLogger(__name__)

# Cloud-only auth and hosted-provider conveniences are stripped from OSS export.
_CLOUD_FEATURES_ENABLED = False

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── Schemas ──

class RegisterRequest(BaseModel):
    username: str | None = None
    email: str
    password: str
    entity_name: str = ""  # company/organization name
    captcha_token: str | None = None
    invitation_code: str | None = None
    invite_token: str | None = None
    """Required when the platform feature flag
    ``require_invitation_code`` is on. When omitted/empty in that mode,
    the endpoint returns 403. Optional codes still work for bonus
    credits / plan auto-assignment when the flag is off."""


class LoginRequest(BaseModel):
    email: str | None = None
    username: str | None = None
    password: str
    remember_me: bool = False
    totp_code: str | None = None
    captcha_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    entity_id: str
    role: str




class UserResponse(BaseModel):
    id: str
    username: str | None = None
    email: str
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    role: str
    permissions: list[str] = []
    staff_role_id: str | None = None
    staff_role_name: str | None = None
    entity_id: str
    avatar_url: str | None = None
    llm_model: str | None = None
    timezone: str
    locale: str
    created_at: str | None = None
    memberships: list["UserMembershipResponse"] = []




class SwitchEntityRequest(BaseModel):
    entity_id: str


class UserMembershipResponse(BaseModel):
    entity_id: str
    entity_name: str | None = None
    role: str
    status: str
    staff_id: str | None = None
    is_primary: bool = False
    is_current: bool = False


# ── Endpoints ──

def _username_for(user: User) -> str:
    return user.display_name or user.email.split("@")[0]


def _user_response(
    user: User,
    permissions: list[str] | None = None,
    staff_role_id: str | None = None,
    staff_role_name: str | None = None,
    memberships: list[UserMembershipResponse] | None = None,
) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=_username_for(user),
        display_name=user.display_name,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        role=user.role,
        permissions=permissions or [],
        staff_role_id=staff_role_id,
        staff_role_name=staff_role_name,
        entity_id=user.entity_id,
        avatar_url=user.avatar_url,
        llm_model=user.llm_model,
        timezone=user.timezone,
        locale=user.locale,
        created_at=user.created_at.isoformat() if user.created_at else None,
        memberships=memberships or [],
    )


UserResponse.model_rebuild()


async def _membership_responses(
    db: AsyncSession,
    user: User,
) -> list[UserMembershipResponse]:
    rows = await list_user_memberships(db, user)
    return [
        UserMembershipResponse(
            entity_id=membership.entity_id,
            entity_name=entity.name if entity else None,
            role=membership.role,
            status=membership.status,
            staff_id=membership.staff_id,
            is_primary=bool(membership.is_primary),
            is_current=membership.entity_id == user.entity_id,
        )
        for membership, entity in rows
    ]


async def _register_from_staff_invite(
    db: AsyncSession,
    *,
    req: RegisterRequest,
) -> tuple[User, Entity]:
    from apps.api.routers.permissions import (
        _find_pending_invite_staff,
        _redeem_staff_invite_for_user,
    )

    invite_token = (req.invite_token or "").strip()
    staff = await _find_pending_invite_staff(db, invite_token)
    if staff is None:
        raise HTTPException(400, "Invalid or expired invite token.")
    if not staff.email:
        raise HTTPException(400, "Invite has no email on file; cannot accept.")

    email = req.email.strip().lower()
    invited_email = staff.email.strip().lower()
    if email != invited_email:
        raise HTTPException(403, "Invite is for a different email address.")

    existing = (await db.execute(
        select(User).where(
            func.lower(User.email) == email,
            User.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if existing is not None:
        if existing.entity_id == staff.entity_id:
            raise HTTPException(409, "Account already exists. Sign in to accept this invitation.")
        raise HTTPException(409, "A user with that email already exists in another organization.")

    entity = await db.get(Entity, staff.entity_id)
    if entity is None:
        raise HTTPException(400, "Invite organization no longer exists.")

    role_name = await legacy_role_for_staff_role(
        db,
        staff.role_id,
        staff.entity_id,
        default="member",
    )
    display_name = (req.username or "").strip() or staff.name or email.split("@")[0]
    user = User(
        id=generate_ulid(),
        entity_id=staff.entity_id,
        email=email,
        display_name=display_name,
        password_hash=hash_password(req.password),
        role=role_name,
        status="active",
    )
    db.add(user)
    await db.flush()

    await _redeem_staff_invite_for_user(
        db,
        token=invite_token,
        user=user,
        name=display_name,
    )
    return user, entity




@router.post("/register")
async def register(req: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Register a new user + entity. If email verification is enabled, returns pending status.

    Invitation-code gate: when the platform feature flag
    ``require_invitation_code`` is on, callers MUST pass a valid code
    or the endpoint returns 403. Codes are validated BEFORE the user
    is created so a bad code doesn't leave a half-baked row behind;
    redemption (use-counter increment + bonus credits + plan assign)
    runs AFTER user/entity creation so it can reference the new IDs.
    """
    if CAPTCHA_ENABLED:
        if not req.captcha_token:
            raise HTTPException(400, "Captcha token is required")
        remote_ip = request.client.host if request and request.client else None
        if not await verify_captcha(req.captcha_token, remote_ip):
            raise HTTPException(400, "Captcha verification failed")

    if (req.invite_token or "").strip():
        user, entity = await _register_from_staff_invite(db, req=req)
        mark_user_login(user, source="auth.register_invite")
        token = create_access_token(user.id, entity.id, user.role)
        return TokenResponse(
            access_token=token,
            user_id=user.id,
            entity_id=entity.id,
            role=user.role,
        )

    # ── Invitation code gate (validate up front) ──────────────────────
    from packages.core.services.invitation_codes import (
        InvitationCodeError,
        is_required as invite_required,
        validate_code as validate_invite,
        redeem_code as redeem_invite,
    )
    invite_row = None
    code_str = (req.invitation_code or "").strip()
    must_have_code = await invite_required(db)
    if code_str:
        try:
            invite_row = await validate_invite(db, code_str)
        except InvitationCodeError as e:
            raise HTTPException(403, str(e))
    elif must_have_code:
        raise HTTPException(
            403,
            "Registration is invite-only right now. Enter your invitation code to continue.",
        )

    try:
        user, entity = await register_user(
            db,
            email=req.email.strip().lower(),
            password=req.password,
            entity_name=req.entity_name,
            display_name=(req.username or "").strip(),
        )
    except ValueError as e:
        # If email already registered but pending verification, resend code
        if "already registered" in str(e) and _CLOUD_FEATURES_ENABLED:
            from packages.core.services.auth_service import get_user_by_email, hash_password
            existing = await get_user_by_email(db, req.email)
            if existing and existing.status == "pending":
                # Update password to whatever they typed this time
                existing.password_hash = hash_password(req.password)
                await db.flush()
                # Redeem invite if not already redeemed for this user
                if invite_row is not None:
                    from packages.core.models.invitation_code import InvitationCodeRedemption
                    already_redeemed = (await db.execute(
                        select(InvitationCodeRedemption).where(
                            InvitationCodeRedemption.user_id == existing.id,
                            InvitationCodeRedemption.code == invite_row.code,
                        )
                    )).scalar_one_or_none()
                    if not already_redeemed:
                        from packages.core.models.user import Entity
                        entity = (await db.execute(
                            select(Entity).where(Entity.id == existing.entity_id)
                        )).scalar_one()
                        await redeem_invite(db, invite_row, user=existing, entity=entity)
                code = await create_verification(existing.email, existing.id)
                await send_verification_email(existing.email, code)
                return {
                    "requires_verification": True,
                    "email": existing.email,
                    "message": "Verification code resent to your email",
                }
        detail = str(e)
        if "already registered" in detail:
            detail = detail.replace("already registered", "already taken")
        raise HTTPException(400, detail)

    # Redeem the invite (if any) inside the same transaction as the
    # signup so a failed redemption (e.g. plan disappeared mid-request)
    # rolls back user/entity too — better than a half-applied state.
    if invite_row is not None:
        try:
            await redeem_invite(db, invite_row, user=user, entity=entity)
        except InvitationCodeError as e:
            raise HTTPException(403, str(e))


    if _CLOUD_FEATURES_ENABLED:
        user.status = "pending"
        await db.flush()
        code = await create_verification(user.email, user.id)
        await send_verification_email(user.email, code)
        return {
            "requires_verification": True,
            "email": user.email,
            "message": "Verification code sent to your email",
        }

    mark_user_login(user, source="auth.register")
    token = create_access_token(user.id, entity.id, user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        entity_id=entity.id,
        role=user.role,
    )


# ── Email Verification Schemas ──

class VerifyEmailRequest(BaseModel):
    email: str
    code: str


class ResendVerificationRequest(BaseModel):
    email: str


# ── Email Verification Endpoints ──

@router.post("/verify-email")
async def verify_email_endpoint(
    req: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify email with 6-digit code. Returns JWT on success."""
    ok = await verify_email(db, req.email, req.code)
    if not ok:
        raise HTTPException(400, "Invalid or expired verification code")

    from packages.core.services.auth_service import get_user_by_email
    user = await get_user_by_email(db, req.email)
    if not user:
        raise HTTPException(400, "User not found")

    # Send welcome email
    from packages.core.services.email_service import send_welcome_email
    await send_welcome_email(req.email, user.display_name or req.email.split("@")[0])

    mark_user_login(user, source="auth.verify_email")
    token = create_access_token(user.id, user.entity_id, user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        entity_id=user.entity_id,
        role=user.role,
    )


@router.post("/resend-verification")
async def resend_verification_endpoint(
    req: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
):
    """Resend verification code to email."""
    from packages.core.services.auth_service import get_user_by_email
    user = await get_user_by_email(db, req.email)
    if not user or user.status != "pending":
        return {"message": "If that email is pending verification, a new code has been sent."}

    code = await resend_verification(req.email, user.id)
    if code:
        await send_verification_email(req.email, code)
    return {"message": "If that email is pending verification, a new code has been sent."}


@router.post("/login")
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Authenticate and return JWT. If 2FA is enabled, requires totp_code."""
    if CAPTCHA_ENABLED:
        if not req.captcha_token:
            raise HTTPException(400, "Captcha token is required")
        remote_ip = request.client.host if request.client else None
        if not await verify_captcha(req.captcha_token, remote_ip):
            raise HTTPException(400, "Captcha verification failed")

    login_id = (req.email or req.username or "").strip()
    if not login_id:
        raise HTTPException(400, "Email or username is required")

    user = await authenticate_user(db, email=login_id, password=req.password)
    if not user:
        # If this is an OAuth-only account, provide a clear next step.
        existing = await get_user_by_login(db, login_id, include_deleted=True)
        if existing and existing.deleted_at is not None:
            # Account is in the soft-delete trash window. If the
            # password matches, redirect to the restore flow rather
            # than surfacing a misleading "invalid credentials" error.
            from packages.core.services.auth_service import verify_password as _vp
            from packages.core.services.user_lifecycle import USER_PURGE_GRACE_DAYS
            if _vp(req.password, existing.password_hash):
                return {
                    "requires_restore": True,
                    "email": existing.email,
                    "grace_days": USER_PURGE_GRACE_DAYS,
                }
            # Wrong password on a deleted account → fall through to
            # the generic 401 below (don't leak deletion state).
        if existing:
            has_password_login = (
                isinstance(existing.password_hash, str)
                and existing.password_hash.startswith(("$2a$", "$2b$", "$2y$"))
            )
            if not has_password_login:
                providers = (
                    await db.execute(
                        select(OAuthAccount.provider).where(OAuthAccount.user_id == existing.id)
                    )
                ).scalars().all()
                if providers:
                    provider_list = ", ".join(sorted(set(providers)))
                    raise HTTPException(
                        400,
                        f"This account uses {provider_list} sign-in. Please use OAuth login or reset your password first.",
                    )
        raise HTTPException(401, "Invalid email/username or password")

    # Check email verification
    if _CLOUD_FEATURES_ENABLED and user.status == "pending":
        code = await create_verification(user.email, user.id)
        await send_verification_email(user.email, code)
        return {"requires_verification": True, "email": user.email}

    # Check 2FA
    if user.totp_enabled:
        if not req.totp_code:
            return {"requires_2fa": True, "user_id": user.id}
        if not await verify_2fa_login(db, user.id, req.totp_code):
            raise HTTPException(401, "Invalid 2FA code")

    mark_user_login(user, source="auth.login")
    token = create_access_token(user.id, user.entity_id, user.role, remember=req.remember_me)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        entity_id=user.entity_id,
        role=user.role,
    )


@router.get("/me", response_model=UserResponse)
async def me(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current authenticated user."""
    staff_role_id, staff_role_name, _ = await user_staff_role_summary(
        db,
        user.id,
        user.entity_id,
    )
    permissions = sorted(await user_effective_permission_keys(
        db,
        user.id,
        user.entity_id,
        user.role,
    ))
    return _user_response(
        user,
        permissions=permissions,
        staff_role_id=staff_role_id,
        staff_role_name=staff_role_name,
        memberships=await _membership_responses(db, user),
    )


@router.post("/entities/switch", response_model=TokenResponse)
async def switch_entity(
    req: SwitchEntityRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entity_id = (req.entity_id or "").strip()
    if not entity_id:
        raise HTTPException(400, "entity_id is required")
    membership = await get_user_membership(db, user=user, entity_id=entity_id)
    if not membership or membership.status != "active":
        raise HTTPException(403, "You do not have an active membership in this company.")
    await activate_user_membership(db, user=user, membership=membership)
    token = create_access_token(user.id, membership.entity_id, membership.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        entity_id=membership.entity_id,
        role=membership.role,
    )




# ── Profile Update Schemas ──

class UpdateProfileRequest(BaseModel):
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    timezone: str | None = None
    locale: str | None = None
    llm_model: str | None = None


# ── Profile Update Endpoints ──

@router.put("/me", response_model=UserResponse)
async def update_profile(
    req: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the current user's profile."""
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    for key, value in updates.items():
        setattr(user, key, value)
    await db.flush()
    if "timezone" in updates:
        calendar_settings = (user.preferences or {}).get("calendar_settings")
        if isinstance(calendar_settings, dict):
            synced_calendar_settings = {**calendar_settings, "timezone": user.timezone}
            await update_user_preferences(db, user.id, {"calendar_settings": synced_calendar_settings})
        try:
            from packages.core.briefing.scheduling import sync_user_briefing_schedules

            await sync_user_briefing_schedules(db, user)
        except Exception as exc:
            logger.warning("failed to sync briefing schedules after timezone update: %s", exc)
    return _user_response(user)


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload avatar image. Stores via JuiceFS or base64 fallback."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    import uuid

    raw_ext = file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "jpg"
    ext = "".join(ch for ch in raw_ext.lower() if ch.isalnum())[:10] or "jpg"
    content = await file.read()

    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        is_fs_enabled,
        write_entity_file_atomic,
    )
    if is_fs_enabled():
        fname = f"{uuid.uuid4().hex}.{ext}"
        try:
            write_entity_file_atomic(
                user.entity_id,
                f"avatars/{fname}",
                content,
                expected_size=len(content),
            )
        except EntityFilesystemError as exc:
            raise HTTPException(
                503,
                f"Entity filesystem is not available: {exc}",
            ) from exc
        avatar_url = f"/api/v1/fs/{user.entity_id}/avatars/{fname}"
    else:
        import base64
        b64 = base64.b64encode(content).decode()
        avatar_url = f"data:{file.content_type};base64,{b64}"

    user.avatar_url = avatar_url
    await db.flush()
    return {"avatar_url": avatar_url}


# ── AI Model Config ──

@router.get("/models/catalog")
async def get_model_catalog(db: AsyncSession = Depends(get_db)):
    """Return available AI models grouped by role.

    Admin-disabled models are filtered out and admin default overrides
    applied, so the picker only ever offers what the platform allows.
    """
    from packages.core.services.model_gateway import model_provider_catalog
    from packages.core.services.model_pricing_gateway import pricing_catalog
    from packages.core.services.model_settings import (
        effective_catalog,
        effective_defaults,
        get_model_settings_cached,
    )

    settings = await get_model_settings_cached(db)
    return {
        "catalog": effective_catalog(settings),
        "defaults": effective_defaults(settings),
        "providers": model_provider_catalog(),
        "pricing": pricing_catalog(),
    }


@router.get("/me/models")
async def get_my_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the user's selected models per role."""
    from packages.core.constants.models import DEFAULTS
    from packages.core.services.model_resolver import resolve_model_for_user

    entity = await _load_current_entity(db, user)
    entity_settings = entity.settings if entity else {}

    resolved = {}
    for role in DEFAULTS:
        resolved[role] = await resolve_model_for_user(
            role,
            user_id=user.id,
            entity_id=user.entity_id,
            db=db,
        )

    return {
        "models": resolved,
        "user_models": {},
        "entity_models": (entity_settings.get("models") or {}),
        "can_manage_byok": _can_manage_entity_byok(user),
    }


class UpdateModelsRequest(BaseModel):
    models: dict  # {"primary": "anthropic/claude-sonnet-4.6", "worker": "openai/gpt-4o-mini", ...}


@router.put("/me/models")
async def update_my_models(
    req: UpdateModelsRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the current entity's model preferences."""
    from packages.core.constants.models import DEFAULTS
    from packages.core.services.audit_service import log_action
    from packages.core.services.model_settings import (
        effective_catalog,
        get_model_settings_cached,
    )

    # Validate against the effective catalog so admin-disabled models
    # can't be saved as a preference.
    catalog = effective_catalog(await get_model_settings_cached(db))
    entity = await _require_entity_byok_manager(db, user)

    updates: dict[str, str] = {}
    for role, raw_model in (req.models or {}).items():
        role_key = str(role or "").strip()
        model_id = str(raw_model or "").strip()
        if role_key not in DEFAULTS:
            raise HTTPException(status_code=400, detail=f"Unknown model role: {role_key or '(empty)'}")
        if not model_id:
            raise HTTPException(status_code=400, detail=f"Model id is required for role: {role_key}")

        catalog_ids = {str(item.get("id")) for item in catalog.get(role_key, []) if item.get("id")}
        if catalog_ids and model_id not in catalog_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Model {model_id} is not available for role: {role_key}",
            )
        updates[role_key] = model_id

    settings = dict(entity.settings or {})
    existing_models = dict(settings.get("models") or {})
    changed = {
        role: {"old": existing_models.get(role), "new": model_id}
        for role, model_id in updates.items()
        if existing_models.get(role) != model_id
    }

    settings["models"] = {**existing_models, **updates}
    entity.settings = settings

    if changed:
        await log_action(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            action="entity.models.update",
            resource_type="entity",
            resource_id=entity.id,
            details={
                "changes": changed,
                "user_agent": request.headers.get("user-agent", "")[:300],
            },
            ip_address=request.client.host if request.client else None,
        )

    await db.flush()
    return {"models": settings["models"], "changed": changed}


# ── LLM API Key Config ──

def _can_manage_entity_byok(user: User) -> bool:
    return getattr(user, "role", None) == "owner"


async def _load_current_entity(db: AsyncSession, user: User) -> Entity:
    entity = (await db.execute(
        select(Entity).where(Entity.id == user.entity_id)
    )).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return entity


async def _require_entity_byok_manager(db: AsyncSession, user: User) -> Entity:
    if not _can_manage_entity_byok(user):
        raise HTTPException(status_code=403, detail="Only the organization owner can manage model provider keys.")
    return await _load_current_entity(db, user)


async def _legacy_owner_preferences(db: AsyncSession, entity_id: str) -> dict:
    from packages.core.services.model_resolver import load_entity_owner_preferences

    return await load_entity_owner_preferences(db, entity_id) or {}


async def _effective_entity_settings(db: AsyncSession, entity: Entity) -> dict:
    settings = dict(entity.settings or {})
    if settings.get("llm_api_key") or settings.get("llm_api_keys"):
        return settings
    owner_prefs = await _legacy_owner_preferences(db, entity.id)
    if owner_prefs.get("llm_api_key") or owner_prefs.get("llm_api_keys"):
        merged = dict(settings)
        for key in ("llm_api_key", "llm_base_url", "llm_api_keys", "llm_base_urls"):
            if key in owner_prefs and key not in merged:
                merged[key] = owner_prefs[key]
        return merged
    return settings

@router.get("/me/llm-config")
async def get_llm_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current entity's LLM API key config (key is masked)."""
    entity = await _load_current_entity(db, user)
    prefs = await _effective_entity_settings(db, entity)
    from packages.core.services.model_resolver import byok_allowed_for_plan, sanitize_llm_api_key
    def _visible_native_key(value: str | None) -> str:
        key = sanitize_llm_api_key(str(value or ""), "stored_user_api_key")
        return key if key and not key.startswith("sk-or-") else ""

    raw_key = prefs.get("llm_api_key", "")
    visible_key = _visible_native_key(raw_key)
    has_key = bool(visible_key)
    masked = (visible_key[:4] + "****" + visible_key[-4:]) if len(visible_key) > 8 else ("****" if visible_key else "")
    role_keys = prefs.get("llm_api_keys") or {}
    masked_role_keys = {
        role: (visible[:4] + "****" + visible[-4:] if len(visible) > 8 else "****")
        for role, key in role_keys.items()
        if (visible := _visible_native_key(str(key)))
    }
    role_base_urls = {
        role: url
        for role, url in (prefs.get("llm_base_urls") or {}).items()
        if role in masked_role_keys
    }
    byok_allowed = byok_allowed_for_plan(getattr(entity, "plan_id", None))
    return {
        "has_api_key": has_key,
        "llm_api_key": masked if has_key else "",
        "llm_base_url": prefs.get("llm_base_url", "") if has_key else "",
        "role_api_keys": masked_role_keys,
        "role_base_urls": role_base_urls,
        "byok_allowed": byok_allowed,
        "byok_effective": byok_allowed and (has_key or bool(masked_role_keys)),
        "can_manage_byok": _can_manage_entity_byok(user),
        "scope": "entity",
    }


class LlmApiKeyRequest(BaseModel):
    llm_api_key: str
    role: str | None = None


class LlmBaseUrlRequest(BaseModel):
    llm_base_url: str
    role: str | None = None


class CustomModelRequest(BaseModel):
    role: str
    model: str
    api_key: str | None = None
    use_saved_api_key: bool = False
    base_url: str | None = None
    test_token: str | None = None


def _validate_model_role(role: str | None) -> str | None:
    if not role:
        return None
    from packages.core.constants.models import DEFAULTS
    if role not in DEFAULTS:
        raise HTTPException(status_code=400, detail=f"Unknown model role: {role}")
    return role


async def _resolve_role_model_for_user(db: AsyncSession, user: User, role: str) -> str:
    from packages.core.services.model_resolver import resolve_model_for_user

    return await resolve_model_for_user(role, user_id=user.id, entity_id=user.entity_id, db=db)


def _catalog_model_provider(model: str) -> str | None:
    if not model or "/" not in model:
        return None
    return model.split("/", 1)[0].strip().lower() or None


def _is_catalog_model(role: str, model: str) -> bool:
    from packages.core.constants.models import CATALOG
    return any(str(item.get("id") or "") == model for item in CATALOG.get(role, []))


def _stored_role_api_key(settings: dict | None, role: str) -> str:
    settings = settings or {}
    role_keys = settings.get("llm_api_keys") or {}
    raw = role_keys.get(role) or (settings.get("llm_api_key", "") if role == "primary" else "")
    from packages.core.services.model_resolver import sanitize_llm_api_key
    return sanitize_llm_api_key(str(raw or ""), f"stored.{role}.api_key")


def _resolve_custom_model_key(req: CustomModelRequest, settings: dict | None, role: str) -> str:
    from packages.core.services.model_resolver import (
        detect_llm_provider_from_key,
        sanitize_llm_api_key,
    )

    key = sanitize_llm_api_key(str(req.api_key or ""), f"{role}.api_key")
    if not key and req.use_saved_api_key:
        key = _stored_role_api_key(settings, role)
    if not key:
        raise HTTPException(status_code=400, detail="A native provider API key is required to test this custom model.")
    if detect_llm_provider_from_key(key) == "openrouter":
        raise HTTPException(
            status_code=400,
            detail=(
                "User-provided model keys must be native provider keys. "
                + (
                    "Clear or reset this key to use Manor's official routing with credits."
                    if _CLOUD_FEATURES_ENABLED
                    else "OpenRouter-style gateway keys are not supported in self-hosted mode."
                )
            ),
        )
    return key


async def _probe_custom_model(role: str, model: str, api_key: str, base_url: str | None) -> tuple[str | None, int | None]:
    from packages.core.services.model_resolver import (
        detect_llm_provider_from_key,
        normalize_llm_model_for_provider,
        resolve_llm_provider_base_url,
    )

    started = time.perf_counter()
    resolved_base = resolve_llm_provider_base_url(model, api_key, base_url).rstrip("/")
    provider = detect_llm_provider_from_key(api_key) or _catalog_model_provider(model)
    provider_model = normalize_llm_model_for_provider(model, resolved_base)

    if role in {"primary", "worker"}:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if "api.anthropic.com" in resolved_base:
            headers["anthropic-version"] = "2023-06-01"
            payload = {
                "model": provider_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            url = f"{resolved_base}/messages"
        else:
            payload = {
                "model": provider_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            url = f"{resolved_base}/chat/completions"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                detail = resp.text[:500]
                try:
                    data = resp.json()
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        detail = str(err.get("message") or detail)
                    elif isinstance(data, dict) and data.get("detail"):
                        detail = str(data.get("detail"))
                except Exception:
                    pass
                raise HTTPException(status_code=400, detail=f"Model test failed: {detail}")

            # The normal Manor chat path sends OpenAI/Anthropic-compatible tool
            # schemas. A plain ping can pass while real chat hangs or fails once
            # tools are present, so validate the production-critical shape too.
            if "api.anthropic.com" in resolved_base:
                tool_payload = {
                    "model": provider_model,
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "ping"}],
                    "tools": [{
                        "name": "noop_probe",
                        "description": "No-op probe for tool schema compatibility.",
                        "input_schema": {"type": "object", "properties": {}},
                    }],
                }
            else:
                tool_payload = {
                    "model": provider_model,
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "ping"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "noop_probe",
                            "description": "No-op probe for tool schema compatibility.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }],
                    "tool_choice": "auto",
                }
            tool_resp = await client.post(url, json=tool_payload, headers=headers)
            if tool_resp.status_code >= 400:
                detail = tool_resp.text[:500]
                try:
                    data = tool_resp.json()
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        detail = str(err.get("message") or detail)
                    elif isinstance(data, dict) and data.get("detail"):
                        detail = str(data.get("detail"))
                except Exception:
                    pass
                raise HTTPException(status_code=400, detail=f"Model tool-call test failed: {detail}")

    elif role == "embedding":
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": provider_model, "input": "ping"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{resolved_base}/embeddings", json=payload, headers=headers)
            if resp.status_code >= 400:
                raise HTTPException(status_code=400, detail=f"Embedding test failed: {resp.text[:500]}")
    else:
        raise HTTPException(
            status_code=400,
            detail="Live testing is currently supported for custom chat and embedding models. Save image, video, and speech models through the catalog flow.",
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    return provider, latency_ms


def _validate_custom_model_request(
    req: CustomModelRequest,
    settings: dict | None = None,
) -> tuple[str, str, str, str]:
    role = _validate_model_role(req.role)
    if not role:
        raise HTTPException(status_code=400, detail="Model role is required.")
    if role in {"image", "video", "stt"}:
        raise HTTPException(status_code=400, detail="Custom model testing is currently supported for Primary AI, Worker AI, and Embedding.")
    model = str(req.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model id is required.")
    key = _resolve_custom_model_key(req, settings, role)
    if _is_catalog_model(role, model):
        _validate_role_api_key_for_model(role, key, model)
    base_url = str(req.base_url or "").strip().rstrip("/")
    return role, model, key, base_url


def _mask_key(raw: str) -> str:
    return raw[:4] + "****" + raw[-4:] if len(raw) > 8 else ("****" if raw else "")


def _validate_role_api_key_for_model(role: str, api_key: str, model: str) -> None:
    """Validate that a user BYOK key can call the selected catalog model."""
    from packages.core.services.model_resolver import (
        detect_llm_provider_from_key,
        sanitize_llm_api_key,
    )

    key = sanitize_llm_api_key(api_key, f"{role}.api_key")
    if not key:
        raise HTTPException(status_code=400, detail="API key is empty or malformed.")

    provider = detect_llm_provider_from_key(key)
    if provider == "openrouter":
        raise HTTPException(
            status_code=400,
            detail=(
                "User-provided model keys must be native provider keys. "
                + (
                    "Clear or reset this key to use Manor's official routing with credits."
                    if _CLOUD_FEATURES_ENABLED
                    else "OpenRouter-style gateway keys are not supported in self-hosted mode."
                )
            ),
        )

    model_provider = _catalog_model_provider(model)
    if role == "image":
        if model_provider == "openai" and provider == "openai":
            return
        if model_provider == "google" and provider == "google":
            return
        raise HTTPException(
            status_code=400,
            detail=(
                f"Image model {model} requires a native key matching the selected "
                "OpenAI/Google image model."
            ),
        )

    if role == "video":
        if model_provider in {"bytedance", "kwaivgi"} and provider not in {"anthropic", "google", "groq"}:
            return
        raise HTTPException(
            status_code=400,
            detail=(
                f"Video model {model} requires a native Seedance/Kling key for the "
                "selected video model."
            ),
        )

    if role == "stt":
        if model_provider == "openai" and provider == "openai":
            return
        raise HTTPException(
            status_code=400,
            detail=(
                f"Speech-to-text model {model} requires a matching native OpenAI key. "
            ),
        )

    if role in {"primary", "worker"}:
        # DeepSeek, Qwen, and Moonshot all issue generic sk-* keys, so the
        # selected catalog model is the routing signal for those vendors.
        if provider == "openai" and model_provider in {"deepseek", "qwen", "moonshotai"}:
            return
        if provider and model_provider == provider:
            return
        raise HTTPException(
            status_code=400,
            detail=(
                f"The selected {role} model {model} is a {model_provider or 'catalog'} model. "
                "Use a native key that matches your selected model."
            ),
        )


def _validate_role_base_url_for_model(role: str, base_url: str, model: str) -> None:
    """Reject known provider endpoints that do not match the selected catalog model."""
    url = str(base_url or "").strip()
    if not url:
        return
    from packages.core.services.model_resolver import llm_provider_from_base_url

    base_provider = llm_provider_from_base_url(url)
    if not base_provider:
        # Custom OpenAI-compatible proxies are allowed for custom model IDs.
        return
    if base_provider == "openrouter":
        raise HTTPException(
            status_code=400,
            detail=(
                "Leave the base URL empty to use Manor's official routing."
                if _CLOUD_FEATURES_ENABLED
                else "OpenRouter base URLs are not supported in self-hosted mode; use the native provider endpoint."
            ),
        )
    model_provider = _catalog_model_provider(model)
    if model_provider and base_provider != model_provider:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The saved base URL routes to {base_provider}, but the selected "
                f"{role} model {model} is from {model_provider}. Use a matching "
                "native provider endpoint."
            ),
        )


@router.post("/me/models/test")
async def test_custom_model(
    req: CustomModelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test a draft custom BYOK model configuration without saving it."""
    entity = await _require_entity_byok_manager(db, user)
    role, model, key, base_url = _validate_custom_model_request(req, entity.settings or {})
    provider, latency_ms = await _probe_custom_model(role, model, key, base_url)
    return {
        "ok": True,
        "detail": "Model test passed",
        "provider": provider,
        "latency_ms": latency_ms,
    }


@router.put("/me/models/custom")
async def save_custom_model(
    req: CustomModelRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a custom BYOK model, key, and base URL atomically."""
    entity = await _require_entity_byok_manager(db, user)
    settings = dict(entity.settings or {})
    role, model, key, base_url = _validate_custom_model_request(req, settings)
    existing_models = dict(settings.get("models") or {})
    role_keys = dict(settings.get("llm_api_keys") or {})
    role_urls = dict(settings.get("llm_base_urls") or {})

    changed = {}
    if existing_models.get(role) != model:
        changed["model"] = {"old": existing_models.get(role), "new": model}
    existing_models[role] = model
    settings["models"] = existing_models

    if req.api_key and req.api_key.strip():
        role_keys[role] = key
        changed["api_key"] = "updated"
    elif req.use_saved_api_key and not role_keys.get(role) and role == "primary" and settings.get("llm_api_key"):
        role_keys[role] = key
    settings["llm_api_keys"] = role_keys

    old_base_url = role_urls.get(role, "")
    if base_url:
        role_urls[role] = base_url
    else:
        role_urls.pop(role, None)
    if old_base_url != (role_urls.get(role, "") or ""):
        changed["base_url"] = {"old": old_base_url, "new": role_urls.get(role, "")}
    settings["llm_base_urls"] = role_urls

    if role == "primary":
        settings["llm_api_key"] = role_keys.get(role, key)
        if base_url:
            settings["llm_base_url"] = base_url
        else:
            settings.pop("llm_base_url", None)

    entity.settings = settings

    from packages.core.services.audit_service import log_action
    if changed:
        await log_action(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            action="entity.models.custom_update",
            resource_type="entity",
            resource_id=entity.id,
            details={
                "role": role,
                "changes": changed,
                "user_agent": request.headers.get("user-agent", "")[:300],
            },
            ip_address=request.client.host if request.client else None,
        )

    await db.flush()
    return {
        "detail": "Custom model settings saved",
        "models": settings["models"],
        "masked": _mask_key(key),
    }


@router.put("/me/llm-api-key")
async def update_llm_api_key(
    req: LlmApiKeyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the current entity's LLM API key."""
    entity = await _require_entity_byok_manager(db, user)
    settings = dict(entity.settings or {})
    role = _validate_model_role(req.role)
    if role:
        if req.llm_api_key.strip():
            model = await _resolve_role_model_for_user(db, user, role)
            _validate_role_api_key_for_model(role, req.llm_api_key, model)
        role_keys = dict(settings.get("llm_api_keys") or {})
        if req.llm_api_key.strip():
            role_keys[role] = req.llm_api_key
            if role == "primary":
                settings["llm_api_key"] = req.llm_api_key
        else:
            role_keys.pop(role, None)
            if role == "primary":
                settings.pop("llm_api_key", None)
                settings.pop("llm_base_url", None)
            role_urls = dict(settings.get("llm_base_urls") or {})
            role_urls.pop(role, None)
            settings["llm_base_urls"] = role_urls
        settings["llm_api_keys"] = role_keys
    else:
        if req.llm_api_key.strip():
            model = await _resolve_role_model_for_user(db, user, "primary")
            _validate_role_api_key_for_model("primary", req.llm_api_key, model)
            role_keys = dict(settings.get("llm_api_keys") or {})
            role_keys["primary"] = req.llm_api_key
            settings["llm_api_keys"] = role_keys
            settings["llm_api_key"] = req.llm_api_key
        else:
            settings.pop("llm_api_key", None)
            settings.pop("llm_base_url", None)
            role_keys = dict(settings.get("llm_api_keys") or {})
            role_keys.pop("primary", None)
            settings["llm_api_keys"] = role_keys
            role_urls = dict(settings.get("llm_base_urls") or {})
            role_urls.pop("primary", None)
            settings["llm_base_urls"] = role_urls
    entity.settings = settings
    await db.flush()
    raw = req.llm_api_key.strip()
    masked = raw[:4] + "****" + raw[-4:] if len(raw) > 8 else ("****" if raw else "")
    return {
        "detail": (
            "API key saved"
            if raw
            else (
                "API key reset to Manor official routing"
                if _CLOUD_FEATURES_ENABLED
                else "API key cleared; self-hosted model calls require your own provider key"
            )
        ),
        "masked": masked,
    }


@router.put("/me/llm-base-url")
async def update_llm_base_url(
    req: LlmBaseUrlRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the current entity's LLM base URL."""
    entity = await _require_entity_byok_manager(db, user)
    settings = dict(entity.settings or {})
    role = _validate_model_role(req.role)
    if role:
        if req.llm_base_url.strip():
            model = await _resolve_role_model_for_user(db, user, role)
            _validate_role_base_url_for_model(role, req.llm_base_url, model)
        role_urls = dict(settings.get("llm_base_urls") or {})
        if req.llm_base_url.strip():
            role_urls[role] = req.llm_base_url
            if role == "primary":
                settings["llm_base_url"] = req.llm_base_url
        else:
            role_urls.pop(role, None)
            if role == "primary":
                settings.pop("llm_base_url", None)
        settings["llm_base_urls"] = role_urls
    else:
        if req.llm_base_url.strip():
            model = await _resolve_role_model_for_user(db, user, "primary")
            _validate_role_base_url_for_model("primary", req.llm_base_url, model)
        role_urls = dict(settings.get("llm_base_urls") or {})
        if req.llm_base_url.strip():
            role_urls["primary"] = req.llm_base_url
            settings["llm_base_url"] = req.llm_base_url
        else:
            role_urls.pop("primary", None)
            settings.pop("llm_base_url", None)
        settings["llm_base_urls"] = role_urls
    entity.settings = settings
    await db.flush()
    return {"detail": "Base URL saved"}


# -- 2FA Schemas --

class TwoFactorCodeRequest(BaseModel):
    code: str


# -- 2FA Endpoints --

@router.post("/2fa/setup")
async def setup_2fa_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin 2FA setup: generate secret and return QR URI."""
    result = await setup_2fa(db, user.id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/2fa/verify")
async def verify_2fa_endpoint(
    req: TwoFactorCodeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify TOTP code and enable 2FA. Returns backup codes."""
    result = await verify_and_enable_2fa(db, user.id, req.code)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/2fa/disable")
async def disable_2fa_endpoint(
    req: TwoFactorCodeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable 2FA after verifying current code."""
    ok = await disable_2fa(db, user.id, req.code)
    if not ok:
        raise HTTPException(400, "Invalid code or 2FA not enabled")
    return {"disabled": True}


@router.get("/2fa/status")
async def get_2fa_status(
    user: User = Depends(get_current_user),
):
    """Check if 2FA is enabled for the current user."""
    return {"enabled": user.totp_enabled, "totp_enabled": user.totp_enabled}


class PermissionsResponse(BaseModel):
    role: str
    permissions: list[str]


@router.get("/permissions", response_model=PermissionsResponse)
async def get_permissions(user: User = Depends(get_current_user)):
    """Get the current user's role and all their permissions."""
    perms = _get_role_permissions(user.role)
    return PermissionsResponse(
        role=user.role,
        permissions=sorted(p.value for p in perms),
    )


@router.get("/locales")
async def get_locales():
    """Return supported locales and the current request locale."""
    return {
        "supported": sorted(SUPPORTED_LOCALES),
        "current": get_locale(),
    }


# ── OAuth Schemas ──

class OAuthGoogleRequest(BaseModel):
    code: str | None = None        # Google auth code (first call)
    redirect_uri: str
    invitation_code: str | None = None
    team_invite_token: str | None = None
    oauth_session: str | None = None  # Opaque token for retry with invite code
    public_chat_token: str | None = None


class OAuthGoogleConfigResponse(BaseModel):
    enabled: bool
    client_id: str | None = None


# ── OAuth Endpoints ──

@router.get("/oauth/google/config", response_model=OAuthGoogleConfigResponse)
async def oauth_google_config(db: AsyncSession = Depends(get_db)):
    """Expose the public Google OAuth client id used by the login page."""
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    cfg = await resolve_oauth_config(db, "gmail")
    if not cfg:
        return OAuthGoogleConfigResponse(enabled=False, client_id=None)
    return OAuthGoogleConfigResponse(enabled=True, client_id=cfg.client_id)


@router.post("/oauth/google")
async def oauth_google(req: OAuthGoogleRequest, db: AsyncSession = Depends(get_db)):
    """Exchange Google auth code for JWT. Creates or links user account.

    Two-step flow when invitation code is required for new users:
      1. First call with `code` → exchanges with Google, checks if user exists.
         If new + invite required → returns 403 with `oauth_session` token.
      2. Retry with `oauth_session` + `invitation_code` → creates user.

    Google already verifies the email, so OAuth users skip email verification.
    """
    import json
    import hashlib
    import hmac
    import base64
    import time
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    cfg = await resolve_oauth_config(db, "gmail")
    if not cfg:
        raise HTTPException(500, "Google OAuth not configured")

    jwt_secret = get_settings().JWT_SECRET_KEY

    def _sign_session(data: dict) -> str:
        """Create a signed opaque token containing Google user info + tokens."""
        data["_ts"] = int(time.time())
        payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        sig = hmac.new(jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def _verify_session(token: str) -> dict | None:
        """Verify and decode the signed session. Returns None if invalid/expired (10min)."""
        try:
            payload, sig = token.rsplit(".", 1)
            expected = hmac.new(jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload))
            if time.time() - data.get("_ts", 0) > 600:  # 10 min expiry
                return None
            return data
        except Exception:
            return None

    # ── Step 2: retry with oauth_session + invitation_code ──────────
    if req.oauth_session:
        session = _verify_session(req.oauth_session)
        if not session:
            raise HTTPException(400, "OAuth session expired. Please sign in with Google again.")
        info = session["info"]
        tokens = session["tokens"]
    else:
        # ── Step 1: exchange Google auth code ───────────────────────
        if not req.code:
            raise HTTPException(400, "Missing Google auth code")

        async with httpx.AsyncClient() as http:
            token_resp = await http.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": req.code,
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "redirect_uri": req.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                raise HTTPException(400, "Failed to exchange Google auth code")
            tokens = token_resp.json()

            userinfo_resp = await http.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if userinfo_resp.status_code != 200:
                raise HTTPException(400, "Failed to get Google user info")
            info = userinfo_resp.json()

    if not info.get("sub") or not info.get("email"):
        raise HTTPException(400, "Google account did not return required profile fields")
    if info.get("email_verified") is False:
        raise HTTPException(400, "Google email is not verified")

    team_invite_token = (req.team_invite_token or "").strip()
    if team_invite_token:
        from packages.core.services.team_invite_service import accept_team_invite_with_oauth

        accepted = await accept_team_invite_with_oauth(
            db,
            token=team_invite_token,
            provider="google",
            provider_user_id=info["sub"],
            email=info.get("email", ""),
            display_name=info.get("name"),
            first_name=info.get("given_name"),
            last_name=info.get("family_name"),
            avatar_url=info.get("picture"),
            access_token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
        )
        mark_user_login(accepted.user, source="auth.oauth.google.team_invite")
        token = create_access_token(accepted.user.id, accepted.user.entity_id, accepted.user.role)
        return TokenResponse(
            access_token=token,
            user_id=accepted.user.id,
            entity_id=accepted.user.entity_id,
            role=accepted.user.role,
        )

    # ── Invitation code gate (only for NEW users) ──────────────────
    from packages.core.models.user import OAuthAccount
    existing = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == "google",
            OAuthAccount.provider_user_id == info["sub"],
        )
    )).scalar_one_or_none()
    if not existing:
        existing_user = (await db.execute(
            select(User).where(User.email == info.get("email", ""))
        )).scalar_one_or_none()
    else:
        existing_user = True

    invite_row = None
    allow_public_customer_signup = False
    if not existing and not existing_user:
        from packages.core.services.invitation_codes import (
            InvitationCodeError,
            is_required as invite_required,
            validate_code as validate_invite,
            redeem_code as redeem_invite,
        )
        code_str = (req.invitation_code or "").strip()
        if req.public_chat_token:
            from packages.core.models.channel import ChannelConfig
            public_chat = (await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.channel_type == "webchat",
                    ChannelConfig.status == "active",
                    ChannelConfig.config["public_token"].astext == req.public_chat_token,
                )
            )).scalar_one_or_none()
            allow_public_customer_signup = bool(public_chat and (public_chat.config or {}).get("login_required"))
        must_have_code = False if allow_public_customer_signup else await invite_required(db)
        if code_str:
            try:
                invite_row = await validate_invite(db, code_str)
            except InvitationCodeError as e:
                raise HTTPException(403, str(e))
        elif must_have_code:
            # Return 403 with a signed session so the frontend can retry
            session_token = _sign_session({
                "info": {k: info.get(k) for k in ("sub", "email", "name", "given_name", "family_name", "picture")},
                "tokens": {"access_token": tokens.get("access_token"), "refresh_token": tokens.get("refresh_token")},
            })
            raise HTTPException(
                403,
                detail={
                    "message": "Registration is invite-only right now. Enter your invitation code to continue.",
                    "oauth_session": session_token,
                },
            )

    user, _is_new = await get_or_create_oauth_user(
        db,
        provider="google",
        provider_user_id=info["sub"],
        email=info.get("email", ""),
        display_name=info.get("name"),
        first_name=info.get("given_name"),
        last_name=info.get("family_name"),
        avatar_url=info.get("picture"),
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
    )
    if _is_new and allow_public_customer_signup:
        user.role = "external"
        await db.flush()

    if _is_new and invite_row is not None:
        from packages.core.models.user import Entity
        entity = (await db.execute(
            select(Entity).where(Entity.id == user.entity_id)
        )).scalar_one()
        await redeem_invite(db, invite_row, user=user, entity=entity)
        # Don't catch — if redemption fails, let the error propagate
        # so the user sees it and can retry. Silent failures = lost credits.

    if _is_new:
        # Welcome email for new OAuth users (verification is skipped)
        try:
            from packages.core.services.email_service import send_welcome_email
            await send_welcome_email(user.email, user.display_name or user.email.split("@")[0])
        except Exception:
            pass  # non-fatal

    mark_user_login(user, source="auth.oauth.google")
    token = create_access_token(user.id, user.entity_id, user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        entity_id=user.entity_id,
        role=user.role,
    )


# ── User Management Schemas ──

class InviteUserRequest(BaseModel):
    email: str
    role: str = "member"


class ChangeRoleRequest(BaseModel):
    role: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ── Helpers ──

def _require_admin(user: User):
    """Raise 403 if user is not owner or admin."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Requires owner or admin role")


# ── User Management Endpoints ──

@router.get("/users", response_model=list[UserResponse])
async def get_users(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all users in the entity (owner/admin only)."""
    _require_admin(user)
    users = await list_users(db, user.entity_id)
    return [
        _user_response(u)
        for u in users
    ]


@router.post("/users/invite", response_model=UserResponse)
async def invite_user_endpoint(
    req: InviteUserRequest,
    _gate=Depends(require_plan("users")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invite a new user by email (owner/admin only)."""
    _require_admin(user)
    new_user = await invite_user(db, user.entity_id, req.email, req.role)
    return _user_response(new_user)


@router.put("/users/{user_id}/role", response_model=UserResponse)
async def change_user_role(
    user_id: str,
    req: ChangeRoleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a user's role (owner/admin only)."""
    _require_admin(user)
    updated = await update_user_role(db, user_id, user.entity_id, req.role)
    if not updated:
        raise HTTPException(404, "User not found")
    return _user_response(updated)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a user (owner/admin only) — flips status only.

    For full account deletion (soft-delete + 30-day grace + Stripe
    cancel + cascade), use ``DELETE /auth/me``."""
    _require_admin(user)
    ok = await deactivate_user(db, user_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return Response(status_code=204)


# ── User directory lookups (P3 — used by ShareDialog & access requests) ─

class LookupByEmailRequest(BaseModel):
    email: str


class UserSummary(BaseModel):
    """Minimal user shape returned by lookup endpoints. Excludes role and
    sensitive fields to keep these endpoints safe to call from any
    authenticated user (vs ``/users`` which is admin-only)."""
    id: str
    email: str
    display_name: str | None = None
    avatar_url: str | None = None


@router.get("/users/directory", response_model=list[UserSummary])
async def list_user_directory(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List minimal in-entity user directory entries.

    This supports collaboration surfaces such as mentions, assignee
    pickers, and direct-message compose for non-admin users. It
    deliberately excludes role/status/profile-management fields; the
    richer ``/users`` endpoint remains owner/admin-only.
    """
    rows = (
        await db.execute(
            select(User)
            .join(UserMembership, UserMembership.user_id == User.id)
            .where(
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
                User.deleted_at.is_(None),
                User.status == "active",
            )
            .order_by(User.display_name.asc().nulls_last(), User.email.asc())
        )
    ).scalars().all()
    return [
        UserSummary(
            id=row.id,
            email=row.email,
            display_name=row.display_name,
            avatar_url=row.avatar_url,
        )
        for row in rows
    ]


@router.post("/users/lookup-by-email", response_model=UserSummary)
async def lookup_user_by_email(
    req: LookupByEmailRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve an email address to a user_id within the caller's entity.

    Returns 404 if no match exists. Tenant-scoped, so this is at worst an
    in-entity enumeration probe — acceptable for the share/grant flow.
    Returns no role / status / phone / timezone.
    """
    from sqlalchemy import func
    email_norm = (req.email or "").strip().lower()
    if not email_norm:
        raise HTTPException(400, "email is required")
    row = (
        await db.execute(
            select(User).join(UserMembership, UserMembership.user_id == User.id).where(
                func.lower(User.email) == email_norm,
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "No user with that email in this organization")
    return UserSummary(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        avatar_url=row.avatar_url,
    )


class BatchUsersRequest(BaseModel):
    ids: list[str]


@router.post("/users/batch", response_model=list[UserSummary])
async def batch_get_users(
    req: BatchUsersRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch minimal user info for a list of ids (entity-scoped).

    Drives the ShareDialog's "show email + name beside each grant" without
    forcing the caller to make N round-trips. Returns only ids that resolve
    to users in the caller's entity — silently drops the rest.
    """
    if not req.ids:
        return []
    # Cap to a sane limit to keep this from becoming an unbounded scan.
    ids = list({i for i in req.ids if i})[:200]
    if not ids:
        return []
    rows = (
        await db.execute(
            select(User).join(UserMembership, UserMembership.user_id == User.id).where(
                User.id.in_(ids),
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
                User.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return [
        UserSummary(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
        )
        for u in rows
    ]


# ── Account deletion (self-service, with 30-day restore window) ────────────

class DeleteAccountResponse(BaseModel):
    user_id: str
    entity_cascaded: bool
    oauth_revoked: int
    grace_days: int


@router.delete("/me", response_model=DeleteAccountResponse)
async def delete_my_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete the authenticated user's account. Restorable for
    ``USER_PURGE_GRACE_DAYS`` days via the login flow.

    Side effects (synchronous):
      * Per-provider OAuth revoke (best-effort)
      * If user is sole admin of entity → cascade-soft-delete entity
        + workspaces + cancel Stripe subscription
    Hard delete + reference anonymization runs in the nightly
    ``ops.purge_soft_deleted_users`` Celery task."""
    from packages.core.services.user_lifecycle import (
        USER_PURGE_GRACE_DAYS, soft_delete_user,
    )
    summary = await soft_delete_user(db, user.id)
    return DeleteAccountResponse(
        user_id=summary["user_id"],
        entity_cascaded=summary["entity_cascaded"],
        oauth_revoked=summary["oauth_revoked"],
        grace_days=USER_PURGE_GRACE_DAYS,
    )


class RestoreAccountRequest(BaseModel):
    email: str
    password: str


@router.post("/me/restore")
async def restore_my_account(
    req: RestoreAccountRequest,
    db: AsyncSession = Depends(get_db),
):
    """Restore a soft-deleted account via email + password (no JWT —
    the user has logged out by this point). Behaves like a login: on
    success, the account is un-deleted and a new JWT is returned.

    Returns 404 if the email isn't soft-deleted (or never existed).
    Password mismatch returns 401 to avoid leaking which emails are
    in trash."""
    from packages.core.services.auth_service import (
        get_user_by_email_including_deleted, verify_password, create_access_token,
        mark_user_login,
    )
    from packages.core.services.user_lifecycle import restore_user
    user = await get_user_by_email_including_deleted(db, req.email)
    if not user or user.deleted_at is None:
        raise HTTPException(404, "No deleted account matches that email")
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    restored = await restore_user(db, user.id)
    if not restored:
        raise HTTPException(410, "Account already purged — no longer recoverable")
    mark_user_login(restored, source="auth.restore")
    token = create_access_token(restored.id, restored.entity_id, restored.role)
    return {"access_token": token, "user_id": restored.id}


@router.get("/me/grace-days")
async def get_account_grace_days():
    """Surface the configured user-account grace window. Mirrors the
    workspace endpoint at /workspaces/trash/grace-days."""
    from packages.core.services.user_lifecycle import USER_PURGE_GRACE_DAYS
    return {"grace_days": USER_PURGE_GRACE_DAYS}


@router.put("/password")
async def change_own_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the current user's password."""
    ok = await change_password(db, user.id, req.old_password, req.new_password)
    if not ok:
        raise HTTPException(400, "Invalid old password")
    return {"detail": "Password changed"}


# ── Password Reset Schemas ──

class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# ── Password Reset Endpoints ──

@router.post("/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Request a password reset email. Always returns 200 to avoid leaking whether email exists."""
    from packages.core.services.password_reset_service import request_password_reset
    from packages.core.services.email_service import send_password_reset_email

    token = await request_password_reset(db, req.email)
    if token:
        await send_password_reset_email(req.email, token)
    return {"detail": "If that email exists, a reset link has been sent."}


@router.post("/reset-password")
async def reset_password_endpoint(
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Reset password using a valid reset token."""
    from packages.core.services.password_reset_service import reset_password

    ok = await reset_password(db, req.token, req.new_password)
    if not ok:
        raise HTTPException(400, "Invalid or expired reset token")
    return {"detail": "Password has been reset"}
