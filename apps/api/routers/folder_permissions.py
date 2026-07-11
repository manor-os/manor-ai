"""Folder-level permission endpoints (RFC §13.3, Phase B).

Mirrors ``document_permissions.py`` for folders. A folder's permissions
cascade to every document inside it via ``authorize()``'s walk-up logic:

  * Grants on a folder also grant access to all child documents (and
    sub-folders, transitively).
  * Classification on a folder is the **floor** for documents inside —
    children's effective classification is ``max(self, ancestor folders)``.
  * Visibility on a folder is the **ceiling** — children cannot be more
    public than the deepest ancestor folder.

Endpoints:
  POST /folders/{id}/properties        — change visibility / classification /
                                         client_visible. Optional cascade=true
                                         applies the new floors/ceilings to
                                         every document inside immediately
                                         (otherwise enforcement is lazy at
                                         next read).
  GET/POST/DELETE /folders/{id}/grants — folder-level resource_grants
  GET/POST/DELETE /folders/{id}/shares — folder-level external Share rows
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from apps.api.web_base import public_web_base
from packages.core.database import get_db
from packages.core.models import (
    Capability,
    Classification,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    Share,
    SubjectType,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.staff import Staff
from packages.core.models.user import User, UserMembership
from packages.core.permissions import Permission, has_permission
from packages.core.services.document_access import user_has_folder_capability

router = APIRouter(prefix="/api/v1/folders", tags=["folder-permissions"])


# ── Helpers (shared with document_permissions; duplicated here to keep
# the import graph simple — they're tiny.) ───────────────────────────────


async def _load_folder(
    db: AsyncSession, folder_id: str, entity_id: str
) -> DocumentFolder:
    folder = (
        await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.id == folder_id,
                DocumentFolder.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if not folder:
        raise CodedError(
            404,
            code="permissions.error.folder.not_found",
            message="Folder not found",
        )
    return folder


def _is_owner_or_admin(folder: DocumentFolder, user: User) -> bool:
    if folder.owner_id and folder.owner_id == user.id:
        return True
    return has_permission(user.role, Permission.ADMIN_SETTINGS)


async def _can_manage_internal_acl(db: AsyncSession, folder: DocumentFolder, user: User) -> bool:
    if _is_owner_or_admin(folder, user):
        return True
    return await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
        user_id=user.id,
        capabilities={Capability.GRANT_ACCESS, Capability.SHARE_INTERNAL},
    )


async def _can_manage_external_share(db: AsyncSession, folder: DocumentFolder, user: User) -> bool:
    if _is_owner_or_admin(folder, user):
        return True
    return await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
        user_id=user.id,
        capabilities={Capability.SHARE_EXTERNAL, Capability.GRANT_ACCESS},
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_CLASS_RANK = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.RESTRICTED: 3,
}

_VIS_RANK = {
    "private": 0,
    "workspace": 1,
    "entity": 2,
    "public": 3,
}


async def _walk_descendant_folders(
    db: AsyncSession, root_id: str, entity_id: str,
) -> list[DocumentFolder]:
    """Return all folders rooted at ``root_id`` (inclusive). Iterative
    BFS to avoid recursion limit / O(n²) re-loads."""
    all_folders = (
        await db.execute(
            select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
        )
    ).scalars().all()
    children_by_parent: dict[str | None, list[DocumentFolder]] = {}
    for f in all_folders:
        children_by_parent.setdefault(f.parent_id, []).append(f)
    out: list[DocumentFolder] = []
    queue: list[str] = [root_id]
    while queue:
        current_id = queue.pop()
        for child in children_by_parent.get(current_id, []):
            out.append(child)
            queue.append(child.id)
    return out


# ── Properties: visibility / classification / client_visible ─────────────


class FolderPropertiesRequest(BaseModel):
    visibility: Literal["private", "workspace", "entity", "public"] | None = None
    classification: Literal["public", "internal", "confidential", "restricted"] | None = None
    client_visible: bool | None = None
    # When true (default), apply the new floors/ceilings to every document
    # already inside this folder + every descendant folder. When false,
    # only the folder row changes — children keep their existing values
    # and pick up the new constraints lazily on read.
    cascade: bool = True


class FolderPropertiesResponse(BaseModel):
    id: str
    visibility: str | None = None
    classification: str | None = None
    client_visible: bool | None = None
    cascade_summary: dict = Field(default_factory=dict)


@router.post("/{folder_id}/properties", response_model=FolderPropertiesResponse)
async def set_folder_properties(
    folder_id: str,
    req: FolderPropertiesRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not _is_owner_or_admin(folder, user):
        raise HTTPException(
            403, "Only the folder owner or an admin can change folder properties",
        )

    # Cross-field invariant: restricted ≠ public (RFC §13.14 inv 1)
    if req.classification == Classification.RESTRICTED and req.visibility == "public":
        raise HTTPException(
            400, "Restricted folder cannot have public visibility",
        )
    # confidential+ can't be client_visible
    if req.client_visible and req.classification in (
        Classification.CONFIDENTIAL, Classification.RESTRICTED,
    ):
        raise HTTPException(
            400, "Confidential/Restricted folders cannot be client_visible",
        )

    # Apply to the folder row first.
    if req.visibility is not None:
        folder.visibility = req.visibility
    if req.classification is not None:
        folder.classification = req.classification
    if req.client_visible is not None:
        folder.client_visible = req.client_visible

    cascade_summary: dict = {"docs_updated": 0, "subfolders_updated": 0}

    if req.cascade and (req.classification is not None or req.visibility is not None or req.client_visible is not None):
        # Walk all descendant folders + child docs.
        descendant_folders = await _walk_descendant_folders(
            db, folder.id, user.entity_id,
        )
        for sub in descendant_folders:
            if req.classification is not None:
                # Floor: child classification ≥ folder classification.
                if _CLASS_RANK.get(sub.classification or Classification.INTERNAL, 1) < _CLASS_RANK[req.classification]:
                    sub.classification = req.classification
                    cascade_summary["subfolders_updated"] += 1
            if req.visibility is not None:
                # Ceiling: child visibility ⊆ folder visibility.
                if _VIS_RANK.get(sub.visibility or "entity", 2) > _VIS_RANK[req.visibility]:
                    sub.visibility = req.visibility
                    cascade_summary["subfolders_updated"] += 1
            if req.client_visible is False:
                sub.client_visible = False

        # All docs whose folder_id is folder.id or any descendant.
        folder_ids = [folder.id] + [f.id for f in descendant_folders]
        docs = (
            await db.execute(
                select(Document).where(
                    Document.entity_id == user.entity_id,
                    Document.folder_id.in_(folder_ids),
                )
            )
        ).scalars().all()
        for doc in docs:
            if req.classification is not None:
                if _CLASS_RANK.get(doc.classification or Classification.INTERNAL, 1) < _CLASS_RANK[req.classification]:
                    doc.classification = req.classification
                    cascade_summary["docs_updated"] += 1
            if req.visibility is not None:
                if _VIS_RANK.get(doc.visibility or "entity", 2) > _VIS_RANK[req.visibility]:
                    doc.visibility = req.visibility
                    cascade_summary["docs_updated"] += 1
            if req.client_visible is False:
                doc.client_visible = False

    await db.commit()
    await db.refresh(folder)
    return FolderPropertiesResponse(
        id=folder.id,
        visibility=folder.visibility,
        classification=folder.classification,
        client_visible=folder.client_visible,
        cascade_summary=cascade_summary,
    )


# ── Grants ────────────────────────────────────────────────────────────────


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


@router.get("/{folder_id}/grants", response_model=list[GrantResponse])
async def list_folder_grants(
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not await _can_manage_internal_acl(db, folder, user):
        raise CodedError(
            403,
            code="permissions.error.folder.grant_view_forbidden",
            message="Only the folder owner/admin or a user with grant access can view grants",
        )
    rows = (
        await db.execute(
            select(ResourceGrant)
            .where(
                ResourceGrant.resource_type == ResourceType.DOCUMENT_FOLDER,
                ResourceGrant.resource_id == folder.id,
                ResourceGrant.entity_id == user.entity_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
            .order_by(desc(ResourceGrant.granted_at))
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    out: list[GrantResponse] = []
    for r in rows:
        if r.expires_at and r.expires_at < now:
            continue
        out.append(await _grant_to_response(db, r))
    return out


@router.post("/{folder_id}/grants", response_model=GrantResponse, status_code=201)
async def create_folder_grant(
    folder_id: str,
    req: CreateGrantRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not await _can_manage_internal_acl(db, folder, user):
        raise HTTPException(
            403, "Only the folder owner/admin or a user with grant access can grant access",
        )

    _ALLOWED_CAPS = {
        Capability.VIEW, Capability.VIEW_REDACTED, Capability.COMMENT,
        Capability.EDIT, Capability.MANAGE_METADATA, Capability.SHARE_INTERNAL,
        Capability.SHARE_EXTERNAL, Capability.DOWNLOAD, Capability.PRINT,
        Capability.RECLASSIFY, Capability.DELETE, Capability.GRANT_ACCESS,
        Capability.UPLOAD_TO,  # folder-specific
    }
    unknown = set(req.capabilities) - _ALLOWED_CAPS
    if unknown:
        raise HTTPException(400, f"Unknown capabilities: {sorted(unknown)}")

    subject_id = req.subject_id
    existing_subject_ids = [subject_id]
    if req.subject_type == SubjectType.USER:
        subject_id, _, _ = await _resolve_user_grant_subject(
            db,
            entity_id=user.entity_id,
            subject_id=req.subject_id,
        )
        existing_subject_ids = list(dict.fromkeys([subject_id, req.subject_id]))

    # Idempotent upsert on (resource, subject)
    existing = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_type == ResourceType.DOCUMENT_FOLDER,
                ResourceGrant.resource_id == folder.id,
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
            resource_type=ResourceType.DOCUMENT_FOLDER,
            resource_id=folder.id,
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


@router.delete("/{folder_id}/grants/{grant_id}", status_code=204)
async def revoke_folder_grant(
    folder_id: str,
    grant_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not await _can_manage_internal_acl(db, folder, user):
        raise CodedError(
            403,
            code="permissions.error.folder.grant_revoke_owner_only",
            message="Only the folder owner/admin or a user with grant access can revoke",
        )
    grant = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.resource_type == ResourceType.DOCUMENT_FOLDER,
                ResourceGrant.resource_id == folder.id,
                ResourceGrant.entity_id == user.entity_id,
            )
        )
    ).scalar_one_or_none()
    if not grant:
        raise CodedError(
            404,
            code="permissions.error.folder.grant_not_found",
            message="Grant not found",
        )
    grant.status = GrantStatus.REVOKED
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoked_by = user.id
    await db.commit()


# ── External shares on a folder ─────────────────────────────────────────


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


@router.get("/{folder_id}/shares", response_model=list[ShareResponse])
async def list_folder_shares(
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not await _can_manage_external_share(db, folder, user):
        raise CodedError(
            403,
            code="permissions.error.folder.share_view_forbidden",
            message="Only the folder owner/admin or a user with external share access can view shares",
        )
    rows = (
        await db.execute(
            select(Share)
            .where(
                Share.resource_type == ResourceType.DOCUMENT_FOLDER,
                Share.resource_id == folder.id,
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


@router.post("/{folder_id}/shares", response_model=CreateShareResponse, status_code=201)
async def create_folder_share(
    folder_id: str,
    req: CreateShareRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not await _can_manage_external_share(db, folder, user):
        raise CodedError(
            403,
            code="permissions.error.folder.share_external_owner_only",
            message="Only the folder owner/admin or a user with external share access can share externally",
        )

    cls = getattr(folder, "classification", None)
    if cls == Classification.RESTRICTED:
        raise CodedError(
            400,
            code="permissions.error.folder.restricted_no_external",
            message="Restricted folder cannot be shared externally",
        )
    if cls == Classification.CONFIDENTIAL:
        # Same approval policy as confidential docs — refuse here so the
        # frontend can route to the approval flow. (Approval inbox for
        # folders is wired separately; see TODO Phase B.5.)
        raise CodedError(
            409,
            code="permissions.error.folder.confidential_needs_approval",
            message="Confidential folder requires approval. Submit via the share-approval flow.",
        )

    _EXTERNAL_CAPS = {Capability.VIEW, Capability.COMMENT, Capability.DOWNLOAD}
    unknown = set(req.capabilities) - _EXTERNAL_CAPS
    if unknown:
        raise HTTPException(
            400, f"External shares only support {sorted(_EXTERNAL_CAPS)}; got {sorted(unknown)}",
        )

    if req.audience_type == "anonymous":
        audience = "anonymous"
    elif req.audience_type == "email":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for email audience")
        audience = f"email:{req.audience_value.strip().lower()}"
    elif req.audience_type == "domain":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for domain audience")
        audience = f"domain:{req.audience_value.strip().lower()}"
    else:
        raise HTTPException(400, f"Invalid audience_type: {req.audience_type}")

    raw_token = secrets.token_urlsafe(32)
    share = Share(
        id=generate_ulid(),
        entity_id=user.entity_id,
        resource_type=ResourceType.DOCUMENT_FOLDER,
        resource_id=folder.id,
        token_hash=_hash_token(raw_token),
        capabilities=req.capabilities,
        audience=audience,
        require_otp=req.require_otp,
        watermark=req.watermark,
        allow_download=req.allow_download and (Capability.DOWNLOAD in req.capabilities),
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=req.expires_in_days),
        max_uses=None,
        use_count=0,
        status="active",
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    # Browser-bound link — see document_permissions.py:_materialize_share
    # for why we use public_web_base instead of request.base_url.
    base = public_web_base(request)
    url = f"{base}/shared-folder/{raw_token}"
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


@router.delete("/{folder_id}/shares/{share_id}", status_code=204)
async def revoke_folder_share(
    folder_id: str,
    share_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(db, folder_id, user.entity_id)
    if not await _can_manage_external_share(db, folder, user):
        raise CodedError(
            403,
            code="permissions.error.folder.revoke_share_owner_only",
            message="Only the folder owner/admin or a user with external share access can revoke",
        )
    share = (
        await db.execute(
            select(Share).where(
                Share.id == share_id,
                Share.resource_type == ResourceType.DOCUMENT_FOLDER,
                Share.resource_id == folder.id,
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
# Public viewer (unauthenticated, token-gated). Mirrors
# document_permissions.public_router's /shared-doc/{token}.
# ─────────────────────────────────────────────────────────────────────────


class PublicFolderDocResponse(BaseModel):
    id: str
    name: str
    file_size: int | None = None
    file_type: str | None = None
    classification: str | None = None


class SharedFolderResponse(BaseModel):
    folder_id: str
    name: str
    classification: str | None = None
    capabilities: list[str]
    watermark: bool
    allow_download: bool
    expires_at: datetime | None = None
    # Direct (non-recursive) children of the shared folder. Subfolder
    # contents are not enumerated to keep the public surface small;
    # subfolders themselves appear in the listing so the user knows the
    # structure exists, but their contents require navigation through
    # additional shares.
    documents: list[PublicFolderDocResponse] = Field(default_factory=list)
    subfolders: list[dict] = Field(default_factory=list)  # [{id, name}]


public_router = APIRouter(prefix="/api/v1/shared-folder", tags=["public-share"])


@public_router.get("/{token}", response_model=SharedFolderResponse)
async def view_shared_folder(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated viewer for a folder shared via opaque token.

    Returns the folder metadata + a flat list of its direct child documents
    (not recursive). Subfolder list is included so the recipient sees the
    structure exists even though contents aren't enumerated.
    """
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
    if share.resource_type != ResourceType.DOCUMENT_FOLDER:
        raise HTTPException(404, "Not a folder share")

    folder = (
        await db.execute(
            select(DocumentFolder).where(DocumentFolder.id == share.resource_id)
        )
    ).scalar_one_or_none()
    if not folder:
        raise HTTPException(404, "Underlying folder not found")

    share.use_count = (share.use_count or 0) + 1
    share.last_used_at = now

    # Direct children: docs + sub-folders.
    docs = (
        await db.execute(
            select(Document).where(
                Document.entity_id == folder.entity_id,
                Document.folder_id == folder.id,
                Document.is_trashed == False,  # noqa: E712
            )
        )
    ).scalars().all()
    subfolders = (
        await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == folder.entity_id,
                DocumentFolder.parent_id == folder.id,
            )
        )
    ).scalars().all()

    await db.commit()
    _ = request  # not used yet; left for future audit-log wiring per-doc preview
    return SharedFolderResponse(
        folder_id=folder.id,
        name=folder.name,
        classification=getattr(folder, "classification", None),
        capabilities=list(share.capabilities or []),
        watermark=bool(share.watermark),
        allow_download=bool(share.allow_download),
        expires_at=share.expires_at,
        documents=[
            PublicFolderDocResponse(
                id=d.id,
                name=d.name,
                file_size=d.file_size,
                file_type=d.file_type,
                classification=getattr(d, "classification", None),
            )
            for d in docs
        ],
        subfolders=[{"id": f.id, "name": f.name} for f in subfolders],
    )
