"""Document share / grant / access-log endpoints (RFC §13, P3).

Companions to ``permissions_v1.py`` which already handles classify /
visibility / access-request CREATE. This module adds what wasn't covered there:

  * Grants CRUD — internal sharing (``ResourceGrant`` rows)
  * Shares CRUD — external sharing (``Share`` rows; opaque tokens)
  * Access requests — owner-side **decide** endpoint
                      (complements existing /permissions/access-requests
                      POST which is the *create* side)
  * Access log — owner-visible read trail (``document_access_log``)
  * Public view (``/shared-doc/{token}``) — unauthenticated access via
              share token; verifies hash, applies capability, writes log

Permission gating follows the effective-capability model in
``packages.core.services.document_access``. Two invariants always hold:

  1. Cross-entity isolation — you cannot touch a document outside your
     own entity, period.
  2. ACL writes require owner/admin or the exact operation capability;
     delegated grants may never exceed the grantor's effective capabilities.
"""
from __future__ import annotations

import hashlib
import asyncio
import logging
import os
import secrets
import urllib.parse
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from apps.api.file_responses import (
    EntitySnapshotFileResponse,
    entity_filesystem_read_boundary,
)
from apps.api.middleware.rate_limit import RateLimiter, client_ip
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
from packages.core.permissions import effective_user_has_permission, Permission
from packages.core.services.document_access import (
    document_is_owned_by_deleted_workspace,
    document_grant_capabilities_for_user,
    document_grants_for_user,
    effective_document_folder_policy,
    lock_folder_policy_rows,
    lock_workspace_policy_rows,
    resolve_document_policy_lock_scope,
    user_can_read_document,
    user_has_document_capability,
)
from packages.core.services.entity_fs import get_entity_root, resolve_path
from packages.core.services.resource_grant_policy import (
    ResourceGrantPolicyError,
    ResourceGrantPolicyFactory,
    revoke_resource_grant_family,
    upsert_manual_resource_grant,
)
from packages.core.services.share_access import (
    ShareAccessError,
    create_otp_challenge_record,
    create_share_view_session,
    discard_otp_challenge,
    share_requires_verification,
    verify_otp_challenge,
    verify_share_access,
    verify_share_view_session,
)

router = APIRouter(prefix="/api/v1/documents", tags=["document-permissions"])
logger = logging.getLogger(__name__)

_SHARE_ACCESS_COOKIE = "manor_share_access"
_SHARE_VIEW_COOKIE = "manor_share_view"
_SHARE_VIEW_SESSION_SECONDS = 60 * 60
_SHARE_OTP_LIMITER = RateLimiter()


# ── Shared helpers ────────────────────────────────────────────────────────


async def _load_doc(
    db: AsyncSession,
    doc_id: str,
    entity_id: str,
    *,
    for_update: bool = False,
) -> Document:
    """Fetch a document scoped to the actor's entity; 404 otherwise."""
    stmt = select(Document).where(
        Document.id == doc_id,
        Document.entity_id == entity_id,
    )
    if not for_update:
        doc = (await db.execute(stmt)).scalar_one_or_none()
        if not doc or await document_is_owned_by_deleted_workspace(db, doc):
            raise CodedError(
                404,
                code="permissions.error.doc.not_found",
                message="Document not found",
            )
        return doc

    for _attempt in range(3):
        savepoint = await db.begin_nested()
        try:
            doc = (await db.execute(stmt)).scalar_one_or_none()
            if not doc:
                raise CodedError(
                    404,
                    code="permissions.error.doc.not_found",
                    message="Document not found",
                )
            scope = await resolve_document_policy_lock_scope(db, doc)
            workspaces = await lock_workspace_policy_rows(
                db,
                entity_id=entity_id,
                workspace_ids=scope.workspace_ids,
                read=True,
            )
            if any(workspace.deleted_at is not None for workspace in workspaces):
                raise CodedError(
                    404,
                    code="permissions.error.doc.not_found",
                    message="Document not found",
                )
            await lock_folder_policy_rows(
                db,
                entity_id=entity_id,
                folder_id=doc.folder_id,
                folder_ids=scope.folder_ids,
                read=True,
            )
            doc = (await db.execute(
                stmt.with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if not doc:
                raise CodedError(
                    404,
                    code="permissions.error.doc.not_found",
                    message="Document not found",
                )
            if await resolve_document_policy_lock_scope(db, doc) != scope:
                await savepoint.rollback()
                continue
            await savepoint.commit()
            return doc
        except Exception:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    raise CodedError(
        409,
        code="permissions.error.doc.changed_during_request",
        message="Document changed during the request; please retry",
    )


async def _is_owner_or_admin(
    db: AsyncSession,
    doc: Document,
    user: User,
) -> bool:
    """P3 minimum gate: doc owner OR tenant admin can mutate ACL."""
    if doc.owner_id and doc.owner_id == user.id:
        return True
    return await effective_user_has_permission(db, user, Permission.ADMIN_SETTINGS)


async def _can_create_internal_grant(
    db: AsyncSession,
    doc: Document,
    user: User,
) -> bool:
    if await _is_owner_or_admin(db, doc, user):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities={Capability.GRANT_ACCESS, Capability.SHARE_INTERNAL},
    )


async def _can_administer_internal_acl(
    db: AsyncSession,
    doc: Document,
    user: User,
) -> bool:
    if await _is_owner_or_admin(db, doc, user):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities={Capability.GRANT_ACCESS},
    )


