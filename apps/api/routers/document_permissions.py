"""Document share / grant / access-log endpoints (RFC §13, P3).

Companions to ``permissions_v1.py`` which already handles classify /
visibility / legal-hold / access-request CREATE via the ``authorize()``
entry. This module adds what wasn't covered there:

  * Grants CRUD — internal sharing (``ResourceGrant`` rows)
  * Shares CRUD — external sharing (``Share`` rows; opaque tokens)
  * Access requests — owner-side **decide** endpoint
                      (complements existing /permissions/access-requests
                      POST which is the *create* side)
  * Access log — owner-visible read trail (``document_access_log``)
  * Public view (``/shared-doc/{token}``) — unauthenticated access via
              share token; verifies hash, applies capability, writes log

Permission gating is intentionally light in this P3 commit: enforcement
proper lives behind the ``permissions_v1_enforce`` feature flag in
``packages/core/auth/authz.py``. Here we apply two minimum invariants
that must hold even with the flag off:

  1. Cross-entity isolation — you cannot touch a document outside your
     own entity, period.
  2. Owner + admin only for write actions (grant/share).

Anything finer (e.g. workspace-role-based view) waits for the flag flip.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from apps.api.web_base import public_web_base
from packages.core.database import get_db
from packages.core.models import (
    Capability,
    Classification,
    GrantStatus,
    PendingStatus,
    ResourceGrant,
    ResourceGrantPending,
    ResourceType,
    Share,
    SubjectType,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.staff import Staff
from packages.core.models.user import User, UserMembership
from packages.core.permissions import Permission, has_permission
from packages.core.services.document_access import user_has_document_capability

router = APIRouter(prefix="/api/v1/documents", tags=["document-permissions"])


# ── Shared helpers ────────────────────────────────────────────────────────


async def _load_doc(
    db: AsyncSession, doc_id: str, entity_id: str
) -> Document:
    """Fetch a document scoped to the actor's entity; 404 otherwise."""
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if not doc:
        raise CodedError(
            404,
            code="permissions.error.doc.not_found",
            message="Document not found",
        )
    return doc


def _is_owner_or_admin(doc: Document, user: User) -> bool:
    """P3 minimum gate: doc owner OR tenant admin can mutate ACL."""
    if doc.owner_id and doc.owner_id == user.id:
        return True
    return has_permission(user.role, Permission.ADMIN_SETTINGS)


async def _can_manage_internal_acl(db: AsyncSession, doc: Document, user: User) -> bool:
    if _is_owner_or_admin(doc, user):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities={Capability.GRANT_ACCESS, Capability.SHARE_INTERNAL},
    )


async def _can_manage_external_share(db: AsyncSession, doc: Document, user: User) -> bool:
    if _is_owner_or_admin(doc, user):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities={Capability.SHARE_EXTERNAL, Capability.GRANT_ACCESS},
    )


