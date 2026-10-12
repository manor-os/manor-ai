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
  2. ACL writes require resource ownership, global management permission, or
     the exact operation capability;
     delegated grants may never exceed the grantor's effective capabilities.
"""
from __future__ import annotations

import hashlib
import asyncio
import base64
import binascii
import html
import io
import json
import logging
import os
import re
import secrets
import textwrap
import unicodedata
import urllib.parse
import zlib
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictBool, StrictStr
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
from packages.core.models.user import Entity, User, UserMembership
from packages.core.permissions import effective_user_has_permission, Permission
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    resolve_current_user_actor,
)
from packages.core.services.document_access import (
    document_entity_is_unavailable,
    document_grant_capabilities_for_user,
    document_grants_for_user,
    effective_document_folder_policy,
    user_owns_workspace_document,
    lock_folder_policy_rows,
    lock_workspace_policy_rows,
    resolve_document_policy_lock_scope,
    user_can_read_document,
    user_has_document_capability,
)
from packages.core.services.entity_fs import get_entity_root, resolve_path
from packages.core.services.pdf_signature_fields import (
    SignatureField,
    stamp_pdf_fields,
    validate_signature_fields,
)
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
    share_recipient_emails,
    share_requires_verification,
    verify_otp_challenge,
    verify_share_access,
    verify_share_view_session,
)
from packages.core.services.connected_email_accounts import (
    ConnectedEmailAccountsUnavailable,
    available_email_senders,
    default_email_sender,
    send_email_from_account,
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
        if not doc or await document_entity_is_unavailable(db, doc):
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
            await lock_workspace_policy_rows(
                db,
                entity_id=entity_id,
                workspace_ids=scope.workspace_ids,
                read=True,
            )
            if await document_entity_is_unavailable(db, doc):
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
    """Document owner or an explicitly configured global manager."""
    if doc.owner_id and doc.owner_id == user.id:
        return True
    if await user_owns_workspace_document(db, document=doc, user_id=user.id):
        return True
    return await effective_user_has_permission(db, user, Permission.DOCS_MANAGE_ALL)


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


async def _can_manage_full_document_access(db: AsyncSession, doc: Document, user: User) -> bool:
    if await _is_owner_or_admin(db, doc, user):
        return True
    capabilities = await document_grant_capabilities_for_user(db, document=doc, user_id=user.id)
    return ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT).allowed_capabilities.issubset(capabilities)


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
            message="Only the document owner or a user with global management or grant access can view grants",
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
            message="Only the document owner or a user with global management or internal share access can grant access",
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

    # A complete human Co-owner grant retains the same classification gates
    # as ownership. Narrow external-share grants remain disallowed here.
    if (
        Capability.SHARE_EXTERNAL in capabilities
        and getattr(doc, "classification", None)
        in {Classification.CONFIDENTIAL, Classification.RESTRICTED}
        and not policy.allowed_capabilities.issubset(capabilities)
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
            message="Only the document owner or a user with global management or grant access can revoke access",
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


_SIGNATURE_REQUEST_KIND = "signature_request"
_SIGNATURE_PENDING = "pending"
_SIGNATURE_COMPLETED = "completed"
_SIGNATURE_MAX_IMAGE_BYTES = 1024 * 1024
_SIGNATURE_MAX_SOURCE_BYTES = 50 * 1024 * 1024


class SignaturePlacement(BaseModel):
    page: int = Field(default=1, ge=1, le=10_000)
    x: float = Field(default=0.62, ge=0, le=1)
    y: float = Field(default=0.78, ge=0, le=1)
    width: float = Field(default=0.30, gt=0, le=0.8)
    height: float = Field(default=0.12, gt=0, le=0.5)


class AdditionalSignatureRecipient(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=120)
    recipient_email: str = Field(min_length=3, max_length=320)
    recipient_emails: list[str] = Field(default_factory=list, max_length=10)
    fields: list[SignatureField] = Field(min_length=1, max_length=50)


class CreateSignatureRequest(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=120)
    recipient_email: str = Field(min_length=3, max_length=320)
    recipient_emails: list[str] = Field(default_factory=list, max_length=10)
    message: str | None = Field(default=None, max_length=2_000)
    expires_in_days: int = Field(default=7, ge=1, le=30)
    placement: SignaturePlacement = Field(default_factory=SignaturePlacement)
    fields: list[SignatureField] | None = Field(default=None, min_length=1, max_length=50)
    additional_recipients: list[AdditionalSignatureRecipient] = Field(default_factory=list, max_length=9)
    sender_integration_id: str | None = None


class SignatureRequestResponse(BaseModel):
    id: str
    recipient_name: str
    recipient_email: str
    recipient_emails: list[str] = Field(default_factory=list)
    signed_by_email: str | None = None
    message: str | None = None
    status: Literal["pending", "completed", "revoked", "expired"]
    delivery_status: Literal["pending", "sent", "failed"]
    delivery_error: str | None = None
    placement: SignaturePlacement
    fields: list[SignatureField]
    created_at: datetime | None = None
    expires_at: datetime | None = None
    viewed_at: datetime | None = None
    signed_at: datetime | None = None
    url: str | None = None
    group_id: str | None = None
    group_size: int = 1
    group_signed_count: int = 0
    group_finalized: bool = False
    group_delivery_failed_count: int = 0


class SignatureSenderOption(BaseModel):
    id: str
    email: str
    provider: Literal["email", "gmail", "outlook"]
    is_default: bool = False


class PublicSignatureRequestResponse(BaseModel):
    id: str
    document_name: str
    recipient_name: str
    recipient_email: str
    sender_name: str
    message: str | None = None
    status: Literal["pending", "completed"]
    placement: SignaturePlacement
    fields: list[SignatureField]
    expires_at: datetime | None = None
    signed_at: datetime | None = None
    group_size: int = 1
    group_signed_count: int = 0
    group_finalized: bool = False


class SubmitSignatureRequest(BaseModel):
    signature_data_url: str | None = Field(default=None, min_length=32, max_length=1_500_000)
    field_values: dict[str, StrictStr | StrictBool] | None = Field(default=None, max_length=50)
    consent: StrictBool


class SignatureCompletionResponse(BaseModel):
    status: Literal["completed"]
    signed_at: datetime
    signer_email: str
    document_sha256: str
    group_finalized: bool = True
    signed_document_id: str | None = None
    signed_document_name: str | None = None


def _signature_metadata(share: Share) -> dict:
    return dict(getattr(share, "metadata_", {}) or {})


def _is_signature_share(share: Share) -> bool:
    return _signature_metadata(share).get("kind") == _SIGNATURE_REQUEST_KIND


def _signature_fields(metadata: dict) -> list[SignatureField]:
    if "fields" in metadata:
        fields = [SignatureField.model_validate(field) for field in metadata["fields"]]
        validate_signature_fields(fields)
        return fields
    placement = SignaturePlacement.model_validate(metadata.get("placement") or {})
    return [SignatureField(id="signature", type="signature", label="Signature", required=True, **placement.model_dump())]


def _signature_fields_snapshot(metadata: dict) -> dict:
    return {
        "fields": [field.model_dump() for field in _signature_fields(metadata)],
        "legacy": "fields" not in metadata,
    }


def _timestamp_key(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_metadata_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_signature_email(value: str) -> str:
    email = value.strip().lower()
    try:
        local_part, domain = email.rsplit("@", 1)
        if not local_part.isascii():
            raise ValueError("Non-ASCII email local parts are not supported")
        email = f"{local_part}@{domain.encode('idna').decode('ascii')}"
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Enter a valid recipient email address") from exc
    if (
        len(email) > 320
        or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", email)
        or "\r" in email
        or "\n" in email
    ):
        raise HTTPException(400, "Enter a valid recipient email address")
    return email


def _validate_signature_placement(placement: SignaturePlacement) -> None:
    if placement.x + placement.width > 1 or placement.y + placement.height > 1:
        raise HTTPException(400, "Signature placement must stay within the PDF page")


def _signature_status(share: Share, *, now: datetime | None = None) -> str:
    if share.status == "revoked":
        return "revoked"
    metadata = _signature_metadata(share)
    if metadata.get("signature_status") == _SIGNATURE_COMPLETED:
        return "completed"
    current = now or datetime.now(timezone.utc)
    if share.expires_at and share.expires_at < current:
        return "expired"
    return "pending"


def _signature_response(share: Share, *, url: str | None = None) -> SignatureRequestResponse:
    metadata = _signature_metadata(share)
    placement = metadata.get("placement") if isinstance(metadata.get("placement"), dict) else {}
    audience = str(share.audience or "")
    return SignatureRequestResponse(
        id=str(share.id),
        recipient_name=str(metadata.get("recipient_name") or ""),
        recipient_email=audience.removeprefix("email:"),
        recipient_emails=share_recipient_emails(share),
        signed_by_email=metadata.get("signer_email"),
        message=metadata.get("message") if isinstance(metadata.get("message"), str) else None,
        status=_signature_status(share),
        delivery_status=str(metadata.get("delivery_status") or "pending"),
        delivery_error=metadata.get("delivery_error") if isinstance(metadata.get("delivery_error"), str) else None,
        placement=SignaturePlacement.model_validate(placement),
        fields=_signature_fields(metadata),
        created_at=share.created_at,
        expires_at=share.expires_at,
        viewed_at=_parse_metadata_datetime(metadata.get("viewed_at")),
        signed_at=_parse_metadata_datetime(metadata.get("signed_at")),
        url=url,
        group_id=metadata.get("signature_group_id"),
        group_size=int(metadata.get("group_size") or 1),
        group_signed_count=int(metadata.get("group_signed_count") or (1 if _signature_status(share) == "completed" else 0)),
        group_finalized=bool(metadata.get("group_finalized", _signature_status(share) == "completed")),
    )


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
            message="Only the document owner or a user with global management or external share access can view shares",
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
        if not _is_signature_share(s)
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
            message="Only the document owner or a user with global management or external share access can share externally",
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
            message="Only the document owner or a user with global management or external share access can revoke",
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
    if _is_signature_share(share):
        raise HTTPException(404, "Share not found")
    share.status = "revoked"
    share.revoked_at = datetime.now(timezone.utc)
    share.revoked_by = user.id
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────
# Signature requests — recipient-scoped fields and signed PDF versions
# ─────────────────────────────────────────────────────────────────────────


def _signature_request_url(request: Request, raw_token: str) -> str:
    return f"{public_web_base(request)}/sign/{raw_token}"


async def _available_signature_senders(
    db: AsyncSession, *, entity_id: str, user_id: str,
) -> list[tuple[str, str, str]]:
    try:
        return await available_email_senders(
            db, entity_id=entity_id, user_id=user_id, reason="signature_sender_check",
        )
    except ConnectedEmailAccountsUnavailable as exc:
        raise CodedError(
            503, code="permissions.error.signature_request.sender_email_unavailable",
            message=str(exc),
        ) from exc


async def _select_signature_sender(
    db: AsyncSession, *, entity_id: str, user_id: str, selected_id: str | None,
) -> tuple[str, str, str]:
    available = await _available_signature_senders(db, entity_id=entity_id, user_id=user_id)
    if not available:
        raise CodedError(
            409, code="permissions.error.signature_request.sender_email_required",
            message="Connect a send-capable email account before sending for signature",
        )
    if selected_id:
        chosen = next((item for item in available if item[0] == selected_id), None)
        if chosen is None:
            raise CodedError(
                409, code="permissions.error.signature_request.sender_email_unavailable",
                message="The selected sending account is no longer available",
            )
        return chosen
    chosen = await default_email_sender(db, entity_id=entity_id, user_id=user_id, available=available)
    if chosen is None:
        raise CodedError(
            409, code="permissions.error.signature_request.sender_email_unavailable",
            message="Your default sending account is unavailable. Choose an available account in For Agents → Email.",
        )
    return chosen


async def _send_signature_mail(
    db: AsyncSession, share: Share, *, to: str, subject: str, content: str,
) -> bool:
    """Use the exact account pinned when the sender created this invitation."""
    from packages.core.services.email_service import send_common_email

    metadata = _signature_metadata(share)
    account_id = metadata.get("sender_integration_id")
    if not account_id:
        # Requests created before sender-account pinning keep their original route.
        await db.rollback()
        return await send_common_email(to, subject, content)
    sender = (str(account_id), str(metadata.get("sender_email") or ""), str(metadata.get("sender_provider") or "email"))
    entity_id, user_id = str(share.entity_id), str(share.created_by)
    sender_name = str(metadata.get("sender_name") or "")
    await db.rollback()
    return await send_email_from_account(
        sender=sender, entity_id=entity_id, user_id=user_id, to=to,
        subject=subject, content=content, sender_name=sender_name, reason="signature_email_delivery",
    )


async def _send_signature_request_email(
    *,
    db: AsyncSession,
    share: Share,
    recipient_email: str,
    recipient_name: str,
    sender_name: str,
    document_name: str,
    message: str | None,
    url: str,
) -> bool:
    safe_recipient = html.escape(recipient_name)
    safe_sender = html.escape(sender_name)
    safe_document = html.escape(document_name)
    safe_message = html.escape(message or "").replace("\n", "<br>")
    safe_url = html.escape(url, quote=True)
    content = (
        f"<p>Hi {safe_recipient},</p>"
        f"<p>{safe_sender} has sent <strong>{safe_document}</strong> for your signature.</p>"
        + (f"<p>{safe_message}</p>" if safe_message else "")
        + (
            f'<p><a href="{safe_url}" style="display:inline-block;padding:12px 18px;'
            'border-radius:8px;background:#292524;color:#fff;text-decoration:none">'
            "Review and sign</a></p>"
            "<p>This private link requires verification using this email address.</p>"
        )
    )
    subject_document = re.sub(r"[\r\n]+", " ", document_name).strip() or "PDF document"
    return await _send_signature_mail(
        db, share, to=recipient_email,
        subject=f"Signature requested: {subject_document}", content=content,
    )


async def _record_signature_delivery(
    db: AsyncSession,
    *,
    share_id: str,
    token_hash: str,
    delivered: bool,
    recipient_delivery_status: dict[str, str],
) -> Share | None:
    share = (await db.execute(
        select(Share)
        .where(
            Share.id == share_id,
            Share.token_hash == token_hash,
            Share.status == "active",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if share is None or not _is_signature_share(share):
        await db.rollback()
        return None
    metadata = _signature_metadata(share)
    metadata["delivery_status"] = "sent" if delivered else "failed"
    metadata["recipient_delivery_status"] = recipient_delivery_status
    metadata["delivery_error"] = None if delivered else "Email delivery failed; resend or copy the signing link"
    if delivered:
        metadata["email_sent_at"] = _timestamp_key(datetime.now(timezone.utc))
    share.metadata_ = metadata
    await db.commit()
    await db.refresh(share)
    return share


async def _deliver_signature_share(
    db: AsyncSession,
    *,
    share: Share,
    raw_token: str,
    request: Request,
) -> tuple[Share, str]:
    metadata = _signature_metadata(share)
    url = _signature_request_url(request, raw_token)
    token_hash = str(share.token_hash)
    share_id = str(share.id)
    recipient_emails = share_recipient_emails(share)
    # Credential leasing rolls back its read transaction before provider I/O.
    # Keep the invitation immutable across deliveries instead of expired ORM state.
    delivery_share = SimpleNamespace(
        id=share_id, entity_id=str(share.entity_id), created_by=str(share.created_by), metadata_=metadata,
    )
    deliveries = {}
    for recipient_email in recipient_emails:
        delivered = await _send_signature_request_email(
            db=db, share=delivery_share, recipient_email=recipient_email,
            recipient_name=str(metadata.get("recipient_name") or "Customer"),
            sender_name=str(metadata.get("sender_name") or "Manor AI user"),
            document_name=str(metadata.get("document_name") or "PDF document"),
            message=metadata.get("message") if isinstance(metadata.get("message"), str) else None,
            url=url,
        )
        deliveries[recipient_email] = "sent" if delivered else "failed"
    updated = await _record_signature_delivery(
        db,
        share_id=share_id,
        token_hash=token_hash,
        delivered=bool(deliveries) and all(status == "sent" for status in deliveries.values()),
        recipient_delivery_status=deliveries,
    )
    return updated or share, url


def _assert_signature_document(doc: Document) -> None:
    name = str(doc.name or "").lower()
    mime = str(doc.mime_type or "").lower()
    if not (name.endswith(".pdf") or mime == "application/pdf"):
        raise HTTPException(400, "Only PDF documents can be sent for signature")
    if not getattr(doc, "fs_path", None):
        raise HTTPException(409, "The PDF file is not available for signing")


@router.get("/{doc_id}/signature-requests", response_model=list[SignatureRequestResponse])
async def list_signature_requests(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_external_share(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.share_view_forbidden",
            message="You cannot view signature requests for this document",
        )
    rows = (await db.execute(
        select(Share)
        .where(
            Share.resource_type == ResourceType.DOCUMENT,
            Share.resource_id == doc.id,
            Share.entity_id == user.entity_id,
        )
        .order_by(desc(Share.created_at))
    )).scalars().all()
    return [_signature_response(row) for row in rows if _is_signature_share(row)]


@router.post(
    "/{doc_id}/signature-requests",
    response_model=SignatureRequestResponse,
    status_code=201,
)
async def create_signature_request(
    doc_id: str,
    req: CreateSignatureRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    credential = AuthenticatedUserCredential.from_user(user)
    sender_name = str(user.display_name or user.email or "Manor AI user")
    # File mutations take the filesystem boundary before policy/document locks.
    # Release authentication reads before waiting, then revalidate admission.
    await db.rollback()
    async with entity_filesystem_read_boundary(get_entity_root(credential.entity_id)):
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            raise HTTPException(403, "Your account or team access is no longer available")
        invitations = await _materialize_signature_request(
            db,
            doc_id=doc_id,
            req=req,
            request=request,
            user=SimpleNamespace(
                id=actor.user_id,
                entity_id=actor.entity_id,
                role=actor.role,
                display_name=sender_name,
            ),
        )
        await db.commit()
    first_response = None
    delivery_failed_count = 0
    for share, raw_token in invitations:
        await db.refresh(share)
        share, url = await _deliver_signature_share(
            db, share=share, raw_token=raw_token, request=request,
        )
        if _signature_metadata(share).get("delivery_status") == "failed":
            delivery_failed_count += 1
        if first_response is None:
            first_response = _signature_response(share, url=url)
    assert first_response is not None
    first_response.group_delivery_failed_count = delivery_failed_count
    return first_response


@router.get("/{doc_id}/signature-senders", response_model=list[SignatureSenderOption])
async def list_signature_senders(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_external_share(db, doc, user):
        raise HTTPException(403, "You cannot send this document for signature")
    available = await _available_signature_senders(db, entity_id=str(user.entity_id), user_id=str(user.id))
    default = await default_email_sender(db, entity_id=str(user.entity_id), user_id=str(user.id), available=available)
    return [
        SignatureSenderOption(id=account_id, email=email, provider=provider, is_default=(account_id == default[0] if default else False))
        for account_id, email, provider in available
    ]


async def _materialize_signature_request(
    db: AsyncSession,
    *,
    doc_id: str,
    req: CreateSignatureRequest,
    request: Request,
    user: SimpleNamespace,
) -> list[tuple[Share, str]]:
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _can_manage_external_share(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.doc.share_external_owner_only",
            message="You cannot send this document for external signature",
        )
    _assert_signature_document(doc)
    document_signature_lock = (
        doc.metadata_.get("signature_lock")
        if isinstance(doc.metadata_, dict)
        else None
    )
    if isinstance(document_signature_lock, dict) and document_signature_lock.get("status") in {"pending", "cancelled", _SIGNATURE_COMPLETED}:
        raise CodedError(
            409,
            code="permissions.error.signature_request.already_signed",
            message="This PDF has already been signed through this workflow",
        )
    recipients = [(req.recipient_name, req.recipient_email, req.recipient_emails, req.fields, req.placement)] + [
        (item.recipient_name, item.recipient_email, item.recipient_emails, item.fields, SignaturePlacement())
        for item in req.additional_recipients
    ]
    email_groups = [list(dict.fromkeys(_normalize_signature_email(email) for email in [primary, *alternates]))
                    for _, primary, alternates, _, _ in recipients]
    normalized_emails = [email for emails in email_groups for email in emails]
    if len(normalized_emails) > 10:
        raise HTTPException(400, "Use no more than 10 recipient email addresses in one request")
    if len(set(normalized_emails)) != len(normalized_emails):
        raise HTTPException(400, "Each email address must belong to only one signer")
    for name, _email, _alternates, fields, placement in recipients:
        if not name.strip():
            raise HTTPException(400, "Enter the recipient's name")
        _validate_signature_placement(placement)
        if fields is not None:
            validate_signature_fields(fields)
    classification, _visibility, _client_visible = await effective_document_folder_policy(db, doc)
    if classification == Classification.RESTRICTED:
        raise CodedError(
            400,
            code="permissions.error.doc.restricted_no_external",
            message="Restricted documents cannot be sent externally",
        )
    if classification == Classification.CONFIDENTIAL:
        raise CodedError(
            409,
            code="permissions.error.doc.confidential_needs_approval",
            message="Confidential documents require external-share approval before signing",
        )

    existing_rows = (await db.execute(
        select(Share).where(
            Share.resource_type == ResourceType.DOCUMENT,
            Share.resource_id == doc.id,
            Share.entity_id == user.entity_id,
            Share.status == "active",
        )
    )).scalars().all()
    existing_signature_statuses = {
        _signature_status(row) for row in existing_rows if _is_signature_share(row)
    }
    if "completed" in existing_signature_statuses:
        raise CodedError(
            409,
            code="permissions.error.signature_request.already_signed",
            message="This PDF has already been signed through this workflow",
        )
    if "pending" in existing_signature_statuses:
        raise CodedError(
            409,
            code="permissions.error.signature_request.active_exists",
            message="This PDF already has an active signature request",
        )

    full_path = _require_public_document_file_path(
        doc,
        unavailable_message="PDF file is not available for signing",
    )
    if os.path.getsize(full_path) > _SIGNATURE_MAX_SOURCE_BYTES:
        raise HTTPException(413, "PDF is too large for online signing")
    from packages.core.services.entity_fs import open_entity_file_snapshot

    def validate_source() -> None:
        with open_entity_file_snapshot(
            str(doc.entity_id),
            str(doc.fs_path),
            expected_resolved_path=full_path,
        ) as snapshot:
            with open(snapshot.descriptor_path, "rb") as handle:
                source_bytes = handle.read(_SIGNATURE_MAX_SOURCE_BYTES + 1)
        reader = _read_signature_pdf(source_bytes, req.placement if req.fields is None else SignaturePlacement())
        for _name, _email, _alternates, fields, _placement in recipients:
            if fields is not None and any(field.page > len(reader.pages) for field in fields):
                raise HTTPException(400, "A field page is outside the PDF")

    await asyncio.to_thread(validate_source)
    sender_integration_id, sender_email, sender_provider = await _select_signature_sender(
        db, entity_id=str(doc.entity_id), user_id=str(user.id),
        selected_id=req.sender_integration_id,
    )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=req.expires_in_days)
    if not await _is_owner_or_admin(db, doc, user):
        try:
            expires_at = ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT).delegated_expiry(
                (Capability.VIEW, Capability.DOWNLOAD),
                grants=await document_grants_for_user(db, document=doc, user_id=user.id),
                expires_at=expires_at,
                authority_capabilities=(Capability.SHARE_EXTERNAL,),
                clamp_expiry=True,
            )
        except ResourceGrantPolicyError as exc:
            raise HTTPException(403, str(exc)) from exc
    group_id = generate_ulid() if len(recipients) > 1 else None
    invitations: list[tuple[Share, str]] = []
    for (recipient_name, _email, _alternates, fields, placement), recipient_emails in zip(recipients, email_groups):
        recipient_email = recipient_emails[0]
        raw_token = secrets.token_urlsafe(32)
        share = Share(
            id=generate_ulid(), entity_id=doc.entity_id,
            resource_type=ResourceType.DOCUMENT, resource_id=doc.id,
            token_hash=_hash_token(raw_token),
            capabilities=[Capability.VIEW, Capability.SIGN, Capability.DOWNLOAD],
            audience=f"email:{recipient_email}", require_otp=True,
            watermark=False, allow_download=True, created_by=user.id,
            created_at=now, expires_at=expires_at,
            max_uses=None, use_count=0, status="active",
            metadata_={
                "kind": _SIGNATURE_REQUEST_KIND,
                "signature_status": _SIGNATURE_PENDING,
                "delivery_status": "pending",
                "recipient_name": recipient_name.strip(),
                "recipient_emails": recipient_emails,
                "sender_name": user.display_name,
                "sender_email": sender_email,
                "sender_integration_id": sender_integration_id,
                "sender_provider": sender_provider,
                "document_name": str(doc.name or "PDF document"),
                "message": req.message.strip() if req.message and req.message.strip() else None,
                "placement": placement.model_dump(),
                **({"fields": [field.model_dump() for field in fields]} if fields is not None else {}),
                "source_updated_at": _timestamp_key(doc.updated_at),
                "classification_at_creation": str(classification or ""),
                "approved_external_share": False,
                **({"signature_group_id": group_id, "group_size": len(recipients),
                    "group_signed_count": 0, "group_finalized": False} if group_id else {}),
            },
        )
        db.add(share)
        invitations.append((share, raw_token))
        await _write_access_log(
            db, doc=doc, actor_type="user", actor_id=user.id,
            action="signature_request_create", share_id=share.id, request=request,
        )
    if group_id:
        document_metadata = dict(doc.metadata_ or {})
        document_metadata["signature_lock"] = {"status": "pending", "group_id": group_id}
        doc.metadata_ = document_metadata
    return invitations


async def _load_managed_signature_request(
    db: AsyncSession,
    *,
    doc: Document,
    share_id: str,
    entity_id: str,
) -> Share:
    share = (await db.execute(
        select(Share)
        .where(
            Share.id == share_id,
            Share.resource_type == ResourceType.DOCUMENT,
            Share.resource_id == doc.id,
            Share.entity_id == entity_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if share is None or not _is_signature_share(share):
        raise HTTPException(404, "Signature request not found")
    return share


@router.post(
    "/{doc_id}/signature-requests/{share_id}/resend",
    response_model=SignatureRequestResponse,
)
async def resend_signature_request(
    doc_id: str,
    share_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _can_manage_external_share(db, doc, user):
        raise HTTPException(403, "You cannot resend this signature request")
    share = await _load_managed_signature_request(
        db, doc=doc, share_id=share_id, entity_id=user.entity_id,
    )
    if _signature_status(share) != "pending":
        raise HTTPException(409, "Only a pending signature request can be resent")
    raw_token = secrets.token_urlsafe(32)
    share.token_hash = _hash_token(raw_token)
    metadata = _signature_metadata(share)
    metadata["delivery_status"] = "pending"
    metadata["delivery_error"] = None
    metadata.pop("recipient_delivery_status", None)
    metadata.pop("otp_challenges", None)
    share.metadata_ = metadata
    await db.commit()
    await db.refresh(share)
    share, url = await _deliver_signature_share(
        db,
        share=share,
        raw_token=raw_token,
        request=request,
    )
    return _signature_response(share, url=url)


@router.delete("/{doc_id}/signature-requests/{share_id}", status_code=204)
async def revoke_signature_request(
    doc_id: str,
    share_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _can_manage_external_share(db, doc, user):
        raise HTTPException(403, "You cannot revoke this signature request")
    share = await _load_managed_signature_request(
        db, doc=doc, share_id=share_id, entity_id=user.entity_id,
    )
    if _signature_status(share) == "completed":
        raise HTTPException(409, "A completed signature request cannot be revoked")
    group_id = str(_signature_metadata(share).get("signature_group_id") or "")
    peers = [share]
    if group_id:
        peers = (await db.execute(
            select(Share).where(
                Share.resource_type == ResourceType.DOCUMENT,
                Share.resource_id == doc.id,
                Share.entity_id == doc.entity_id,
                Share.metadata_["signature_group_id"].as_string() == group_id,
            ).order_by(Share.id).with_for_update().execution_options(populate_existing=True)
        )).scalars().all()
        lock = (doc.metadata_ or {}).get("signature_lock")
        if not isinstance(lock, dict) or lock.get("status") != "pending" or lock.get("group_id") != group_id:
            raise HTTPException(409, "This signing group is no longer active")
        document_metadata = dict(doc.metadata_ or {})
        if any(_signature_metadata(peer).get("signature_status") == _SIGNATURE_COMPLETED for peer in peers):
            document_metadata["signature_lock"] = {
                **lock,
                "status": "cancelled",
                "group_id": group_id,
            }
        else:
            document_metadata.pop("signature_lock", None)
        doc.metadata_ = document_metadata
    for peer in peers:
        peer.status = "revoked"
        peer.revoked_at = datetime.now(timezone.utc)
        peer.revoked_by = user.id
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
    # Completion aliases are not supported. Reject them before any Share
    # lookup instead of admitting the unfinished alternate token namespace.
    if token.startswith("sc1."):
        raise HTTPException(404, "Share not found or revoked")
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
            entity_id = await db.scalar(
                select(Entity.id)
                .where(
                    Entity.id == share_scope.entity_id,
                    Entity.deleted_at.is_(None),
                )
                .with_for_update(read=True)
            )
            if entity_id is None:
                raise CodedError(
                    410,
                    code="permissions.error.share.not_found_or_revoked",
                    message="Share is no longer available",
                )

            doc_stmt = select(Document).where(
                Document.id == share_scope.resource_id,
                Document.entity_id == share_scope.entity_id,
            )
            doc = (await db.execute(doc_stmt)).scalar_one_or_none()
            if not doc:
                raise HTTPException(404, "Underlying document not found")
            scope = await resolve_document_policy_lock_scope(db, doc)
            await lock_workspace_policy_rows(
                db,
                entity_id=doc.entity_id,
                workspace_ids=scope.workspace_ids,
                read=True,
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
            await _require_public_document_share_policy(db, share, doc)
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


async def _require_public_document_share_policy(
    db: AsyncSession, share: Share, doc: Document,
) -> None:
    """Check live document policy while the caller holds its policy locks."""
    if bool(getattr(doc, "is_trashed", False)) or getattr(
        doc, "quarantine_status", None
    ) in {"quarantined", "rejected"}:
        raise HTTPException(404, "Underlying document not found")
    current_classification, _visibility, _client_visible = (
        await effective_document_folder_policy(db, doc)
    )
    metadata = dict(getattr(share, "metadata_", {}) or {})
    created_classification = metadata.get("classification_at_creation")
    escalated = created_classification in _PUBLIC_CLASS_RANK and (
        _PUBLIC_CLASS_RANK.get(current_classification, 1)
        > _PUBLIC_CLASS_RANK[created_classification]
    )
    if (
        current_classification == Classification.RESTRICTED
        or escalated
        or (
            current_classification == Classification.CONFIDENTIAL
            and not metadata.get("approved_external_share")
        )
    ):
        raise CodedError(
            410, code="permissions.error.share.revoked_by_policy",
            message="Share is no longer available",
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
    signature_metadata = _signature_metadata(share)
    if signature_metadata.get("signature_group_id") and not signature_metadata.get("group_finalized"):
        raise CodedError(
            409,
            code="permissions.error.signature.group_pending",
            message="The signed document is available for download after every signer has signed",
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


def _signed_copy_name(source_name: str) -> str:
    filename = os.path.basename(source_name.strip()) or "document.pdf"
    stem, extension = os.path.splitext(filename)
    return f"{stem or 'document'}-signed{extension or '.pdf'}"


def _signature_working_fs_path(doc: Document, group_id: str) -> str | None:
    if not group_id:
        return None
    lock = (doc.metadata_ or {}).get("signature_lock")
    if not isinstance(lock, dict) or str(lock.get("group_id") or "") != group_id:
        return None
    working_fs_path = lock.get("working_fs_path")
    expected_path = f".signatures/{group_id}.pdf"
    return expected_path if working_fs_path == expected_path else None


async def _signature_content_target(
    db: AsyncSession,
    share: Share,
    source_doc: Document,
    *,
    unavailable_message: str,
) -> tuple[Document, str]:
    """Resolve the source, private partial, or final signed copy for a share."""
    metadata = _signature_metadata(share)
    signed_document_id = metadata.get("signed_document_id")
    if isinstance(signed_document_id, str) and signed_document_id:
        signed_doc = await db.get(Document, signed_document_id)
        signed_source_id = (
            (signed_doc.metadata_ or {}).get("signature_source_document_id")
            if signed_doc is not None
            else None
        )
        if (
            signed_doc is None
            or signed_doc.is_trashed
            or str(signed_doc.entity_id) != str(source_doc.entity_id)
            or str(signed_source_id or "") != str(source_doc.id)
        ):
            raise CodedError(
                404,
                code="permissions.error.share.file_unavailable",
                message=unavailable_message,
            )
        return signed_doc, _require_public_document_file_path(
            signed_doc,
            unavailable_message=unavailable_message,
        )

    group_id = str(metadata.get("signature_group_id") or "")
    working_fs_path = _signature_working_fs_path(source_doc, group_id)
    if working_fs_path:
        full_path = resolve_path(source_doc.entity_id, working_fs_path)
        if full_path and os.path.isfile(full_path):
            return source_doc, full_path

    return source_doc, _require_public_document_file_path(
        source_doc,
        unavailable_message=unavailable_message,
    )


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
        INTERACTIVE_RENDER_ADMISSION_TIMEOUT_SECONDS,
        OfficeRenderAbandonedError,
        OfficeRenderBusyError,
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
            else render_slides(
                source_path,
                cache_dir,
                source_ext=source_ext,
                admission_timeout=INTERACTIVE_RENDER_ADMISSION_TIMEOUT_SECONDS,
                should_abandon=request.is_disconnected,
            )
        )
    except OfficeRenderLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except (OfficeRenderBusyError, OfficeRenderAbandonedError) as exc:
        # Public share links must not queue unbounded renders behind the
        # shared render slots; viewers retry after the hint.
        raise HTTPException(
            503,
            {
                "code": "office_render_busy",
                "message": "Slide rendering is busy; retry shortly",
            },
            headers={"Retry-After": str(documents.SLIDE_RENDER_BUSY_RETRY_AFTER_SECONDS)},
        ) from exc
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


def _require_signature_share(share: Share) -> dict:
    metadata = _signature_metadata(share)
    if metadata.get("kind") != _SIGNATURE_REQUEST_KIND or Capability.SIGN not in set(
        share.capabilities or []
    ):
        raise HTTPException(404, "Signature request not found")
    return metadata


def _decode_signature_image(data_url: str):
    from PIL import Image, ImageChops, UnidentifiedImageError

    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise HTTPException(400, "Signature must be a PNG image")
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, "Signature image is invalid") from exc
    if not raw or len(raw) > _SIGNATURE_MAX_IMAGE_BYTES:
        raise HTTPException(413, "Signature image is too large")
    try:
        image = Image.open(io.BytesIO(raw))
        if image.format != "PNG":
            raise HTTPException(400, "Signature must be a PNG image")
        if image.width < 8 or image.height < 8 or image.width > 4096 or image.height > 4096:
            raise HTTPException(400, "Signature image dimensions are invalid")
        image.verify()
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise HTTPException(400, "Signature image is invalid") from exc
    alpha = image.getchannel("A")
    # Opaque white canvases are empty too. Only visible, non-white ink counts.
    bbox = ImageChops.multiply(ImageChops.invert(image.convert("L")), alpha).getbbox()
    if bbox is None:
        raise HTTPException(400, "Draw a signature before submitting")
    image = image.crop(bbox)
    if image.width < 2 or image.height < 2:
        raise HTTPException(400, "Draw a signature before submitting")
    return image, hashlib.sha256(raw).hexdigest()


def _validate_signature_values(metadata: dict, req: SubmitSignatureRequest):
    fields = _signature_fields(metadata)
    values = dict(req.field_values or {})
    if "fields" not in metadata and not values and req.signature_data_url:
        values = {"signature": req.signature_data_url}
    if set(values) - {field.id for field in fields}:
        raise HTTPException(400, "Submitted values contain an unknown field")
    # Decode only a bounded total input, not 50 maximum-sized images at once.
    if sum(len(value) for value in values.values() if isinstance(value, str)) > 6_000_000:
        raise HTTPException(413, "Submitted field values are too large")
    images, evidence = {}, {}
    signature_pixels = 0
    for field in fields:
        value = values.get(field.id)
        if value is None or value == "":
            if field.required:
                raise HTTPException(400, f"Complete the required field: {field.label}")
            values.pop(field.id, None)
            continue
        if field.type == "checkbox":
            if type(value) is not bool:
                raise HTTPException(400, "Checkbox fields require a boolean value")
            if field.required and not value:
                raise HTTPException(400, f"Check the required field: {field.label}")
            evidence[field.id] = value
            continue
        if not isinstance(value, str):
            raise HTTPException(400, "This field requires a text value")
        if field.type == "signature":
            image, digest = _decode_signature_image(value)
            signature_pixels += image.width * image.height
            if signature_pixels > 16_000_000:
                raise HTTPException(413, "Combined signature images are too large")
            images[field.id] = image
            evidence[field.id] = {"sha256": digest}
            continue
        limit = 120 if field.type == "name" else 10 if field.type == "date" else 2000
        if len(value) > limit or any(unicodedata.category(char).startswith("C") and char != "\n" for char in value):
            raise HTTPException(400, "Text field value is invalid or too long")
        value = value.strip()
        if not value:
            if field.required:
                raise HTTPException(400, f"Complete the required field: {field.label}")
            values.pop(field.id, None)
            continue
        if field.type == "date":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError("Invalid date format")
                date.fromisoformat(value)
            except ValueError as exc:
                raise HTTPException(400, "Date fields must contain a valid YYYY-MM-DD date") from exc
        values[field.id] = value
        evidence[field.id] = value
    return fields, values, images, evidence


def _pdf_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _read_signature_pdf(source_bytes: bytes, placement: SignaturePlacement):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if len(source_bytes) > _SIGNATURE_MAX_SOURCE_BYTES:
        raise HTTPException(413, "PDF is too large for online signing")
    if re.search(rb"/FT\s*/Sig\b|/Type\s*/Sig\b|/ByteRange\s*\[", source_bytes, re.I):
        raise HTTPException(409, "This PDF already contains a digital signature")
    try:
        reader = PdfReader(io.BytesIO(source_bytes), strict=False)
        if reader.is_encrypted:
            raise HTTPException(409, "Password-protected PDFs cannot be signed online")
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, "The source file is not a readable PDF") from exc
    if placement.page > page_count:
        raise HTTPException(400, "Signature page is outside the PDF")
    return reader


def _stamp_pdf_signature(
    source_bytes: bytes,
    signature_image,
    *,
    placement: SignaturePlacement,
    signer_email: str,
    signed_at: datetime,
    request_id: str,
    signature_status: str = "completed",
) -> bytes:
    """Add a transparent PNG XObject and an audit caption to one PDF page."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )

    reader = _read_signature_pdf(source_bytes, placement)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    page = writer.pages[placement.page - 1]
    if int(page.get("/Rotate", 0) or 0) % 360:
        page.transfer_rotation_to_content()
    page_left = max(float(page.mediabox.left), float(page.cropbox.left))
    page_bottom = max(float(page.mediabox.bottom), float(page.cropbox.bottom))
    page_right = min(float(page.mediabox.right), float(page.cropbox.right))
    page_top = min(float(page.mediabox.top), float(page.cropbox.top))
    page_width = page_right - page_left
    page_height = page_top - page_bottom
    box_width = page_width * placement.width
    box_height = page_height * placement.height
    aspect = signature_image.width / max(1, signature_image.height)
    draw_width = min(box_width, box_height * aspect)
    draw_height = min(box_height, draw_width / max(aspect, 0.01))
    box_x = page_left + page_width * placement.x
    box_bottom = page_bottom + page_height * (1 - placement.y - placement.height)
    draw_x = box_x + (box_width - draw_width) / 2
    draw_y = box_bottom + (box_height - draw_height) / 2

    # Courier has a fixed 600-unit advance, so wrapping stays exact without
    # platform fonts. Preserve readable type and every email/timestamp character.
    caption_font_size = 9
    caption_line_height = 11
    caption_margin = 8
    caption_width = min(max(box_width, 180), page_width - 2 * caption_margin)
    caption_columns = int(caption_width / (caption_font_size * 0.6))
    if caption_columns < 1:
        raise HTTPException(400, "PDF page is too small for readable signing details")
    caption_lines = textwrap.wrap(
        f"Signed by {signer_email}",
        width=caption_columns,
        break_on_hyphens=False,
    ) + textwrap.wrap(
        f"at {signed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        width=caption_columns,
        break_on_hyphens=False,
    )
    caption_x = max(page_left + caption_margin, min(box_x, page_right - caption_margin - caption_width))
    caption_y = box_bottom - caption_line_height
    last_baseline = caption_y - (len(caption_lines) - 1) * caption_line_height
    shift_y = max(0.0, page_bottom + caption_margin + 2 - last_baseline)
    if draw_y + draw_height + shift_y > page_top - caption_margin:
        raise HTTPException(400, "PDF page is too small for readable signing details")
    draw_y += shift_y
    caption_y += shift_y

    rgb = signature_image.convert("RGB")
    alpha = signature_image.getchannel("A")
    mask_stream = StreamObject()
    mask_stream._data = zlib.compress(alpha.tobytes())
    mask_stream.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(signature_image.width),
        NameObject("/Height"): NumberObject(signature_image.height),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
    })
    mask_ref = writer._add_object(mask_stream)

    image_stream = StreamObject()
    image_stream._data = zlib.compress(rgb.tobytes())
    image_stream.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(signature_image.width),
        NameObject("/Height"): NumberObject(signature_image.height),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
        NameObject("/SMask"): mask_ref,
    })
    image_ref = writer._add_object(image_stream)

    resources_source = page.get("/Resources")
    resources_existing = resources_source.get_object() if resources_source is not None else {}
    resources = DictionaryObject()
    for key, value in resources_existing.items():
        resources[key] = value
    xobject_source = resources_existing.get("/XObject")
    xobject_existing = xobject_source.get_object() if xobject_source is not None else {}
    xobjects = DictionaryObject()
    for key, value in xobject_existing.items():
        xobjects[key] = value
    image_name = NameObject(f"/ManorSignature{request_id[:8]}")
    xobjects[image_name] = image_ref
    resources[NameObject("/XObject")] = xobjects

    font_source = resources_existing.get("/Font")
    font_existing = font_source.get_object() if font_source is not None else {}
    fonts = DictionaryObject()
    for key, value in font_existing.items():
        fonts[key] = value
    font_name = NameObject("/ManorSignatureFont")
    fonts[font_name] = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
    }))
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources

    commands = (
        f"q {draw_width:.3f} 0 0 {draw_height:.3f} {draw_x:.3f} {draw_y:.3f} cm "
        f"{image_name} Do Q\n"
        + "".join(
            f"BT {font_name} {caption_font_size} Tf {caption_x:.3f} "
            f"{caption_y - index * caption_line_height:.3f} Td "
            f"({_pdf_string(line)}) Tj ET\n"
            for index, line in enumerate(caption_lines)
        )
    ).encode("ascii")
    content_stream = StreamObject()
    content_stream._data = commands
    content_ref = writer._add_object(content_stream)
    current_contents = page.get("/Contents")
    if current_contents is None:
        page[NameObject("/Contents")] = content_ref
    else:
        resolved_contents = current_contents.get_object()
        original_contents = (
            list(resolved_contents)
            if isinstance(resolved_contents, ArrayObject)
            else [current_contents]
        )
        # Contents can reference an array indirectly. Flatten it, and restore
        # the initial graphics state before drawing our independent overlay.
        save_state = StreamObject()
        save_state._data = b"q\n"
        restore_state = StreamObject()
        restore_state._data = b"Q\n"
        page[NameObject("/Contents")] = ArrayObject([
            writer._add_object(save_state),
            *original_contents,
            writer._add_object(restore_state),
            content_ref,
        ])

    existing_metadata = {
        str(key): str(value)
        for key, value in (reader.metadata or {}).items()
        if isinstance(key, str) and value is not None
    }
    existing_metadata.update({
        "/ManorSignatureStatus": signature_status,
        "/ManorSignatureRequestId": request_id,
        "/ManorSignedAt": _timestamp_key(signed_at),
        "/ManorSigner": signer_email,
    })
    writer.add_metadata(existing_metadata)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@public_router.get(
    "/{token}/signature-request",
    response_model=PublicSignatureRequestResponse,
)
async def view_signature_request(
    token: str,
    request: Request,
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    share, doc, _now = await _load_public_document_share(
        db,
        token,
        share_for_update=True,
        allow_use_limit_reached=True,
    )
    metadata = _require_signature_share(share)
    verified_email = _require_public_share_access(
        share,
        access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
    )
    now = datetime.now(timezone.utc)
    if not metadata.get("viewed_at"):
        metadata["viewed_at"] = _timestamp_key(now)
        share.metadata_ = metadata
        await _write_access_log(
            db,
            doc=doc,
            actor_type="share_token",
            actor_id=verified_email or share.id,
            action="signature_request_view",
            share_id=share.id,
            request=request,
        )
    await db.commit()
    placement = metadata.get("placement") if isinstance(metadata.get("placement"), dict) else {}
    return PublicSignatureRequestResponse(
        id=str(share.id),
        document_name=str(metadata.get("document_name") or doc.name),
        recipient_name=str(metadata.get("recipient_name") or "Customer"),
        recipient_email=str(verified_email or ""),
        sender_name=str(metadata.get("sender_name") or "Manor AI user"),
        message=metadata.get("message") if isinstance(metadata.get("message"), str) else None,
        status=(
            _SIGNATURE_COMPLETED
            if metadata.get("signature_status") == _SIGNATURE_COMPLETED
            else _SIGNATURE_PENDING
        ),
        placement=SignaturePlacement.model_validate(placement),
        fields=_signature_fields(metadata),
        expires_at=share.expires_at,
        signed_at=_parse_metadata_datetime(metadata.get("signed_at")),
        group_size=int(metadata.get("group_size") or 1),
        group_signed_count=int(metadata.get("group_signed_count") or (1 if metadata.get("signature_status") == _SIGNATURE_COMPLETED else 0)),
        group_finalized=bool(metadata.get("group_finalized", metadata.get("signature_status") == _SIGNATURE_COMPLETED)),
    )


@public_router.post(
    "/{token}/signature",
    response_model=SignatureCompletionResponse,
)
async def submit_signature(
    token: str,
    req: SubmitSignatureRequest,
    request: Request,
    access_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if token.startswith("sc1."):
        raise HTTPException(409, "Completion receipts cannot be used to sign")
    if not req.consent:
        raise HTTPException(400, "You must consent to use this electronic signature")
    share, doc, _now = await _load_public_document_share(
        db,
        token,
        allow_use_limit_reached=True,
    )
    metadata = _require_signature_share(share)
    group_id = str(metadata.get("signature_group_id") or "")
    signer_email = _require_public_share_access(
        share,
        access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
    )
    if not signer_email:
        raise HTTPException(401, "Email verification is required")
    if metadata.get("signature_status") == _SIGNATURE_COMPLETED:
        raise HTTPException(409, "This document has already been signed")
    field_snapshot = _signature_fields_snapshot(metadata)
    fields, field_values, signature_images, field_evidence = _validate_signature_values(metadata, req)
    evidence_sha256 = hashlib.sha256(
        json.dumps(field_evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    signature_sha256 = (
        field_evidence["signature"]["sha256"]
        if field_snapshot["legacy"] else hashlib.sha256(json.dumps({
            field.id: field_evidence[field.id]["sha256"]
            for field in fields if field.type == "signature" and field.id in field_evidence
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    placement = SignaturePlacement.model_validate(metadata.get("placement") or {})
    _validate_signature_placement(placement)
    _assert_signature_document(doc)

    read_boundary, current_share, current_doc, _now = await _acquire_public_document_file_boundary(
        db,
        token,
        expected_entity_id=str(doc.entity_id),
    )
    try:
        current_metadata = _require_signature_share(current_share)
        _require_public_share_access(
            current_share,
            access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
        )
        if current_metadata.get("signature_status") == _SIGNATURE_COMPLETED:
            raise HTTPException(409, "This document has already been signed")
        if _signature_fields_snapshot(current_metadata) != field_snapshot:
            raise HTTPException(409, "Signing fields changed; reload this request")
        signature_input_fs_path = (
            _signature_working_fs_path(current_doc, group_id)
            or str(current_doc.fs_path or "")
        )
        full_path = resolve_path(current_doc.entity_id, signature_input_fs_path)
        if not full_path or not os.path.isfile(full_path):
            raise CodedError(
                404,
                code="permissions.error.share.file_unavailable",
                message="PDF file is not available for signing",
            )
        if os.path.getsize(full_path) > _SIGNATURE_MAX_SOURCE_BYTES:
            raise HTTPException(413, "PDF is too large for online signing")
        from packages.core.services.entity_fs import open_entity_file_snapshot

        def read_source() -> bytes:
            with open_entity_file_snapshot(
                str(current_doc.entity_id),
                signature_input_fs_path,
                expected_resolved_path=full_path,
            ) as snapshot:
                with open(snapshot.descriptor_path, "rb") as handle:
                    return handle.read(_SIGNATURE_MAX_SOURCE_BYTES + 1)

        source_bytes = await asyncio.to_thread(read_source)
        source_updated_at = str(current_metadata.get("source_updated_at") or "")
        share_id = str(current_share.id)
        entity_id = str(current_doc.entity_id)
        document_id = str(current_doc.id)
        document_name = str(current_doc.name)
        creator_id = str(current_share.created_by)
        expected_group_finalized = True
        if group_id:
            group_metadata = (await db.execute(
                select(Share.metadata_).where(
                    Share.resource_type == ResourceType.DOCUMENT,
                    Share.resource_id == current_doc.id,
                    Share.entity_id == current_doc.entity_id,
                    Share.metadata_["signature_group_id"].as_string() == group_id,
                )
            )).scalars().all()
            if len(group_metadata) != int(current_metadata.get("group_size") or 0):
                raise HTTPException(409, "Signing group is incomplete")
            expected_group_finalized = sum(
                1 for item in group_metadata
                if item.get("signature_status") == _SIGNATURE_COMPLETED
            ) == len(group_metadata) - 1
        await db.rollback()
    finally:
        await read_boundary.__aexit__(None, None, None)
    if len(source_bytes) > _SIGNATURE_MAX_SOURCE_BYTES:
        raise HTTPException(413, "PDF is too large for online signing")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    signed_at = datetime.now(timezone.utc)
    def stamp() -> bytes:
        if field_snapshot["legacy"]:
            return _stamp_pdf_signature(
                source_bytes, signature_images["signature"], placement=placement,
                signer_email=signer_email, signed_at=signed_at, request_id=share_id,
                signature_status="completed" if expected_group_finalized else "pending",
            )
        reader = _read_signature_pdf(source_bytes, SignaturePlacement())
        if any(field.page > len(reader.pages) for field in fields):
            raise HTTPException(400, "A field page is outside the PDF")
        return stamp_pdf_fields(
            reader, fields, field_values, signature_images,
            signer_email=signer_email, signed_at=signed_at, request_id=share_id,
            signature_status="completed" if expected_group_finalized else "pending",
        )

    signed_bytes = await asyncio.to_thread(stamp)
    signed_sha256 = hashlib.sha256(signed_bytes).hexdigest()

    group_result = {"finalized": True}

    async def authorize_signature_write(
        session: AsyncSession,
        locked_doc: Document,
        signed_doc: Document | None,
        output_fs_path: str,
    ) -> None:
        if not group_id and _timestamp_key(locked_doc.updated_at) != source_updated_at:
            raise CodedError(
                409,
                code="permissions.error.signature_request.document_changed",
                message="The PDF changed after it was sent. Ask the sender for a new request",
            )
        locked_share = (await session.execute(
            select(Share)
            .where(
                Share.id == share_id,
                Share.token_hash == _hash_token(token),
                Share.status == "active",
                Share.resource_type == ResourceType.DOCUMENT,
                Share.resource_id == locked_doc.id,
                Share.entity_id == locked_doc.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if locked_share is None:
            raise HTTPException(404, "Signature request is no longer available")
        await _require_public_document_share_policy(session, locked_share, locked_doc)
        if _require_public_share_access(
            locked_share,
            access_token or request.cookies.get(_SHARE_ACCESS_COOKIE),
        ) != signer_email:
            raise HTTPException(401, "Email verification is required")
        locked_metadata = _require_signature_share(locked_share)
        if locked_share.expires_at and locked_share.expires_at < datetime.now(timezone.utc):
            raise HTTPException(410, "Signature request has expired")
        if locked_metadata.get("signature_status") != _SIGNATURE_PENDING:
            raise HTTPException(409, "This document has already been signed")
        if _signature_fields_snapshot(locked_metadata) != field_snapshot:
            raise HTTPException(409, "Signing fields changed; reload this request")
        group_members: list[Share] = []
        if group_id:
            lock = (locked_doc.metadata_ or {}).get("signature_lock")
            if not isinstance(lock, dict) or lock.get("status") != "pending" or lock.get("group_id") != group_id:
                raise HTTPException(409, "This signing group is no longer active")
            group_members = (await session.execute(
                select(Share).where(
                    Share.resource_type == ResourceType.DOCUMENT,
                    Share.resource_id == locked_doc.id,
                    Share.entity_id == locked_doc.entity_id,
                    Share.metadata_["signature_group_id"].as_string() == group_id,
                ).order_by(Share.id).with_for_update().execution_options(populate_existing=True)
            )).scalars().all()
            if len(group_members) != int(locked_metadata.get("group_size") or 0):
                raise HTTPException(409, "Signing group is incomplete")
            if any(
                member.status != "active" or
                (member.expires_at is not None and member.expires_at < datetime.now(timezone.utc))
                for member in group_members
            ):
                raise HTTPException(409, "A signing invitation is no longer active")
        locked_metadata.update({
            "signature_status": _SIGNATURE_COMPLETED,
            "signed_at": _timestamp_key(signed_at),
            "signer_email": signer_email,
            "signature_sha256": signature_sha256,
            **({"signed_document_sha256": signed_sha256} if not group_id else {}),
            "field_values": field_evidence,
            "field_values_sha256": evidence_sha256,
        })
        locked_share.metadata_ = locked_metadata
        document_metadata = dict(getattr(locked_doc, "metadata_", {}) or {})
        if group_id:
            signed_count = sum(
                1 for member in group_members
                if member.id == share_id or _signature_metadata(member).get("signature_status") == _SIGNATURE_COMPLETED
            )
            finalized = signed_count == len(group_members)
            if finalized != expected_group_finalized:
                raise HTTPException(409, "Signing status changed; reload and try again")
            group_result["finalized"] = finalized
            for member in group_members:
                member_metadata = locked_metadata if member.id == share_id else _signature_metadata(member)
                member_metadata["group_signed_count"] = signed_count
                member_metadata["group_finalized"] = finalized
                if finalized:
                    member_metadata["signed_document_sha256"] = signed_sha256
                member.metadata_ = member_metadata
            if finalized:
                if signed_doc is None:
                    raise HTTPException(409, "Signed copy could not be finalized")
                document_metadata["signature_lock"] = {
                    "status": _SIGNATURE_COMPLETED,
                    "group_id": group_id,
                    "signed_at": _timestamp_key(signed_at),
                    "signer_emails": [_signature_metadata(member).get("signer_email") or str(member.audience).removeprefix("email:") for member in group_members],
                    "document_sha256": signed_sha256,
                    "signed_document_id": signed_doc.id,
                    "signed_document_name": signed_doc.name,
                }
                for member in group_members:
                    member_metadata = _signature_metadata(member)
                    member_metadata["signed_document_id"] = signed_doc.id
                    member_metadata["signed_document_name"] = signed_doc.name
                    member.metadata_ = member_metadata
            else:
                document_metadata["signature_lock"] = {
                    "status": "pending",
                    "group_id": group_id,
                    "working_fs_path": output_fs_path,
                    "document_sha256": signed_sha256,
                }
        else:
            if signed_doc is None:
                raise HTTPException(409, "Signed copy could not be finalized")
            locked_metadata["signed_document_id"] = signed_doc.id
            locked_metadata["signed_document_name"] = signed_doc.name
            locked_share.metadata_ = locked_metadata
            document_metadata["signature_lock"] = {
                "status": _SIGNATURE_COMPLETED,
                "request_id": share_id,
                "signed_at": _timestamp_key(signed_at),
                "signer_email": signer_email,
                "document_sha256": signed_sha256,
                "signed_document_id": signed_doc.id,
                "signed_document_name": signed_doc.name,
            }
        locked_doc.metadata_ = document_metadata
        await _write_access_log(
            session,
            doc=locked_doc,
            actor_type="share_token",
            actor_id=signer_email,
            action="signature_complete",
            share_id=locked_share.id,
            request=request,
        )

    from packages.core.services.document_service import (
        DocumentSignatureCommitUncertainError,
        save_document_signature_output,
    )
    from packages.core.services.entity_fs import EntityFilesystemStaleWriteError

    try:
        saved = await save_document_signature_output(
            db,
            document_id,
            entity_id,
            signed_bytes,
            input_fs_path=signature_input_fs_path,
            expected_input_sha256=source_sha256,
            working_fs_path=(
                f".signatures/{group_id}.pdf"
                if group_id and not expected_group_finalized
                else None
            ),
            signed_name=(
                _signed_copy_name(document_name)
                if expected_group_finalized
                else None
            ),
            created_by=f"Signed by {signer_email}",
            mutation_authorizer=authorize_signature_write,
        )
    except EntityFilesystemStaleWriteError as exc:
        await db.rollback()
        raise CodedError(
            409,
            code="permissions.error.signature_request.document_changed",
            message="The PDF changed while it was being signed. Ask the sender for a new request",
        ) from exc
    except DocumentSignatureCommitUncertainError as exc:
        raise CodedError(
            503,
            code="permissions.error.signature_request.commit_uncertain",
            message="Signature completion is still being reconciled. Reload this request before retrying",
        ) from exc
    if saved is None:
        raise HTTPException(404, "PDF is no longer available")
    signed_document = saved.signed_document
    signed_document_id = signed_document.id if signed_document is not None else None
    signed_document_name = signed_document.name if signed_document is not None else None

    creator_email = (await db.execute(
        select(User.email).where(User.id == creator_id, User.entity_id == entity_id)
    )).scalar_one_or_none()
    if creator_email and group_result["finalized"]:
        safe_name = html.escape(document_name)
        safe_signer = html.escape(signer_email)
        subject_document = re.sub(r"[\r\n]+", " ", document_name).strip() or "PDF document"
        await db.rollback()
        notice_share = await db.get(Share, share_id)
        if notice_share is not None:
            await _send_signature_mail(
                db, notice_share, to=creator_email,
                subject=f"Signed: {subject_document}",
                content=f"<p><strong>{safe_name}</strong> was signed by {safe_signer}.</p>",
            )
    return SignatureCompletionResponse(
        status="completed",
        signed_at=signed_at,
        signer_email=signer_email,
        document_sha256=signed_sha256,
        group_finalized=group_result["finalized"],
        signed_document_id=signed_document_id,
        signed_document_name=signed_document_name,
    )


class ShareOtpRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ShareOtpVerifyRequest(ShareOtpRequest):
    code: str = Field(min_length=6, max_length=6)


def _normalize_share_otp_email(share: Share, email: str) -> str:
    if not _is_signature_share(share):
        return email
    try:
        return _normalize_signature_email(email)
    except HTTPException as exc:
        # Reuse the normal OTP failure path, including request anti-enumeration.
        raise ShareAccessError("Verification code is invalid or expired") from exc


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
        challenge = create_otp_challenge_record(share, _normalize_share_otp_email(share, req.email))
    except ShareAccessError:
        # Keep recipient membership private even from someone holding the
        # opaque share URL. The response is intentionally indistinguishable.
        await db.rollback()
        return {"status": "sent"}
    # The challenge must be durable before external provider I/O, and no
    # lifecycle/resource/share row lock may be held while email is sent.
    share_entity_id = str(share.entity_id)
    signature_share = _is_signature_share(share)
    await db.commit()
    if signature_share:
        delivered = await _send_signature_mail(
            db, share, to=challenge.email,
            subject="Verify your email to sign a document",
            content=f"<p>Your verification code is <strong>{html.escape(challenge.code)}</strong>.</p>",
        )
    else:
        from packages.core.services.email_service import send_share_verification_email
        delivered = await send_share_verification_email(challenge.email, challenge.code)
    if not delivered:
        failed_share = (await db.execute(
            select(Share)
            .where(
                Share.id == share_id,
                Share.entity_id == share_entity_id,
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
        access_token = verify_otp_challenge(share, _normalize_share_otp_email(share, req.email), req.code)
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

        target_doc, full_path = await _signature_content_target(
            db,
            share,
            doc,
            unavailable_message="File is not available for preview",
        )

        media_type = target_doc.mime_type or "application/octet-stream"
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

        target_doc, full_path = await _signature_content_target(
            db,
            share,
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
        filename = target_doc.name
        media_type = target_doc.mime_type or "application/octet-stream"
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
    if not await _can_manage_full_document_access(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.access_request.view_owner_only",
            message="Only the document owner, a Co-owner, or an admin can view access requests",
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
    if not await _can_manage_full_document_access(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.access_request.decide_owner_only",
            message="Only the document owner, a Co-owner, or an admin can decide access requests",
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
        owner_or_admin = await _is_owner_or_admin(db, doc, user)
        grantor_capabilities = None if owner_or_admin else await document_grant_capabilities_for_user(
            db, document=doc, user_id=user.id,
        )
        expires_at = req.expires_at
        requested_capabilities = list(pending.requested_capabilities or [])
        proposed_capabilities = (
            requested_capabilities
            if req.approved_capabilities is None
            else req.approved_capabilities
        )
        try:
            policy = ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT)
            capabilities = policy.validate(
                proposed_capabilities,
                requested_capabilities=requested_capabilities,
                grantor_capabilities=grantor_capabilities,
            )
            if not owner_or_admin:
                expires_at = policy.delegated_expiry(
                    capabilities,
                    grants=await document_grants_for_user(db, document=doc, user_id=user.id),
                    expires_at=expires_at,
                )
        except ResourceGrantPolicyError as exc:
            raise HTTPException(403 if grantor_capabilities is not None else 400, str(exc)) from exc

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
            expires_at=expires_at,
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
    # Owner and Co-owner self-service (RFC §13.8) — admin can also see.
    if not await _can_manage_full_document_access(db, doc, user):
        raise HTTPException(403, "Only the document owner, a Co-owner, or an admin can view access history")
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

    An owner, Co-owner, or global manager can submit.
    The actual ``shares`` row is NOT created here — it's materialized by
    ``decide_share_approval`` when an admin approves.
    """
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await _can_manage_full_document_access(db, doc, user):
        raise HTTPException(
            403, "Only the document owner, a Co-owner, or an admin can request a share approval",
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

    config = _serialize_share_config(req)
    if not await _is_owner_or_admin(db, doc, user):
        try:
            delegated_expires_at = ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT).delegated_expiry(
                req.capabilities,
                grants=await document_grants_for_user(db, document=doc, user_id=user.id),
                # Snapshot authority only; the share lifetime starts on approval.
                expires_at=None,
                authority_capabilities=(Capability.SHARE_EXTERNAL,),
                clamp_expiry=True,
            )
        except ResourceGrantPolicyError as exc:
            raise HTTPException(403, str(exc)) from exc
        config["delegated_expires_at"] = delegated_expires_at.isoformat() if delegated_expires_at else None

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
        metadata_=config,
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

    Owner, Co-owner, or global manager — same gating as other doc admin actions. Most useful
    on the admin inbox surface (filter status=pending).
    """
    doc = await _load_doc(db, doc_id, user.entity_id)
    if not await _can_manage_full_document_access(db, doc, user):
        raise CodedError(
            403,
            code="permissions.error.share_approval.view_owner_only",
            message="Only the document owner, a Co-owner, or an admin can view share approvals",
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

    Decision requires explicit global Knowledge-management authority. On approve, we
    materialize the actual ``shares`` row from the snapshotted config and
    return the raw token + URL exactly once for the admin to relay.
    """
    doc = await _load_doc(db, doc_id, user.entity_id, for_update=True)
    if not await effective_user_has_permission(db, user, Permission.DOCS_MANAGE_ALL):
        raise CodedError(
            403,
            code="permissions.error.share_approval.admin_required",
            message="Global Knowledge management permission is required to decide share approvals",
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
        delegated_expires_at = None
        if "delegated_expires_at" in cfg:
            requester = await db.get(User, pending.requester_user_id)
            if requester is None or not await _can_manage_full_document_access(db, doc, requester):
                raise HTTPException(403, "The requester's Co-owner access is no longer available")
            authority_expiry = datetime.fromisoformat(cfg["delegated_expires_at"]) if cfg["delegated_expires_at"] else None
            if authority_expiry is not None and authority_expiry <= now:
                raise HTTPException(403, "The requester's Co-owner approval authority has expired")
            requested_expiry = now + timedelta(days=proxy.expires_in_days) if proxy.expires_in_days is not None else None
            if authority_expiry is not None:
                requested_expiry = min(requested_expiry, authority_expiry) if requested_expiry else authority_expiry
            try:
                delegated_expires_at = ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT).delegated_expiry(
                    proxy.capabilities,
                    grants=await document_grants_for_user(db, document=doc, user_id=requester.id),
                    expires_at=requested_expiry,
                    authority_capabilities=(Capability.SHARE_EXTERNAL,),
                    clamp_expiry=True,
                )
            except ResourceGrantPolicyError as exc:
                raise HTTPException(403, str(exc)) from exc
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
            delegated_expires_at=delegated_expires_at,
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