async def _can_manage_external_share(db: AsyncSession, doc: Document, user: User) -> bool:
    if await _is_owner_or_admin(db, doc, user):
        return True
    return await user_has_document_capability(
        db,
        document=doc,
        user_id=user.id,
        capabilities={Capability.SHARE_EXTERNAL},
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
        async with db.begin_nested():
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
        logger.warning("Failed to append document access log", exc_info=True)


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
    if not await _can_administer_internal_acl(db, doc, user):
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    owner_or_admin = await _is_owner_or_admin(db, doc, user)
    if not owner_or_admin and not await _can_create_internal_grant(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.grant_owner_only",
            message="Only the document owner/admin or a user with internal share access can grant access",
        )
    if req.subject_type != SubjectType.USER:
        raise HTTPException(
            400,
            "Document grants currently support only user subjects",
        )

    grantor_capabilities = None
    if not owner_or_admin:
        grantor_capabilities = await document_grant_capabilities_for_user(
            db,
            document=doc,
            user_id=user.id,
        )
    expires_at = req.expires_at
    try:
        policy = ResourceGrantPolicyFactory.create(
            ResourceType.DOCUMENT
        )
        capabilities = policy.validate(
            req.capabilities,
            grantor_capabilities=grantor_capabilities,
        )
        if not owner_or_admin:
            expires_at = policy.delegated_expiry(
                capabilities,
                grants=await document_grants_for_user(db, document=doc, user_id=user.id),
                expires_at=expires_at,
            )
    except ResourceGrantPolicyError as exc:
        raise HTTPException(
            403 if grantor_capabilities is not None else 400,
            str(exc),
        ) from exc

    # Invariant 7: agents can never receive share_external on confidential+
    # (this endpoint serves human grants but defense-in-depth)
    if (
        Capability.SHARE_EXTERNAL in capabilities
        and getattr(doc, "classification", None)
        in {Classification.CONFIDENTIAL, Classification.RESTRICTED}
    ):
        raise HTTPException(
            400,
            "share_external requires the per-share approval path on confidential+ docs",
        )

    subject_id = req.subject_id
    existing_subject_ids = [subject_id]
    if req.subject_type == SubjectType.USER:
        subject_id, _, _ = await _resolve_user_grant_subject(
            db,
            entity_id=user.entity_id,
            subject_id=req.subject_id,
        )
        existing_subject_ids = list(dict.fromkeys([subject_id, req.subject_id]))

    grant = await upsert_manual_resource_grant(
        db,
        entity_id=user.entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=doc.id,
        subject_type=req.subject_type,
        subject_id=subject_id,
        equivalent_subject_ids=existing_subject_ids,
        capabilities=capabilities,
        granted_by=user.id,
        expires_at=expires_at,
    )
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _can_administer_internal_acl(db, doc, user):
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
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not grant:
        raise CodedError(
            404,
            code="permissions.error.doc.grant_not_found",
            message="Grant not found",
        )
    equivalent_subject_ids = [grant.subject_id]
    if grant.subject_type == SubjectType.USER:
        canonical_subject_id, _subject_user, subject_staff = (
            await _resolve_user_grant_subject(
                db,
                entity_id=user.entity_id,
                subject_id=grant.subject_id,
            )
        )
        equivalent_subject_ids.extend([
            canonical_subject_id,
            subject_staff.id if subject_staff else canonical_subject_id,
        ])
    await revoke_resource_grant_family(
        db,
        target=grant,
        equivalent_subject_ids=equivalent_subject_ids,
        revoked_by=user.id,
    )
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
    expires_in_days: int | None = Field(default=7, ge=1, le=90)
    watermark: bool = True
    require_otp: bool = False
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
    if Capability.VIEW not in req.capabilities:
        raise HTTPException(400, "External shares must include the view capability")
    if req.audience_type == "anonymous":
        return "anonymous"
    if req.audience_type == "email":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for email audience")
        if not req.require_otp:
            raise HTTPException(400, "Email audience shares require OTP verification")
        return f"email:{req.audience_value.strip().lower()}"
    if req.audience_type == "domain":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for domain audience")
        if not req.require_otp:
            raise HTTPException(400, "Domain audience shares require OTP verification")
        return f"domain:{req.audience_value.strip().lower().lstrip('@')}"
    raise HTTPException(400, f"Invalid audience_type: {req.audience_type}")


async def _materialize_share(
    db: AsyncSession,
    *,
    doc: Document,
    creator_user_id: str,
    req: CreateShareRequest,
    audience: str,
    request: Request,
    approved_external_share: bool = False,
    delegated_expires_at: datetime | None = None,
) -> tuple[Share, str, str]:
    """Persist a new Share row + write the audit entry; return (share, raw_token, url).

    Caller is responsible for ``await db.commit()`` and ``await db.refresh(share)``.
    """
    effective_classification, _visibility, _client_visible = (
        await effective_document_folder_policy(db, doc)
    )
    if effective_classification == Classification.RESTRICTED:
        raise CodedError(
            400,
            code="permissions.error.doc.restricted_no_external",
            message="Restricted documents cannot be shared externally",
        )
    if (
        effective_classification == Classification.CONFIDENTIAL
        and not approved_external_share
    ):
        raise CodedError(
            409,
            code="permissions.error.doc.confidential_needs_approval",
            message="Confidential documents require approval",
        )
    if effective_classification == Classification.CONFIDENTIAL and (
        req.audience_type == "anonymous" or not req.require_otp
    ):
        raise HTTPException(
            400,
            "Confidential external shares require an OTP-protected email or domain audience",
        )
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)
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
        created_at=now,
        expires_at=delegated_expires_at or (
            now + timedelta(days=req.expires_in_days) if req.expires_in_days is not None else None
        ),
        max_uses=None,
        use_count=0,
        status="active",
        metadata_={
            "classification_at_creation": str(effective_classification or ""),
            "approved_external_share": approved_external_share,
        },
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _can_manage_external_share(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.share_external_owner_only",
            message="Only the document owner/admin or a user with external share access can share externally",
        )

    cls, _visibility, _client_visible = await effective_document_folder_policy(
        db, doc
    )
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
    delegated_expires_at = None
    if not await _is_owner_or_admin(db, doc, user):
        policy = ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT)
        grants = await document_grants_for_user(db, document=doc, user_id=user.id)
        implicit_view = await user_can_read_document(
            db, doc, entity_id=user.entity_id, user_id=user.id, role=user.role,
            allow_redacted=False, include_grants=False,
        )
        try:
            delegated_expires_at = policy.delegated_expiry(
                req.capabilities,
                grants=grants,
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)
                    if req.expires_in_days is not None else None
                ),
                authority_capabilities=(Capability.SHARE_EXTERNAL,),
                clamp_expiry="expires_in_days" not in req.model_fields_set,
                implicit_capabilities=(Capability.VIEW,) if implicit_view else (),
            )
        except ResourceGrantPolicyError as exc:
            raise HTTPException(403, str(exc)) from exc
    share, raw_token, url = await _materialize_share(
        db, doc=doc, creator_user_id=user.id,
        req=req, audience=audience, request=request,
        delegated_expires_at=delegated_expires_at,
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
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

_PUBLIC_CLASS_RANK = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.RESTRICTED: 3,
}