def _hash_token(token: str) -> str:
    """sha256 of the raw token; we never persist the plaintext."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _write_access_log(
    db: AsyncSession,
    *,
    doc: Document,
    actor_type: str,
    actor_id: str | None,
    action: str,
    share_id: str | None = None,
    request: Request | None = None,
    redacted: bool = False,
) -> None:
    """Append a row to ``document_access_log``. Best-effort — failures
    do not break the caller (audit must never block the request)."""
    cls = getattr(doc, "classification", None)
    # Per RFC §13.8: only restricted/confidential get full read-audit;
    # internal gets download+share only; public skipped. The router
    # decides what action types to log — this helper writes whatever
    # it's told.
    try:
        ip = request.client.host if (request and request.client) else None
        ua = request.headers.get("user-agent") if request else None
        await db.execute(
            text(
                "INSERT INTO document_access_log "
                "(id, entity_id, document_id, workspace_id, actor_type, "
                " actor_id, action, classification_at_access, ip, user_agent, "
                " share_id, redacted) "
                "VALUES (:id, :entity_id, :document_id, :workspace_id, "
                "        :actor_type, :actor_id, :action, "
                "        :classification, :ip, :ua, :share_id, :redacted)"
            ),
            {
                "id": generate_ulid(),
                "entity_id": doc.entity_id,
                "document_id": doc.id,
                "workspace_id": None,  # populate when we have workspace doc link
                "actor_type": actor_type,
                "actor_id": actor_id,
                "action": action,
                "classification": cls,
                "ip": ip,
                "ua": ua,
                "share_id": share_id,
                "redacted": redacted,
            },
        )
    except Exception:
        # Audit must never fail the request.
        pass


# ─────────────────────────────────────────────────────────────────────────
# Grants — internal sharing
# ─────────────────────────────────────────────────────────────────────────


class GrantResponse(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    subject_type: str
    subject_id: str
    capabilities: list[str]
    granted_by: str | None = None
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    status: str
    subject_user_id: str | None = None
    subject_staff_id: str | None = None
    subject_display_name: str | None = None
    subject_email: str | None = None
    subject_avatar_url: str | None = None


class CreateGrantRequest(BaseModel):
    # subject_type/subject_id let admins grant to a staff_role or future
    # team. The default path is "give this user these capabilities".
    subject_type: Literal["user", "staff_role", "workspace_role", "team"] = "user"
    subject_id: str
    capabilities: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


async def _resolve_user_grant_subject(
    db: AsyncSession,
    *,
    entity_id: str,
    subject_id: str,
) -> tuple[str, User, Staff | None]:
    """Return canonical user_id and display source for a user grant.

    ``subject_id`` should be a User.id. For compatibility with older clients
    that sent Staff.id, resolve active linked staff rows and store User.id for
    newly created/updated grants.
    """
    user = (
        await db.execute(
            select(User).where(
                User.id == subject_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if user:
        membership = (
            await db.execute(
                select(UserMembership).where(
                    UserMembership.user_id == user.id,
                    UserMembership.entity_id == entity_id,
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not membership and user.entity_id != entity_id:
            raise HTTPException(400, "Shared user is not a member of this organization")
        staff = (
            await db.execute(
                select(Staff).where(
                    Staff.entity_id == entity_id,
                    Staff.user_id == user.id,
                    Staff.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        return user.id, user, staff

    staff = (
        await db.execute(
            select(Staff).where(
                Staff.id == subject_id,
                Staff.entity_id == entity_id,
                Staff.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not staff or not staff.user_id:
        raise HTTPException(400, "Shared staff member must have a linked user account")

    user = (
        await db.execute(
            select(User).where(
                User.id == staff.user_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(400, "Shared staff member's linked user account was not found")
    return user.id, user, staff


async def _grant_subject_profile(
    db: AsyncSession,
    *,
    grant: ResourceGrant,
) -> dict[str, str | None]:
    if grant.subject_type != SubjectType.USER:
        return {}

    user = (
        await db.execute(
            select(User).where(
                User.id == grant.subject_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if user and user.entity_id != grant.entity_id:
        membership = (
            await db.execute(
                select(UserMembership).where(
                    UserMembership.user_id == user.id,
                    UserMembership.entity_id == grant.entity_id,
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not membership:
            user = None
    staff = None
    if user:
        staff = (
            await db.execute(
                select(Staff).where(
                    Staff.entity_id == grant.entity_id,
                    Staff.user_id == user.id,
                    Staff.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
    else:
        staff = (
            await db.execute(
                select(Staff).where(
                    Staff.id == grant.subject_id,
                    Staff.entity_id == grant.entity_id,
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if staff and staff.user_id:
            user = (
                await db.execute(
                    select(User).where(
                        User.id == staff.user_id,
                        User.entity_id == grant.entity_id,
                        User.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

    return {
        "subject_user_id": user.id if user else None,
        "subject_staff_id": staff.id if staff else None,
        "subject_display_name": (
            (staff.name if staff else None)
            or (user.display_name if user else None)
            or (user.email if user else None)
        ),
        "subject_email": (
            (staff.email if staff and staff.email else None)
            or (user.email if user else None)
        ),
        "subject_avatar_url": (
            (staff.avatar_url if staff else None)
            or (user.avatar_url if user else None)
        ),
    }


async def _grant_to_response(db: AsyncSession, grant: ResourceGrant) -> GrantResponse:
    profile = await _grant_subject_profile(db, grant=grant)
    return GrantResponse(
        id=grant.id,
        resource_type=grant.resource_type,
        resource_id=grant.resource_id,
        subject_type=grant.subject_type,
        subject_id=grant.subject_id,
        capabilities=list(grant.capabilities or []),
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        status=grant.status,
        **profile,
    )


@router.get("/{doc_id}/grants", response_model=list[GrantResponse])
async def list_doc_grants(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_internal_acl(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.grant_view_forbidden",
            message="Only the document owner/admin or a user with grant access can view grants",
        )
    rows = (
        await db.execute(
            select(ResourceGrant)
            .where(
                ResourceGrant.resource_type == ResourceType.DOCUMENT,
                ResourceGrant.resource_id == doc.id,
                ResourceGrant.entity_id == user.entity_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
            .order_by(desc(ResourceGrant.granted_at))
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    out: list[GrantResponse] = []
    for r in rows:
        # Lazy-expire: don't return grants past their expires_at.
        if r.expires_at and r.expires_at < now:
            continue
        out.append(await _grant_to_response(db, r))
    return out


@router.post("/{doc_id}/grants", response_model=GrantResponse, status_code=201)
async def create_doc_grant(
    doc_id: str,
    req: CreateGrantRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_internal_acl(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.grant_owner_only",
            message="Only the document owner/admin or a user with grant access can grant access",
        )

    # Validate capability strings against the canonical Capability set.
    _ALLOWED_CAPS = {
        Capability.VIEW, Capability.VIEW_REDACTED, Capability.COMMENT,
        Capability.EDIT, Capability.MANAGE_METADATA, Capability.SHARE_INTERNAL,
        Capability.SHARE_EXTERNAL, Capability.DOWNLOAD, Capability.PRINT,
        Capability.RECLASSIFY, Capability.DELETE, Capability.GRANT_ACCESS,
    }
    unknown = set(req.capabilities) - _ALLOWED_CAPS
    if unknown:
        raise HTTPException(400, f"Unknown capabilities: {sorted(unknown)}")

    # Invariant 7: agents can never receive share_external on confidential+
    # (this endpoint serves human grants but defense-in-depth)
    if (
        Capability.SHARE_EXTERNAL in req.capabilities
        and getattr(doc, "classification", None) in {Classification.CONFIDENTIAL, Classification.RESTRICTED}
    ):
        raise HTTPException(400, "share_external requires the per-share approval path on confidential+ docs")

    subject_id = req.subject_id
    existing_subject_ids = [subject_id]
    if req.subject_type == SubjectType.USER:
        subject_id, _, _ = await _resolve_user_grant_subject(
            db,
            entity_id=user.entity_id,
            subject_id=req.subject_id,
        )
        existing_subject_ids = list(dict.fromkeys([subject_id, req.subject_id]))

    # Idempotent: upsert (resource, subject) — replace capabilities on existing.
    existing = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == ResourceType.DOCUMENT,
                ResourceGrant.resource_id == doc.id,
                ResourceGrant.subject_type == req.subject_type,
                ResourceGrant.subject_id.in_(existing_subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        existing.subject_id = subject_id
        existing.capabilities = req.capabilities
        existing.expires_at = req.expires_at
        existing.granted_by = user.id
        existing.granted_at = datetime.now(timezone.utc)
        grant = existing
    else:
        grant = ResourceGrant(
            id=generate_ulid(),
            entity_id=user.entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc.id,
            subject_type=req.subject_type,
            subject_id=subject_id,
            capabilities=req.capabilities,
            granted_by=user.id,
            granted_at=datetime.now(timezone.utc),
            expires_at=req.expires_at,
            status=GrantStatus.ACTIVE,
        )
        db.add(grant)
    await db.commit()
    await db.refresh(grant)
    return await _grant_to_response(db, grant)


@router.delete("/{doc_id}/grants/{grant_id}", status_code=204)
async def revoke_doc_grant(
    doc_id: str,
    grant_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_internal_acl(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.grant_revoke_owner_only",
            message="Only the document owner/admin or a user with grant access can revoke access",
        )
    grant = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.resource_type == ResourceType.DOCUMENT,
                ResourceGrant.resource_id == doc.id,
                ResourceGrant.entity_id == user.entity_id,
            )
        )
    ).scalar_one_or_none()
    if not grant:
        raise CodedError(
            404,
            code="permissions.error.doc.grant_not_found",
            message="Grant not found",
        )
    grant.status = GrantStatus.REVOKED
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoked_by = user.id
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────
# Shares — external sharing
# ─────────────────────────────────────────────────────────────────────────


class ShareResponse(BaseModel):
    id: str
    audience: str | None = None
    capabilities: list[str]
    watermark: bool
    require_otp: bool
    allow_download: bool
    expires_at: datetime | None = None
    max_uses: int | None = None
    use_count: int
    last_used_at: datetime | None = None
    status: str
    created_at: datetime | None = None


class CreateShareResponse(ShareResponse):
    # Plaintext token returned **once** at creation. Server only stores
    # the sha256 hash going forward.
    token: str
    url: str


class CreateShareRequest(BaseModel):
    audience_type: Literal["anonymous", "email", "domain"] = "anonymous"
    audience_value: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["view"])
    expires_in_days: int = Field(default=7, ge=1, le=90)
    watermark: bool = True
    require_otp: bool = True
    allow_download: bool = False


@router.get("/{doc_id}/shares", response_model=list[ShareResponse])
async def list_doc_shares(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_external_share(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.share_view_forbidden",
            message="Only the document owner/admin or a user with external share access can view shares",
        )
    rows = (
        await db.execute(
            select(Share)
            .where(
                Share.resource_type == ResourceType.DOCUMENT,
                Share.resource_id == doc.id,
                Share.entity_id == user.entity_id,
                Share.status == "active",
            )
            .order_by(desc(Share.created_at))
        )
    ).scalars().all()
    return [
        ShareResponse(
            id=s.id,
            audience=s.audience,
            capabilities=list(s.capabilities or []),
            watermark=bool(s.watermark),
            require_otp=bool(s.require_otp),
            allow_download=bool(s.allow_download),
            expires_at=s.expires_at,
            max_uses=s.max_uses,
            use_count=s.use_count or 0,
            last_used_at=s.last_used_at,
            status=s.status,
            created_at=s.created_at,
        )
        for s in rows
    ]


_EXTERNAL_CAPS = {Capability.VIEW, Capability.COMMENT, Capability.DOWNLOAD}


def _normalize_share_config(req: CreateShareRequest) -> str:
    """Validate capabilities + audience and return the normalized audience string.

    Used by both ``create_doc_share`` (internal route handler) and
    ``decide_share_approval`` (when materializing a previously-approved
    share). Raising HTTPException is OK in either context.
    """
    unknown = set(req.capabilities) - _EXTERNAL_CAPS
    if unknown:
        raise HTTPException(
            400,
            f"External shares only support {sorted(_EXTERNAL_CAPS)}; got {sorted(unknown)}",
        )
    if req.audience_type == "anonymous":
        return "anonymous"
    if req.audience_type == "email":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for email audience")
        return f"email:{req.audience_value.strip().lower()}"
    if req.audience_type == "domain":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for domain audience")
        return f"domain:{req.audience_value.strip().lower()}"
    raise HTTPException(400, f"Invalid audience_type: {req.audience_type}")


async def _materialize_share(
    db: AsyncSession,
    *,
    doc: Document,
    creator_user_id: str,
    req: CreateShareRequest,
    audience: str,
    request: Request,
) -> tuple[Share, str, str]:
    """Persist a new Share row + write the audit entry; return (share, raw_token, url).

    Caller is responsible for ``await db.commit()`` and ``await db.refresh(share)``.
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    share = Share(
        id=generate_ulid(),
        entity_id=doc.entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=doc.id,
        token_hash=token_hash,
        capabilities=req.capabilities,
        audience=audience,
        require_otp=req.require_otp,
        watermark=req.watermark,
        allow_download=req.allow_download and (Capability.DOWNLOAD in req.capabilities),
        created_by=creator_user_id,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=req.expires_in_days),
        max_uses=None,
        use_count=0,
        status="active",
    )
    db.add(share)
    await _write_access_log(
        db, doc=doc, actor_type="user", actor_id=creator_user_id,
        action="share_create", share_id=share.id, request=request,
    )
    # The link is browser-bound — must point at the SPA origin, not the
    # backend port. `public_web_base()` honors APP_URL / X-Forwarded-Host
    # / Host headers in that order; in dev vite's proxy sets the
    # X-Forwarded-* headers so we land on the frontend port (e.g. 3010)
    # instead of `request.base_url` which would give the backend port.
    base = public_web_base(request)
    url = f"{base}/shared-doc/{raw_token}"
    return share, raw_token, url