async def _load_public_document_share(
    db: AsyncSession,
    token: str,
    *,
    share_for_update: bool = False,
    allow_use_limit_reached: bool = False,
) -> tuple[Share, Document, datetime]:
    token_hash = _hash_token(token)
    for _attempt in range(3):
        savepoint = await db.begin_nested()
        try:
            share_scope = (await db.execute(
                select(Share).where(
                    Share.token_hash == token_hash,
                    Share.status == "active",
                )
            )).scalar_one_or_none()
            if not share_scope:
                raise CodedError(
                    404,
                    code="permissions.error.share.not_found_or_revoked",
                    message="Share not found or revoked",
                )
            if share_scope.resource_type != ResourceType.DOCUMENT:
                raise HTTPException(404, "Not a document share")

            doc_stmt = select(Document).where(
                Document.id == share_scope.resource_id,
                Document.entity_id == share_scope.entity_id,
            )
            doc = (await db.execute(doc_stmt)).scalar_one_or_none()
            if not doc:
                raise HTTPException(404, "Underlying document not found")
            scope = await resolve_document_policy_lock_scope(db, doc)
            workspaces = await lock_workspace_policy_rows(
                db,
                entity_id=doc.entity_id,
                workspace_ids=scope.workspace_ids,
                read=True,
            )
            if any(workspace.deleted_at is not None for workspace in workspaces):
                raise CodedError(
                    410,
                    code="permissions.error.share.revoked_by_workspace_lifecycle",
                    message="Share is no longer available",
                )
            await lock_folder_policy_rows(
                db,
                entity_id=doc.entity_id,
                folder_id=doc.folder_id,
                folder_ids=scope.folder_ids,
                read=True,
            )
            doc = (await db.execute(
                doc_stmt.with_for_update(read=True).execution_options(
                    populate_existing=True
                )
            )).scalar_one_or_none()
            if not doc:
                raise HTTPException(404, "Underlying document not found")
            if await resolve_document_policy_lock_scope(db, doc) != scope:
                await savepoint.rollback()
                continue

            share = (await db.execute(
                select(Share)
                .where(
                    Share.id == share_scope.id,
                    Share.token_hash == token_hash,
                    Share.status == "active",
                    Share.resource_type == ResourceType.DOCUMENT,
                    Share.resource_id == doc.id,
                    Share.entity_id == doc.entity_id,
                )
                .with_for_update(read=not share_for_update)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if not share:
                raise CodedError(
                    404,
                    code="permissions.error.share.not_found_or_revoked",
                    message="Share not found or revoked",
                )
            now = datetime.now(timezone.utc)
            if share.expires_at and share.expires_at < now:
                raise CodedError(410, code="permissions.error.share.expired", message="Share link has expired")
            if (
                share.max_uses is not None
                and (share.use_count or 0) >= share.max_uses
                and not allow_use_limit_reached
            ):
                raise CodedError(
                    410,
                    code="permissions.error.share.use_limit_reached",
                    message="Share link reached use limit",
                )
            if Capability.VIEW not in set(share.capabilities or []):
                raise CodedError(403, code="permissions.error.share.view_not_allowed", message="Share does not allow viewing")
            if bool(getattr(doc, "is_trashed", False)):
                raise HTTPException(404, "Underlying document not found")
            if getattr(doc, "quarantine_status", None) in {"quarantined", "rejected"}:
                raise HTTPException(404, "Underlying document not found")
            current_classification, _effective_visibility, _client_visible = (
                await effective_document_folder_policy(db, doc)
            )
            if current_classification == Classification.RESTRICTED:
                raise CodedError(410, code="permissions.error.share.revoked_by_policy", message="Share is no longer available")
            metadata = dict(getattr(share, "metadata_", {}) or {})
            created_classification = metadata.get("classification_at_creation")
            if created_classification in _PUBLIC_CLASS_RANK and (
                _PUBLIC_CLASS_RANK.get(current_classification, 1)
                > _PUBLIC_CLASS_RANK[created_classification]
            ):
                raise CodedError(410, code="permissions.error.share.revoked_by_policy", message="Share is no longer available")
            if current_classification == Classification.CONFIDENTIAL and not metadata.get(
                "approved_external_share"
            ):
                raise CodedError(410, code="permissions.error.share.revoked_by_policy", message="Share is no longer available")
            await savepoint.commit()
            return share, doc, now
        except Exception:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    raise CodedError(
        409,
        code="permissions.error.share.resource_changed",
        message="Shared document changed during the request; please retry",
    )


async def _acquire_public_document_file_boundary(
    db: AsyncSession,
    token: str,
    *,
    expected_entity_id: str,
    share_for_update: bool = False,
) -> tuple[AbstractAsyncContextManager[None], Share, Document, datetime]:
    """Authorize again under the filesystem read lock used by the response."""
    await db.rollback()
    for _attempt in range(3):
        read_boundary = entity_filesystem_read_boundary(
            get_entity_root(expected_entity_id)
        )
        await read_boundary.__aenter__()
        try:
            share, doc, now = await _load_public_document_share(
                db,
                token,
                share_for_update=share_for_update,
                allow_use_limit_reached=True,
            )
            if str(doc.entity_id) != expected_entity_id:
                expected_entity_id = str(doc.entity_id)
                await db.rollback()
                await read_boundary.__aexit__(None, None, None)
                continue
            return read_boundary, share, doc, now
        except BaseException:
            await read_boundary.__aexit__(None, None, None)
            raise

    raise CodedError(
        409,
        code="permissions.error.share.resource_changed",
        message="Shared document changed during the request; please retry",
    )


def _require_public_share_access(share: Share, access_token: str | None) -> str | None:
    try:
        return verify_share_access(share, access_token)
    except ShareAccessError as exc:
        raise CodedError(
            401,
            code="permissions.error.share.verification_required",
            message=str(exc),
        ) from exc


def _require_public_share_view_session(share: Share, session_token: str | None) -> None:
    if share.max_uses is None:
        return
    if not verify_share_view_session(share, session_token):
        raise CodedError(
            410,
            code="permissions.error.share.use_limit_reached",
            message="Share link reached use limit",
        )


def _require_public_share_download_access(
    share: Share,
    *,
    request: Request,
    access_token: str | None,
) -> str | None:
    verified_email = _require_public_share_access(
        share,
        access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
    )
    _require_public_share_view_session(
        share,
        request.cookies.get(_SHARE_VIEW_COOKIE),
    )
    if "download" not in set(share.capabilities or []) or not getattr(
        share,
        "allow_download",
        False,
    ):
        raise CodedError(
            403,
            code="permissions.error.share.download_not_allowed",
            message="This share link does not allow downloading the file",
        )
    return verified_email


def _require_public_document_file_path(
    doc: Document,
    *,
    unavailable_message: str,
) -> str:
    if not getattr(doc, "fs_path", None):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message=unavailable_message,
        )
    full_path = resolve_path(doc.entity_id, str(doc.fs_path))
    if not full_path:
        raise HTTPException(403, "Access denied")
    if not os.path.isfile(full_path):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message=unavailable_message,
        )
    return full_path


async def _load_shared_preview_source(
    db: AsyncSession,
    token: str,
    request: Request,
    access_token: str | None,
    expected_kind: Literal["docx", "pptx"],
) -> tuple[Document, str]:
    """Resolve one token-authorized Office source without exposing its identity."""
    from apps.api.routers import documents
    from packages.core.config import get_settings

    share, doc, _now = await _load_public_document_share(
        db,
        token,
        allow_use_limit_reached=True,
    )
    _require_public_share_access(
        share,
        access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
    )
    _require_public_share_view_session(share, request.cookies.get(_SHARE_VIEW_COOKIE))
    if not get_settings().MANOR_FS_ENABLED:
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message="File is not available for preview",
        )
    if expected_kind == "docx" and not documents._is_docx_document(doc):
        raise HTTPException(400, "Not a Word document")
    if expected_kind == "pptx" and not documents._is_pptx_document(doc):
        raise HTTPException(400, "Not a presentation file")
    source_path = documents._document_full_path(doc, str(doc.entity_id))
    if not source_path or not os.path.isfile(source_path):
        raise CodedError(
            404,
            code="permissions.error.share.file_unavailable",
            message="File is not available for preview",
        )
    return doc, source_path


async def _render_shared_office_preview(
    token: str,
    request: Request,
    access_token: str | None,
    db: AsyncSession,
    expected_kind: Literal["docx", "pptx"],
) -> tuple[list[str], Document]:
    """Render a share-scoped Office source with the internal cache contract."""
    from apps.api.routers import documents
    from packages.core.services.slide_renderer import (
        OfficeRenderLimitError,
        render_document_pages,
        render_slides,
    )

    doc, source_path = await _load_shared_preview_source(
        db, token, request, access_token, expected_kind,
    )
    entity_id = str(doc.entity_id)
    cache_name = ".document-page-cache" if expected_kind == "docx" else ".slide-cache"
    cache_dir = os.path.join(documents.settings.MANOR_FS_ROOT, entity_id, cache_name, doc.id)

    # Do not occupy a database transaction while waiting for the entity lock.
    await db.rollback()
    async with documents._document_filesystem_mutation(entity_id):
        doc, source_path = await _load_shared_preview_source(
            db, token, request, access_token, expected_kind,
        )
        source_snapshot = await asyncio.to_thread(
            documents._capture_document_source_snapshot,
            source_path,
        )
        document_ext = documents._document_ext(doc)
        source_ext = (
            f".{document_ext}" if document_ext in {"doc", "docx", "wps"} else ".docx"
        ) if expected_kind == "docx" else documents._presentation_source_format(doc)
        await db.rollback()

    try:
        paths = await (
            render_document_pages(source_path, cache_dir, source_ext=source_ext)
            if expected_kind == "docx"
            else render_slides(source_path, cache_dir, source_ext=source_ext)
        )
    except OfficeRenderLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except Exception as exc:
        logger.warning("Shared %s preview rendering failed: %s", expected_kind, exc)
        raise HTTPException(502, "Office preview rendering failed") from exc

    async with documents._document_filesystem_mutation(entity_id):
        current_doc, current_source_path = await _load_shared_preview_source(
            db, token, request, access_token, expected_kind,
        )
        source_matches = await asyncio.to_thread(
            documents._document_source_matches,
            source_snapshot,
            current_source_path,
        )
        if not source_matches:
            raise HTTPException(
                409,
                {
                    "code": "document_source_changed",
                    "message": "Document changed while its preview was being prepared",
                },
            )
        await asyncio.to_thread(
            documents._publish_rendered_preview_version,
            cache_dir,
            paths,
            source_snapshot.path,
        )
        await db.rollback()
    return paths, current_doc


class ShareOtpRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ShareOtpVerifyRequest(ShareOtpRequest):
    code: str = Field(min_length=6, max_length=6)


async def _limit_share_otp(request: Request, share_id: str, purpose: str) -> None:
    max_requests = 5 if purpose == "request" else 10
    window_seconds = 10 * 60
    result = await _SHARE_OTP_LIMITER.check(
        f"share-otp:{purpose}:{share_id}:{client_ip(request)}",
        max_requests,
        window_seconds,
    )
    if not result.allowed:
        raise HTTPException(
            429,
            "Too many verification attempts. Please try again later",
            headers={"Retry-After": str(result.retry_after or window_seconds)},
        )


@public_router.post("/{token}/request-otp")
async def request_shared_doc_otp(
    token: str,
    req: ShareOtpRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    share, _doc, _now = await _load_public_document_share(db, token)
    share_id = str(share.id)
    await db.rollback()
    await _limit_share_otp(request, share_id, "request")
    share, _doc, _now = await _load_public_document_share(
        db,
        token,
        share_for_update=True,
    )
    if not share_requires_verification(share):
        raise HTTPException(400, "This share does not require email verification")
    try:
        challenge = create_otp_challenge_record(share, req.email)
    except ShareAccessError:
        # Keep recipient membership private even from someone holding the
        # opaque share URL. The response is intentionally indistinguishable.
        await db.rollback()
        return {"status": "sent"}
    from packages.core.services.email_service import send_share_verification_email

    # The challenge must be durable before external provider I/O, and no
    # lifecycle/resource/share row lock may be held while email is sent.
    await db.commit()
    if not await send_share_verification_email(challenge.email, challenge.code):
        failed_share = (await db.execute(
            select(Share)
            .where(
                Share.id == share_id,
                Share.entity_id == share.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if failed_share is not None:
            discard_otp_challenge(
                failed_share,
                challenge.email,
                challenge.challenge_id,
            )
            await db.commit()
        raise HTTPException(503, "Verification email could not be sent")
    return {"status": "sent"}


@public_router.post("/{token}/verify-otp")
async def verify_shared_doc_otp(
    token: str,
    req: ShareOtpVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    share, _doc, _now = await _load_public_document_share(db, token)
    share_id = str(share.id)
    await db.rollback()
    await _limit_share_otp(request, share_id, "verify")
    share, _doc, _now = await _load_public_document_share(
        db,
        token,
        share_for_update=True,
    )
    try:
        access_token = verify_otp_challenge(share, req.email, req.code)
    except ShareAccessError as exc:
        await db.commit()
        raise HTTPException(400, str(exc)) from exc
    response.set_cookie(
        key=_SHARE_ACCESS_COOKIE,
        value=access_token,
        max_age=3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path=f"/api/v1/shared-doc/{token}",
    )
    await db.commit()
    return {"access_token": access_token, "expires_in": 3600}


@public_router.get("/{token}", response_model=SharedDocResponse)
async def view_shared_doc(
    token: str,
    request: Request,
    response: Response,
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated. Look up a share by hashed token; enforce expiry &
    use-count; bump counters; write access log."""
    share, doc, now = await _load_public_document_share(
        db,
        token,
        share_for_update=True,
    )
    verified_email = _require_public_share_access(
        share,
        access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
    )
    effective_classification, _visibility, _client_visible = (
        await effective_document_folder_policy(db, doc)
    )

    share.use_count = (share.use_count or 0) + 1
    share.last_used_at = now

    await _write_access_log(
        db, doc=doc, actor_type="share_token", actor_id=verified_email or share.id,
        action="share_use", share_id=share.id, request=request,
    )

    if share.max_uses is not None:
        response.set_cookie(
            key=_SHARE_VIEW_COOKIE,
            value=create_share_view_session(share=share, use_count=share.use_count),
            max_age=_SHARE_VIEW_SESSION_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path=f"/api/v1/shared-doc/{token}",
        )
    await db.commit()
    return SharedDocResponse(
        document_id=doc.id,
        name=doc.name,
        classification=effective_classification,
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
    access_token: str | None = Query(None),
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
    scoped_share, scoped_doc, _now = await _load_public_document_share(
        db,
        token,
        allow_use_limit_reached=True,
    )
    _require_public_share_access(
        scoped_share,
        access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
    )
    _require_public_share_view_session(
        scoped_share,
        request.cookies.get(_SHARE_VIEW_COOKIE),
    )
    _require_public_document_file_path(
        scoped_doc,
        unavailable_message="File is not available for preview",
    )
    read_boundary, share, doc, _now = await _acquire_public_document_file_boundary(
        db,
        token,
        expected_entity_id=str(scoped_doc.entity_id),
    )
    try:
        _require_public_share_access(
            share,
            access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
        )
        _require_public_share_view_session(
            share,
            request.cookies.get(_SHARE_VIEW_COOKIE),
        )

        full_path = _require_public_document_file_path(
            doc,
            unavailable_message="File is not available for preview",
        )

        media_type = doc.mime_type or "application/octet-stream"
        await db.rollback()
        return EntitySnapshotFileResponse(
            path=full_path,
            media_type=media_type,
            headers={
                "Content-Disposition": "inline",
                # Public share content is sensitive-ish; don't let shared
                # proxies cache it. Browser may still keep it for the tab.
                "Cache-Control": "private, no-store",
            },
            read_boundary=read_boundary,
        )
    except BaseException:
        await read_boundary.__aexit__(None, None, None)
        raise


def _shared_preview_response(rendered_file, *, filename: str) -> StreamingResponse:
    from apps.api.routers import documents

    return StreamingResponse(
        documents._stream_open_file(rendered_file),
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"inline; filename*=UTF-8''{urllib.parse.quote(filename)}",
        },
    )


@public_router.get("/{token}/preview/pages")
async def get_shared_document_page_images(
    token: str,
    request: Request,
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    paths, _doc = await _render_shared_office_preview(token, request, access_token, db, "docx")
    from apps.api.routers import documents

    version = Path(paths[0]).parent.name if paths else ""
    pages = []
    for index, path in enumerate(paths):
        dimensions = documents._png_dimensions(path)
        pages.append({
            "index": index,
            "url": f"/api/v1/shared-doc/{token}/preview/pages/{index}?version={version}",
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
        })
    return {"pages": pages, "total": len(paths), "version": version}


@public_router.get("/{token}/preview/pages/{page_index}")
async def get_shared_document_page_image(
    token: str,
    page_index: int,
    request: Request,
    version: str = Query(pattern=r"^[0-9a-f]{16}$"),
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from apps.api.routers import documents
    from packages.core.services.slide_renderer import open_cached_document_page

    doc, source_path = await _load_shared_preview_source(db, token, request, access_token, "docx")
    entity_id = str(doc.entity_id)
    cache_dir = os.path.join(documents.settings.MANOR_FS_ROOT, entity_id, ".document-page-cache", doc.id)
    await db.rollback()
    try:
        async with documents._document_filesystem_mutation(entity_id):
            doc, source_path = await _load_shared_preview_source(db, token, request, access_token, "docx")
            rendered_file = await open_cached_document_page(cache_dir, version, page_index, source_path)
            await db.rollback()
    except FileNotFoundError as exc:
        raise HTTPException(404, "Word preview version not found") from exc
    except IndexError as exc:
        raise HTTPException(404, "Page index out of range") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Shared Word preview image read failed: %s", exc)
        raise HTTPException(502, "Word page image could not be read") from exc
    return _shared_preview_response(rendered_file, filename=f"page-{page_index + 1}.png")


@public_router.get("/{token}/preview/slides")
async def get_shared_slide_images(
    token: str,
    request: Request,
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    paths, _doc = await _render_shared_office_preview(token, request, access_token, db, "pptx")
    from apps.api.routers import documents

    version = Path(paths[0]).parent.name if paths else ""
    slides = []
    for index, path in enumerate(paths):
        dimensions = documents._png_dimensions(path)
        slides.append({
            "index": index,
            "url": f"/api/v1/shared-doc/{token}/preview/slides/{index}?version={version}",
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
        })
    return {"slides": slides, "total": len(paths), "version": version}


@public_router.get("/{token}/preview/slides/{slide_index}")
async def get_shared_slide_image(
    token: str,
    slide_index: int,
    request: Request,
    version: str = Query(pattern=r"^[0-9a-f]{16}$"),
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from apps.api.routers import documents
    from packages.core.services.slide_renderer import open_cached_slide

    doc, source_path = await _load_shared_preview_source(db, token, request, access_token, "pptx")
    entity_id = str(doc.entity_id)
    cache_dir = os.path.join(documents.settings.MANOR_FS_ROOT, entity_id, ".slide-cache", doc.id)
    await db.rollback()
    try:
        async with documents._document_filesystem_mutation(entity_id):
            doc, source_path = await _load_shared_preview_source(db, token, request, access_token, "pptx")
            rendered_file = await open_cached_slide(cache_dir, version, slide_index, source_path)
            await db.rollback()
    except FileNotFoundError as exc:
        raise HTTPException(404, "Slide preview version not found") from exc
    except IndexError as exc:
        raise HTTPException(404, "Slide index out of range") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Shared presentation preview image read failed: %s", exc)
        raise HTTPException(502, "Slide image could not be read") from exc
    return _shared_preview_response(rendered_file, filename=f"slide-{slide_index + 1}.png")


@public_router.get("/{token}/download")
async def download_shared_doc(
    token: str,
    request: Request,
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Stream the actual file bytes for a public document share.

    Unauthenticated — the opaque share token is the entitlement. Same
    validity gates as ``view_shared_doc`` (active / not expired / under
    use limit / matches DOCUMENT resource type) PLUS the share must have
    been created with the ``download`` capability AND ``allow_download``
    must be true (the latter is the per-share override the dialog sets
    when the anonymous role is "downloader").

    Limited shares reuse the counted public-page view, so a preview and its
    allowed download consume one use together. Unlimited shares retain the
    existing per-download access log and counter behavior.
    """
    scoped_share, scoped_doc, _now = await _load_public_document_share(
        db,
        token,
        allow_use_limit_reached=True,
    )
    _require_public_share_download_access(
        scoped_share,
        request=request,
        access_token=access_token,
    )
    _require_public_document_file_path(
        scoped_doc,
        unavailable_message="File is not available for download",
    )
    read_boundary, share, doc, now = await _acquire_public_document_file_boundary(
        db,
        token,
        expected_entity_id=str(scoped_doc.entity_id),
        share_for_update=True,
    )
    try:
        verified_email = _require_public_share_download_access(
            share,
            request=request,
            access_token=access_token,
        )

        full_path = _require_public_document_file_path(
            doc,
            unavailable_message="File is not available for download",
        )

        if share.max_uses is None:
            share.use_count = (share.use_count or 0) + 1
            share.last_used_at = now
            await _write_access_log(
                db,
                doc=doc,
                actor_type="share_token",
                actor_id=verified_email or share.id,
                action="share_download",
                share_id=share.id,
                request=request,
            )
        filename = doc.name
        media_type = doc.mime_type or "application/octet-stream"
        await db.commit()

        return EntitySnapshotFileResponse(
            path=full_path,
            media_type=media_type,
            filename=filename,
            read_boundary=read_boundary,
        )
    except BaseException:
        await read_boundary.__aexit__(None, None, None)
        raise


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
    if not await _is_owner_or_admin(db, doc, user):
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _is_owner_or_admin(db, doc, user):
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
            ).with_for_update()
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
        requested_capabilities = list(pending.requested_capabilities or [])
        proposed_capabilities = (
            requested_capabilities
            if req.approved_capabilities is None
            else req.approved_capabilities
        )
        try:
            capabilities = ResourceGrantPolicyFactory.create(
                ResourceType.DOCUMENT
            ).validate(
                proposed_capabilities,
                requested_capabilities=requested_capabilities,
            )
        except ResourceGrantPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc

        canonical_subject_id, _subject_user, subject_staff = (
            await _resolve_user_grant_subject(
                db,
                entity_id=user.entity_id,
                subject_id=pending.requester_user_id,
            )
        )
        equivalent_subject_ids = [
            pending.requester_user_id,
            canonical_subject_id,
        ]
        if subject_staff:
            equivalent_subject_ids.append(subject_staff.id)
        grant = await upsert_manual_resource_grant(
            db,
            entity_id=user.entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc.id,
            subject_type=SubjectType.USER,
            subject_id=canonical_subject_id,
            equivalent_subject_ids=equivalent_subject_ids,
            capabilities=capabilities,
            granted_by=user.id,
            expires_at=req.expires_at,
            merge_capabilities=True,
            metadata={"last_access_request_id": pending.id},
        )
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
    if not await _is_owner_or_admin(db, doc, user):
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
    expires_in_days: int | None = Field(default=7, ge=1, le=90)
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _is_owner_or_admin(db, doc, user):
        raise HTTPException(
            403, "Only the document owner or an admin can request a share approval",
        )

    cls, _visibility, _client_visible = await effective_document_folder_policy(
        db, doc
    )
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
    if req.audience_type == "anonymous" or not req.require_otp:
        raise HTTPException(
            400,
            "Confidential external shares require an OTP-protected email or domain audience",
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
    if not await _is_owner_or_admin(db, doc, user):
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
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await effective_user_has_permission(db, user, Permission.ADMIN_SETTINGS):
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
            ).with_for_update()
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
    cls, _visibility, _client_visible = await effective_document_folder_policy(
        db, doc
    )
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
        if cls == Classification.CONFIDENTIAL and (
            proxy.audience_type == "anonymous" or not proxy.require_otp
        ):
            raise HTTPException(
                400,
                "Confidential external shares require an OTP-protected email or domain audience",
            )
        share, raw_token, share_url = await _materialize_share(
            db, doc=doc,
            # The share is owned/created by the original requester, not the
            # approving admin — that way `created_by` on `shares` lines up
            # with the user who'll consume the link.
            creator_user_id=pending.requester_user_id,
            req=proxy, audience=audience, request=request,
            approved_external_share=True,
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
# Note: classify endpoints live in permissions_v1.py,
# under /api/v1/permissions/documents/:id/classify.
# ─────────────────────────────────────────────────────────────────────────