def _share_to_create_response(
    share: Share, raw_token: str, url: str,
) -> "CreateShareResponse":
    return CreateShareResponse(
        id=share.id,
        audience=share.audience,
        capabilities=list(share.capabilities or []),
        watermark=bool(share.watermark),
        require_otp=bool(share.require_otp),
        allow_download=bool(share.allow_download),
        expires_at=share.expires_at,
        max_uses=share.max_uses,
        use_count=share.use_count or 0,
        last_used_at=share.last_used_at,
        status=share.status,
        created_at=share.created_at,
        token=raw_token,
        url=url,
    )


@router.post("/{doc_id}/shares", response_model=CreateShareResponse, status_code=201)
async def create_doc_share(
    doc_id: str,
    req: CreateShareRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_external_share(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.share_external_owner_only",
            message="Only the document owner/admin or a user with external share access can share externally",
        )

    cls = getattr(doc, "classification", None)
    # RFC §13.14 invariants
    if cls == Classification.RESTRICTED:
        raise CodedError(
            400,
            code="permissions.error.doc.restricted_no_external",
            message="Restricted documents cannot be shared externally",
        )
    if cls == Classification.CONFIDENTIAL:
        # Confidential requires admin approval — caller must use the
        # approval flow instead. 409 lets the frontend branch cleanly.
        raise CodedError(
            409,
            code="permissions.error.doc.confidential_needs_approval",
            message="Confidential documents require approval. POST to "
                    "/api/v1/documents/{doc_id}/share-approvals instead.",
        )

    audience = _normalize_share_config(req)
    share, raw_token, url = await _materialize_share(
        db, doc=doc, creator_user_id=user.id,
        req=req, audience=audience, request=request,
    )
    await db.commit()
    await db.refresh(share)
    return _share_to_create_response(share, raw_token, url)


@router.delete("/{doc_id}/shares/{share_id}", status_code=204)
async def revoke_doc_share(
    doc_id: str,
    share_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_external_share(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.revoke_share_owner_only",
            message="Only the document owner/admin or a user with external share access can revoke",
        )
    share = (
        await db.execute(
            select(Share).where(
                Share.id == share_id,
                Share.resource_type == ResourceType.DOCUMENT,
                Share.resource_id == doc.id,
                Share.entity_id == user.entity_id,
            )
        )
    ).scalar_one_or_none()
    if not share:
        raise HTTPException(404, "Share not found")
    share.status = "revoked"
    share.revoked_at = datetime.now(timezone.utc)
    share.revoked_by = user.id
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────
# Public viewer (unauthenticated, token-gated)
# ─────────────────────────────────────────────────────────────────────────


class SharedDocResponse(BaseModel):
    document_id: str
    name: str
    classification: str | None = None
    capabilities: list[str]
    watermark: bool
    allow_download: bool
    expires_at: datetime | None = None
    # Surfaced so the public viewer can pick a render mode (markdown /
    # text / pdf / image / unsupported) without a second request.
    file_type: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


public_router = APIRouter(prefix="/api/v1/shared-doc", tags=["public-share"])


@public_router.get("/{token}", response_model=SharedDocResponse)
async def view_shared_doc(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated. Look up a share by hashed token; enforce expiry &
    use-count; bump counters; write access log."""
    token_hash = _hash_token(token)
    share = (
        await db.execute(
            select(Share).where(
                Share.token_hash == token_hash,
                Share.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not share:
        raise CodedError(
            404,
            code="permissions.error.share.not_found_or_revoked",
            message="Share not found or revoked",
        )
    now = datetime.now(timezone.utc)
    if share.expires_at and share.expires_at < now:
        raise CodedError(
            410,
            code="permissions.error.share.expired",
            message="Share link has expired",
        )
    if share.max_uses is not None and share.use_count >= share.max_uses:
        raise CodedError(
            410,
            code="permissions.error.share.use_limit_reached",
            message="Share link reached use limit",
        )
    if share.resource_type != ResourceType.DOCUMENT:
        raise HTTPException(404, "Not a document share")

    doc = (
        await db.execute(
            select(Document).where(Document.id == share.resource_id)
        )
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Underlying document not found")

    share.use_count = (share.use_count or 0) + 1
    share.last_used_at = now

    await _write_access_log(
        db, doc=doc, actor_type="share_token", actor_id=share.id,
        action="share_use", share_id=share.id, request=request,
    )

    await db.commit()
    return SharedDocResponse(
        document_id=doc.id,
        name=doc.name,
        classification=getattr(doc, "classification", None),
        capabilities=list(share.capabilities or []),
        watermark=bool(share.watermark),
        allow_download=bool(share.allow_download),
        expires_at=share.expires_at,
        file_type=getattr(doc, "file_type", None),
        mime_type=getattr(doc, "mime_type", None),
        file_size=getattr(doc, "file_size", None),
    )


@public_router.get("/{token}/content")
async def view_shared_doc_content(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Serve the file bytes *inline* for a public document share.

    This powers the in-browser preview on the /shared-doc page —
    distinct from ``/download`` which forces a save dialog and requires
    the ``download`` capability. Viewing the content is what the
    ``view`` capability (present on every share) is *for*, so this
    endpoint only needs the standard validity gates, not a download
    grant.

    ``Content-Disposition: inline`` so PDFs/images render in the
    browser tab / iframe / <img> rather than downloading.
    """
    from fastapi.responses import FileResponse
    from packages.core.config import get_settings as _get_settings

    token_hash = _hash_token(token)
    share = (
        await db.execute(
            select(Share).where(
                Share.token_hash == token_hash,
                Share.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not share:
        raise CodedError(
            404,
            code="permissions.error.share.not_found_or_revoked",
            message="Share not found or revoked",
        )
    now = datetime.now(timezone.utc)
    if share.expires_at and share.expires_at < now:
        raise CodedError(
            410,
            code="permissions.error.share.expired",
            message="Share link has expired",
        )
    if share.max_uses is not None and share.use_count >= share.max_uses:
        raise CodedError(
            410,
            code="permissions.error.share.use_limit_reached",
            message="Share link reached use limit",
        )
    if share.resource_type != ResourceType.DOCUMENT:
        raise HTTPException(404, "Not a document share")

    doc = (
        await db.execute(select(Document).where(Document.id == share.resource_id))
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Underlying document not found")

    if not getattr(doc, "fs_path", None):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message="File is not available for preview",
        )
    import os as _os
    from packages.core.services.entity_fs import resolve_path

    full_path = resolve_path(doc.entity_id, str(doc.fs_path))
    if not full_path:
        raise HTTPException(403, "Access denied")
    if not _os.path.isfile(full_path):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message="File is not available for preview",
        )

    share.use_count = (share.use_count or 0) + 1
    share.last_used_at = now
    await _write_access_log(
        db, doc=doc, actor_type="share_token", actor_id=share.id,
        action="share_view_content", share_id=share.id, request=request,
    )
    await db.commit()

    return FileResponse(
        path=full_path,
        media_type=doc.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": "inline",
            # Public share content is sensitive-ish; don't let shared
            # proxies cache it. Browser may still keep it for the tab.
            "Cache-Control": "private, no-store",
        },
    )


@public_router.get("/{token}/download")
async def download_shared_doc(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Stream the actual file bytes for a public document share.

    Unauthenticated — the opaque share token is the entitlement. Same
    validity gates as ``view_shared_doc`` (active / not expired / under
    use limit / matches DOCUMENT resource type) PLUS the share must have
    been created with the ``download`` capability AND ``allow_download``
    must be true (the latter is the per-share override the dialog sets
    when the anonymous role is "downloader").

    Each successful download bumps ``use_count`` and writes an access
    log entry just like ``share_use`` — so admins can see who pulled
    bytes via which link.
    """
    from fastapi.responses import FileResponse
    from packages.core.config import get_settings as _get_settings

    token_hash = _hash_token(token)
    share = (
        await db.execute(
            select(Share).where(
                Share.token_hash == token_hash,
                Share.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not share:
        raise CodedError(
            404,
            code="permissions.error.share.not_found_or_revoked",
            message="Share not found or revoked",
        )
    now = datetime.now(timezone.utc)
    if share.expires_at and share.expires_at < now:
        raise CodedError(
            410,
            code="permissions.error.share.expired",
            message="Share link has expired",
        )
    if share.max_uses is not None and share.use_count >= share.max_uses:
        raise CodedError(
            410,
            code="permissions.error.share.use_limit_reached",
            message="Share link reached use limit",
        )
    if share.resource_type != ResourceType.DOCUMENT:
        raise HTTPException(404, "Not a document share")

    # Capability gate — this is the only difference from view_shared_doc.
    # We require both: (a) the share's capabilities list contains
    # 'download', AND (b) the per-share allow_download flag is true. The
    # frontend dialog already enforces this on the create side, but
    # belt-and-braces.
    caps = set(share.capabilities or [])
    if "download" not in caps or not getattr(share, "allow_download", False):
        raise CodedError(
            403,
            code="permissions.error.share.download_not_allowed",
            message="This share link does not allow downloading the file",
        )

    doc = (
        await db.execute(select(Document).where(Document.id == share.resource_id))
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Underlying document not found")

    if not getattr(doc, "fs_path", None):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message="File is not available for download",
        )
    import os as _os
    from packages.core.services.entity_fs import resolve_path

    full_path = resolve_path(doc.entity_id, str(doc.fs_path))
    if not full_path:
        raise HTTPException(403, "Access denied")
    if not _os.path.isfile(full_path):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message="File is not available for download",
        )

    share.use_count = (share.use_count or 0) + 1
    share.last_used_at = now
    await _write_access_log(
        db, doc=doc, actor_type="share_token", actor_id=share.id,
        action="share_download", share_id=share.id, request=request,
    )
    await db.commit()

    return FileResponse(
        path=full_path,
        media_type=doc.mime_type or "application/octet-stream",
        filename=doc.name,
    )


# ─────────────────────────────────────────────────────────────────────────
# Access requests (request-access flow, RFC §13.10)
# ─────────────────────────────────────────────────────────────────────────


class AccessRequestResponse(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    requester_user_id: str
    requested_capabilities: list[str]
    reason: str | None = None
    status: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    created_at: datetime | None = None


# NOTE: POST /access-requests (create) lives in apps/api/routers/permissions_v1.py
# under /api/v1/permissions/access-requests. This router only owns the
# owner-side **decide** endpoint, plus listing scoped to a single doc.


@router.get("/{doc_id}/access-requests", response_model=list[AccessRequestResponse])
async def list_doc_access_requests(
    doc_id: str,
    status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not _is_owner_or_admin(doc, user):
        raise CodedError(
            403,
            code="permissions.error.access_request.view_owner_only",
            message="Only the document owner or an admin can view access requests",
        )
    q = select(ResourceGrantPending).where(
        ResourceGrantPending.resource_type == ResourceType.DOCUMENT,
        ResourceGrantPending.resource_id == doc.id,
        ResourceGrantPending.entity_id == user.entity_id,
    )
    if status_filter:
        q = q.where(ResourceGrantPending.status == status_filter)
    rows = (await db.execute(q.order_by(desc(ResourceGrantPending.created_at)))).scalars().all()
    return [
        AccessRequestResponse(
            id=r.id,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            requester_user_id=r.requester_user_id,
            requested_capabilities=list(r.requested_capabilities or []),
            reason=r.reason,
            status=r.status,
            decided_by=r.decided_by,
            decided_at=r.decided_at,
            decision_note=r.decision_note,
            created_at=r.created_at,
        )
        for r in rows
    ]


class DecideAccessRequestRequest(BaseModel):
    decision: Literal["approve", "deny"]
    approved_capabilities: list[str] | None = None  # may narrow request
    expires_at: datetime | None = None
    note: str | None = None


@router.post(
    "/{doc_id}/access-requests/{request_id}/decision",
    response_model=AccessRequestResponse,
)
async def decide_access_request(
    doc_id: str,
    request_id: str,
    req: DecideAccessRequestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not _is_owner_or_admin(doc, user):
        raise CodedError(
            403,
            code="permissions.error.access_request.decide_owner_only",
            message="Only the document owner or an admin can decide access requests",
        )
    pending = (
        await db.execute(
            select(ResourceGrantPending).where(
                ResourceGrantPending.id == request_id,
                ResourceGrantPending.resource_type == ResourceType.DOCUMENT,
                ResourceGrantPending.resource_id == doc.id,
                ResourceGrantPending.entity_id == user.entity_id,
            )
        )
    ).scalar_one_or_none()
    if not pending:
        raise CodedError(
            404,
            code="permissions.error.access_request.not_found",
            message="Access request not found",
        )
    if pending.status != PendingStatus.PENDING:
        raise CodedError(
            400,
            code="permissions.error.access_request.already_decided",
            message=f"Access request already {pending.status}",
            vars={"status": str(pending.status)},
        )

    now = datetime.now(timezone.utc)
    if req.decision == "approve":
        # Materialize a real grant
        caps = req.approved_capabilities or list(pending.requested_capabilities or [])
        if not caps:
            raise HTTPException(400, "No capabilities to grant")
        grant = ResourceGrant(
            id=generate_ulid(),
            entity_id=user.entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc.id,
            subject_type=SubjectType.USER,
            subject_id=pending.requester_user_id,
            capabilities=caps,
            granted_by=user.id,
            granted_at=now,
            expires_at=req.expires_at,
            status=GrantStatus.ACTIVE,
        )
        db.add(grant)
        await db.flush()
        pending.granted_grant_id = grant.id
        pending.status = PendingStatus.APPROVED
    else:
        pending.status = PendingStatus.DENIED

    pending.decided_by = user.id
    pending.decided_at = now
    pending.decision_note = req.note
    await db.commit()
    await db.refresh(pending)
    return AccessRequestResponse(
        id=pending.id,
        resource_type=pending.resource_type,
        resource_id=pending.resource_id,
        requester_user_id=pending.requester_user_id,
        requested_capabilities=list(pending.requested_capabilities or []),
        reason=pending.reason,
        status=pending.status,
        decided_by=pending.decided_by,
        decided_at=pending.decided_at,
        decision_note=pending.decision_note,
        created_at=pending.created_at,
    )


# ─────────────────────────────────────────────────────────────────────────
# Access log
# ─────────────────────────────────────────────────────────────────────────


class AccessLogRowResponse(BaseModel):
    ts: datetime
    actor_type: str
    actor_id: str | None = None
    action: str
    classification_at_access: str | None = None
    ip: str | None = None
    redacted: bool = False
    share_id: str | None = None


@router.get("/{doc_id}/access-log", response_model=list[AccessLogRowResponse])
async def list_access_log(
    doc_id: str,
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    # Owner self-service (RFC §13.8) — admin can also see.
    if not _is_owner_or_admin(doc, user):
        raise HTTPException(403, "Only the document owner or an admin can view access history")
    rows = (
        await db.execute(
            text(
                "SELECT ts, actor_type, actor_id, action, "
                "       classification_at_access, ip, redacted, share_id "
                "FROM document_access_log "
                "WHERE document_id = :doc_id "
                "  AND entity_id = :entity_id "
                "ORDER BY ts DESC "
                "LIMIT :limit"
            ),
            {"doc_id": doc.id, "entity_id": user.entity_id, "limit": limit},
        )
    ).mappings().all()
    return [
        AccessLogRowResponse(
            ts=r["ts"],
            actor_type=r["actor_type"],
            actor_id=r["actor_id"],
            action=r["action"],
            classification_at_access=r["classification_at_access"],
            ip=r["ip"],
            redacted=bool(r["redacted"]),
            share_id=r["share_id"],
        )
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────
# Share approvals (Confidential external share, RFC §13.6)
# ─────────────────────────────────────────────────────────────────────────


class CreateShareApprovalRequest(BaseModel):
    """Same shape as CreateShareRequest plus a required reason field.

    The whole config is snapshotted into ``resource_grants_pending.metadata``
    so admins can review what's being shared and with whom before signing off.
    """
    audience_type: Literal["anonymous", "email", "domain"] = "email"
    audience_value: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["view"])
    expires_in_days: int = Field(default=7, ge=1, le=90)
    watermark: bool = True
    require_otp: bool = True
    allow_download: bool = False
    reason: str = Field(min_length=1)  # mandatory for audit


class ShareApprovalResponse(BaseModel):
    id: str
    document_id: str
    requester_user_id: str
    reason: str | None = None
    status: str
    # Snapshot of the requested share config — frontend can render a summary
    config: dict
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    approved_share_id: str | None = None
    created_at: datetime | None = None


class DecideShareApprovalRequest(BaseModel):
    decision: Literal["approve", "deny"]
    note: str | None = None


class DecideShareApprovalResponse(BaseModel):
    approval: ShareApprovalResponse
    # Populated only when decision='approve' AND admin actually
    # materialized a share. Token is returned exactly once — relay to
    # the requester out-of-band (email).
    token: str | None = None
    url: str | None = None


# Discriminator value used in ResourceGrantPending.resource_type for the
# share-approval flow (distinct from 'document' which is the original
# "request access" semantic).
_RT_SHARE_APPROVAL = "share"


def _serialize_share_config(req: CreateShareApprovalRequest) -> dict:
    return {
        "audience_type": req.audience_type,
        "audience_value": req.audience_value,
        "capabilities": list(req.capabilities or []),
        "expires_in_days": req.expires_in_days,
        "watermark": req.watermark,
        "require_otp": req.require_otp,
        "allow_download": req.allow_download,
    }


def _approval_to_response(p: ResourceGrantPending) -> ShareApprovalResponse:
    return ShareApprovalResponse(
        id=p.id,
        document_id=p.resource_id,
        requester_user_id=p.requester_user_id,
        reason=p.reason,
        status=p.status,
        config=dict(getattr(p, "metadata_", {}) or {}),
        decided_by=p.decided_by,
        decided_at=p.decided_at,
        decision_note=p.decision_note,
        approved_share_id=p.granted_grant_id,  # reused as share id on approve
        created_at=p.created_at,
    )


@router.post(
    "/{doc_id}/share-approvals",
    response_model=ShareApprovalResponse,
    status_code=201,
)
async def request_share_approval(
    doc_id: str,
    req: CreateShareApprovalRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a confidential-doc external share for admin approval.

    Anyone who could otherwise create a share can submit (owner or admin).
    The actual ``shares`` row is NOT created here — it's materialized by
    ``decide_share_approval`` when an admin approves.
    """
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not _is_owner_or_admin(doc, user):
        raise HTTPException(
            403, "Only the document owner or an admin can request a share approval",
        )

    cls = getattr(doc, "classification", None)
    # Restricted is hard-banned for external share, period.
    if cls == Classification.RESTRICTED:
        raise HTTPException(400, "Restricted documents cannot be shared externally")
    # Public / Internal don't need approval — caller should use the
    # plain /shares endpoint and not pay the latency of an inbox round-trip.
    if cls not in (Classification.CONFIDENTIAL,):
        raise HTTPException(
            400,
            "Approval is only required for Confidential documents. "
            "Use POST /documents/{doc_id}/shares directly.",
        )

    # Validate the embedded config up-front so a bad request gets caught
    # before the admin ever sees it. We construct the same CreateShareRequest
    # shape and reuse the normalizer.
    proxy = CreateShareRequest(
        audience_type=req.audience_type,
        audience_value=req.audience_value,
        capabilities=req.capabilities,
        expires_in_days=req.expires_in_days,
        watermark=req.watermark,
        require_otp=req.require_otp,
        allow_download=req.allow_download,
    )
    _ = _normalize_share_config(proxy)

    pending = ResourceGrantPending(
        id=generate_ulid(),
        entity_id=user.entity_id,
        resource_type=_RT_SHARE_APPROVAL,
        resource_id=doc.id,
        requester_user_id=user.id,
        # We keep requested_capabilities populated for cross-table consistency
        # but the source of truth for the share config is metadata_.
        requested_capabilities=list(req.capabilities or []),
        reason=req.reason,
        status=PendingStatus.PENDING,
        metadata_=_serialize_share_config(req),
    )
    db.add(pending)
    await db.commit()
    await db.refresh(pending)
    return _approval_to_response(pending)


@router.get(
    "/{doc_id}/share-approvals",
    response_model=list[ShareApprovalResponse],
)
async def list_share_approvals(
    doc_id: str,
    status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List share-approval requests for this document.

    Owner/admin only — same gating as other doc admin actions. Most useful
    on the admin inbox surface (filter status=pending).
    """
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not _is_owner_or_admin(doc, user):
        raise CodedError(
            403,
            code="permissions.error.share_approval.view_owner_only",
            message="Only the document owner or an admin can view share approvals",
        )
    q = select(ResourceGrantPending).where(
        ResourceGrantPending.resource_type == _RT_SHARE_APPROVAL,
        ResourceGrantPending.resource_id == doc.id,
        ResourceGrantPending.entity_id == user.entity_id,
    )
    if status_filter:
        q = q.where(ResourceGrantPending.status == status_filter)
    rows = (await db.execute(q.order_by(desc(ResourceGrantPending.created_at)))).scalars().all()
    return [_approval_to_response(r) for r in rows]


@router.post(
    "/{doc_id}/share-approvals/{approval_id}/decision",
    response_model=DecideShareApprovalResponse,
)
async def decide_share_approval(
    doc_id: str,
    approval_id: str,
    req: DecideShareApprovalRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or deny a share-approval request.

    Decision requires admin role (not just owner). On approve, we
    materialize the actual ``shares`` row from the snapshotted config and
    return the raw token + URL exactly once for the admin to relay.
    """
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not has_permission(user.role, Permission.ADMIN_SETTINGS):
        raise CodedError(
            403,
            code="permissions.error.share_approval.admin_required",
            message="Admin role required to decide share approvals",
        )

    pending = (
        await db.execute(
            select(ResourceGrantPending).where(
                ResourceGrantPending.id == approval_id,
                ResourceGrantPending.resource_type == _RT_SHARE_APPROVAL,
                ResourceGrantPending.resource_id == doc.id,
                ResourceGrantPending.entity_id == user.entity_id,
            )
        )
    ).scalar_one_or_none()
    if not pending:
        raise CodedError(
            404,
            code="permissions.error.share_approval.not_found",
            message="Share approval not found",
        )
    if pending.status != PendingStatus.PENDING:
        raise CodedError(
            400,
            code="permissions.error.share_approval.already_decided",
            message=f"Share approval already {pending.status}",
            vars={"status": str(pending.status)},
        )

    # If the doc was downgraded out of Confidential between submission and
    # decision, the approval flow no longer applies — caller should resubmit
    # via plain /shares.
    cls = getattr(doc, "classification", None)
    if cls == Classification.RESTRICTED:
        raise CodedError(
            400,
            code="permissions.error.share_approval.doc_now_restricted",
            message="Document is now Restricted; cannot approve external share",
        )

    now = datetime.now(timezone.utc)
    token: str | None = None
    url: str | None = None

    if req.decision == "approve":
        cfg = dict(getattr(pending, "metadata_", {}) or {})
        try:
            proxy = CreateShareRequest(
                audience_type=cfg.get("audience_type", "anonymous"),
                audience_value=cfg.get("audience_value"),
                capabilities=cfg.get("capabilities") or ["view"],
                expires_in_days=cfg.get("expires_in_days", 7),
                watermark=bool(cfg.get("watermark", True)),
                require_otp=bool(cfg.get("require_otp", True)),
                allow_download=bool(cfg.get("allow_download", False)),
            )
        except Exception as e:
            raise HTTPException(500, f"Stored share config invalid: {e}")

        audience = _normalize_share_config(proxy)
        share, raw_token, share_url = await _materialize_share(
            db, doc=doc,
            # The share is owned/created by the original requester, not the
            # approving admin — that way `created_by` on `shares` lines up
            # with the user who'll consume the link.
            creator_user_id=pending.requester_user_id,
            req=proxy, audience=audience, request=request,
        )
        await db.flush()
        pending.granted_grant_id = share.id  # reuse field as approved_share_id
        pending.status = PendingStatus.APPROVED
        token, url = raw_token, share_url
    else:
        pending.status = PendingStatus.DENIED

    pending.decided_by = user.id
    pending.decided_at = now
    pending.decision_note = req.note
    await db.commit()
    await db.refresh(pending)

    return DecideShareApprovalResponse(
        approval=_approval_to_response(pending),
        token=token,
        url=url,
    )


# ─────────────────────────────────────────────────────────────────────────
# Note: legal-hold + classify endpoints live in permissions_v1.py,
# under /api/v1/permissions/documents/:id/{legal-hold,classify}.
# ─────────────────────────────────────────────────────────────────────────
